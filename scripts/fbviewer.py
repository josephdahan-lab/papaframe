#!/usr/bin/env python3
"""Minimal framebuffer slideshow viewer for PapaFrame.

Displays images from a file list by writing directly to /dev/fb0.
Uses ~19 MB RSS vs fbi's ~40 MB+, never grabs DRM master (so
setterm --blank force works), and handles one image at a time with
no readahead buffering.

Image decoding:
  - If Pillow is available (full installs): uses PIL for decode + resize.
  - Otherwise (lite installs): shells out to djpeg(1) for JPEG decode
    and does nearest-neighbor resize in pure Python. Requires the
    libjpeg-turbo-progs package (installed by setup.sh on lite boards).
"""

import mmap
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

# ── Try Pillow, fall back to djpeg ──────────────────────────────────
try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# ── Framebuffer setup ────────────────────────────────────────────────
FB_DEV = '/dev/fb0'

def _detect_fb():
    """Read framebuffer geometry from sysfs. Returns (width, height, bpp)."""
    base = '/sys/class/graphics/fb0'
    try:
        with open(f'{base}/virtual_size') as f:
            w, h = f.read().strip().split(',')
            w, h = int(w), int(h)
        with open(f'{base}/bits_per_pixel') as f:
            bpp = int(f.read().strip())
        return w, h, bpp
    except Exception:
        return 1920, 1080, 16  # safe default for Pi

FB_W, FB_H, _FB_BPP = _detect_fb()
FB_BPP = _FB_BPP // 8  # bytes per pixel
FB_STRIDE = FB_W * FB_BPP
FB_SIZE = FB_STRIDE * FB_H

# ── IPC files (shared with start_frame.sh) ────────────────────────
STOP_FLAG = Path('/tmp/frame_stop_requested')
DURATION_FILE = Path('/tmp/frame_duration_override.txt')
RESUME_FILE = Path('/tmp/frame_resume_photo.txt')

# ── Signal handling ───────────────────────────────────────────────
_quit = False

def _on_signal(signum, frame):
    global _quit
    _quit = True

signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ═══════════════════════════════════════════════════════════════════
# Image loading — Pillow path
# ═══════════════════════════════════════════════════════════════════

def _fit_image_pil(path):
    """Load, scale to fit FB, return RGB PIL Image."""
    img = Image.open(path)
    img = img.convert('RGB')
    iw, ih = img.size
    scale = min(FB_W / iw, FB_H / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)

    if new_w != iw or new_h != ih:
        img = img.resize((new_w, new_h), Image.LANCZOS)

    if new_w != FB_W or new_h != FB_H:
        canvas = Image.new('RGB', (FB_W, FB_H), (0, 0, 0))
        canvas.paste(img, ((FB_W - new_w) // 2, (FB_H - new_h) // 2))
        img = canvas
    return img


def _pil_to_565(img):
    """Convert PIL Image → RGB565 bytes for the framebuffer."""
    try:
        import numpy as np
        arr = np.asarray(img, dtype=np.uint16)
        rgb565 = (
            ((arr[:, :, 0] & 0xF8) << 8) |
            ((arr[:, :, 1] & 0xFC) << 3) |
            ( arr[:, :, 2]         >> 3)
        ).astype('<u2')
        return rgb565.tobytes()
    except ImportError:
        pass
    # Pure-Python fallback
    pixels = img.tobytes()
    return _rgb_bytes_to_565(pixels, FB_W, FB_H)


def _load_pil(path):
    """Load image via Pillow, return RGB565 bytes."""
    img = _fit_image_pil(path)
    result = _pil_to_565(img)
    del img
    return result


# ═══════════════════════════════════════════════════════════════════
# Image loading — djpeg/subprocess path (no Pillow)
# ═══════════════════════════════════════════════════════════════════

def _parse_ppm(data):
    """Parse PPM P6 binary data → (width, height, rgb_bytes)."""
    idx = 0
    # Magic: P6\n
    end = data.index(b'\n', idx)
    magic = data[idx:end].strip()
    if magic != b'P6':
        return None
    idx = end + 1

    # Skip comment lines
    while idx < len(data) and data[idx:idx + 1] == b'#':
        idx = data.index(b'\n', idx) + 1

    # Width and height (may be on one line or two)
    end = data.index(b'\n', idx)
    parts = data[idx:end].split()
    w, h = int(parts[0]), int(parts[1])
    idx = end + 1

    # Maxval
    end = data.index(b'\n', idx)
    idx = end + 1

    rgb = data[idx:idx + w * h * 3]
    return w, h, rgb


def _resize_nearest_to_565(rgb, src_w, src_h, dst_w, dst_h):
    """Nearest-neighbor resize from raw RGB bytes → letterboxed RGB565 bytes.
    Fits src into dst preserving aspect ratio, black letterbox bars."""
    scale = min(dst_w / src_w, dst_h / src_h)
    scaled_w = int(src_w * scale)
    scaled_h = int(src_h * scale)
    off_x = (dst_w - scaled_w) // 2
    off_y = (dst_h - scaled_h) // 2

    # Build the output row by row
    out = bytearray(dst_w * dst_h * 2)  # RGB565
    src_row_bytes = src_w * 3

    for dy in range(dst_h):
        # Where does this row start in the output buffer?
        out_row = dy * dst_w * 2

        if dy < off_y or dy >= off_y + scaled_h:
            # Letterbox bar — already zero (black)
            continue

        # Which source row?
        sy = int((dy - off_y) * src_h / scaled_h)
        if sy >= src_h:
            sy = src_h - 1
        src_row_start = sy * src_row_bytes

        for dx in range(dst_w):
            if dx < off_x or dx >= off_x + scaled_w:
                # Letterbox column — already zero
                continue

            sx = int((dx - off_x) * src_w / scaled_w)
            if sx >= src_w:
                sx = src_w - 1

            si = src_row_start + sx * 3
            r = rgb[si]
            g = rgb[si + 1]
            b = rgb[si + 2]
            val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

            oi = out_row + dx * 2
            out[oi] = val & 0xFF
            out[oi + 1] = (val >> 8) & 0xFF

    return bytes(out)


def _resize_nearest_to_565_fast(rgb, src_w, src_h, dst_w, dst_h):
    """Same as above but processes full rows with struct.pack for speed."""
    scale = min(dst_w / src_w, dst_h / src_h)
    scaled_w = int(src_w * scale)
    scaled_h = int(src_h * scale)
    off_x = (dst_w - scaled_w) // 2
    off_y = (dst_h - scaled_h) // 2

    # Pre-compute source X indices for the scaled region
    sx_map = [min(int(i * src_w / scaled_w), src_w - 1) for i in range(scaled_w)]
    black_row = b'\x00\x00' * dst_w

    rows = []
    for dy in range(dst_h):
        if dy < off_y or dy >= off_y + scaled_h:
            rows.append(black_row)
            continue

        sy = min(int((dy - off_y) * src_h / scaled_h), src_h - 1)
        src_row_start = sy * src_w * 3

        # Build one row of RGB565 pixels
        row = bytearray(dst_w * 2)

        for i, sx in enumerate(sx_map):
            si = src_row_start + sx * 3
            r = rgb[si]
            g = rgb[si + 1]
            b = rgb[si + 2]
            val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            oi = (off_x + i) * 2
            row[oi] = val & 0xFF
            row[oi + 1] = (val >> 8) & 0xFF

        rows.append(bytes(row))

    return b''.join(rows)


def _load_djpeg(path):
    """Decode a JPEG via djpeg subprocess, resize, return RGB565 bytes."""
    try:
        proc = subprocess.run(
            ['djpeg', '-ppm', str(path)],
            capture_output=True, timeout=60
        )
        if proc.returncode != 0:
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    parsed = _parse_ppm(proc.stdout)
    if parsed is None:
        return None

    w, h, rgb = parsed
    del proc  # free the subprocess output buffer

    return _resize_nearest_to_565_fast(rgb, w, h, FB_W, FB_H)


def _load_pnm_tool(path):
    """Try to decode non-JPEG images via common CLI tools → PPM."""
    ext = str(path).lower().rsplit('.', 1)[-1]

    # Try pngtopam/pngtopnm for PNG
    if ext == 'png':
        for tool in ('pngtopnm', 'pngtopam'):
            try:
                proc = subprocess.run(
                    [tool, str(path)],
                    capture_output=True, timeout=60
                )
                if proc.returncode == 0:
                    parsed = _parse_ppm(proc.stdout)
                    if parsed:
                        w, h, rgb = parsed
                        return _resize_nearest_to_565_fast(rgb, w, h, FB_W, FB_H)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

    return None


# ═══════════════════════════════════════════════════════════════════
# Common helpers
# ═══════════════════════════════════════════════════════════════════

def _rgb_bytes_to_565(pixels, w, h):
    """Convert raw RGB bytes → RGB565 bytes (pure Python)."""
    buf = bytearray(w * h * 2)
    src = 0
    dst = 0
    total = w * h
    for _ in range(total):
        r = pixels[src]
        g = pixels[src + 1]
        b = pixels[src + 2]
        val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        buf[dst] = val & 0xFF
        buf[dst + 1] = (val >> 8) & 0xFF
        src += 3
        dst += 2
    return bytes(buf)


def load_image(path):
    """Load an image and return RGB565 bytes ready for the framebuffer.
    Tries PIL first, falls back to djpeg for JPEG files."""
    if _HAS_PIL:
        return _load_pil(path)

    ext = str(path).lower().rsplit('.', 1)[-1]
    if ext in ('jpg', 'jpeg'):
        result = _load_djpeg(path)
        if result:
            return result

    # Try PNM tools for non-JPEG (or if djpeg failed)
    result = _load_pnm_tool(path)
    if result:
        return result

    return None


def read_duration():
    """Read current slide duration from the IPC file."""
    try:
        val = DURATION_FILE.read_text().strip()
        return max(1, int(val))
    except Exception:
        return 30


def load_file_list(path):
    """Load and return lines from a file list."""
    try:
        with open(path) as f:
            return [line.strip() for line in f if line.strip()]
    except Exception:
        return []


def write_current_photo(path):
    """Write the currently-displayed photo path for the web UI."""
    try:
        Path('/tmp/frame_current_photo.txt').write_text(str(path))
    except Exception:
        pass


def main():
    global _quit

    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} <file-list>', file=sys.stderr)
        sys.exit(2)

    list_path = sys.argv[1]

    backend = 'PIL' if _HAS_PIL else 'djpeg'
    print(f'fbviewer: using {backend} backend, fb={FB_W}x{FB_H}@{_FB_BPP}bpp',
          file=sys.stderr)

    # Open and mmap the framebuffer
    fb_fd = os.open(FB_DEV, os.O_RDWR)
    fb = mmap.mmap(fb_fd, FB_SIZE, mmap.MAP_SHARED,
                   mmap.PROT_READ | mmap.PROT_WRITE)

    # Clear screen to black
    fb.seek(0)
    fb.write(b'\x00' * FB_SIZE)

    photos = load_file_list(list_path)
    if not photos:
        print(f'fbviewer: no photos in {list_path}', file=sys.stderr)
        time.sleep(5)
        fb.close()
        os.close(fb_fd)
        sys.exit(1)

    idx = 0

    # Resume support
    if RESUME_FILE.exists():
        try:
            resume_path = RESUME_FILE.read_text().strip()
            RESUME_FILE.unlink(missing_ok=True)
            if resume_path in photos:
                idx = photos.index(resume_path)
                print(f'fbviewer: resuming at index {idx}', file=sys.stderr)
        except Exception:
            pass

    print(f'fbviewer: started, {len(photos)} photos, pid={os.getpid()}',
          file=sys.stderr)

    list_mtime = 0
    try:
        list_mtime = os.path.getmtime(list_path)
    except OSError:
        pass

    while not _quit:
        if STOP_FLAG.exists():
            print('fbviewer: stop flag, exiting', file=sys.stderr)
            break

        # Reload list if file changed
        try:
            cur_mtime = os.path.getmtime(list_path)
            if cur_mtime != list_mtime:
                new_photos = load_file_list(list_path)
                if new_photos:
                    photos = new_photos
                    idx = idx % len(photos)
                    list_mtime = cur_mtime
                    print(f'fbviewer: reloaded list, {len(photos)} photos',
                          file=sys.stderr)
        except OSError:
            pass

        if idx >= len(photos):
            idx = 0

        photo_path = photos[idx]

        # Display
        try:
            raw = load_image(photo_path)
            if raw is None:
                print(f'fbviewer: skip {photo_path}: decode failed',
                      file=sys.stderr)
                idx += 1
                continue
            fb.seek(0)
            fb.write(raw)
            del raw
            import gc; gc.collect()
            write_current_photo(photo_path)
        except Exception as e:
            print(f'fbviewer: skip {photo_path}: {e}', file=sys.stderr)
            idx += 1
            continue

        # Sleep in short increments for responsiveness
        duration = read_duration()
        elapsed = 0.0
        while elapsed < duration and not _quit:
            if STOP_FLAG.exists():
                break
            time.sleep(1.0)
            elapsed += 1.0

        idx += 1

    # Clear screen on exit
    fb.seek(0)
    fb.write(b'\x00' * FB_SIZE)
    fb.close()
    os.close(fb_fd)
    print('fbviewer: exited cleanly', file=sys.stderr)


if __name__ == '__main__':
    main()
