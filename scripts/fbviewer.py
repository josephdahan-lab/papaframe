#!/usr/bin/env python3
"""Minimal framebuffer slideshow viewer for PapaFrame.

Displays images from a file list by writing directly to /dev/fb0.
Uses ~19 MB RSS vs fbi's ~40 MB+, never grabs DRM master (so
setterm --blank force works), and handles one image at a time with
no readahead buffering.

Framebuffer format: RGB565 (16 bpp), 1920x1080, stride 3840.
"""

import mmap
import os
import signal
import struct
import sys
import time
from pathlib import Path

from PIL import Image

# ── Framebuffer constants ─────────────────────────────────────────
FB_DEV = '/dev/fb0'
FB_W, FB_H = 1920, 1080
FB_BPP = 2  # 16-bit / 2 bytes per pixel
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


def rgb_to_565(r, g, b):
    """Pack 8-bit RGB into 16-bit RGB565."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def image_to_565(img):
    """Convert a PIL Image (already sized to FB_W x FB_H) to an RGB565
    bytes buffer ready to write to the framebuffer."""
    pixels = img.tobytes()
    buf = bytearray(FB_SIZE)
    src = 0
    dst = 0
    for _ in range(FB_W * FB_H):
        r = pixels[src]
        g = pixels[src + 1]
        b = pixels[src + 2]
        val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        buf[dst] = val & 0xFF
        buf[dst + 1] = (val >> 8) & 0xFF
        src += 3
        dst += 2
    return bytes(buf)


def image_to_565_fast(img):
    """Fast RGB→RGB565 conversion. Minimises temporary arrays to keep
    peak RSS low on memory-constrained boards."""
    import numpy as np
    arr = np.asarray(img, dtype=np.uint16)  # (H, W, 3) as uint16
    # Single expression — numpy fuses into one output array
    rgb565 = (
        ((arr[:, :, 0] & 0xF8) << 8) |
        ((arr[:, :, 1] & 0xFC) << 3) |
        ( arr[:, :, 2]         >> 3)
    ).astype('<u2')
    del arr
    result = rgb565.tobytes()
    del rgb565
    return result


def fit_image(path):
    """Load an image, scale to fit FB_W x FB_H (letterboxed), return RGB."""
    img = Image.open(path)
    img = img.convert('RGB')

    # Scale to fit, preserving aspect ratio
    iw, ih = img.size
    scale = min(FB_W / iw, FB_H / ih)
    new_w = int(iw * scale)
    new_h = int(ih * scale)

    if new_w != iw or new_h != ih:
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # Paste onto black canvas (letterbox)
    if new_w != FB_W or new_h != FB_H:
        canvas = Image.new('RGB', (FB_W, FB_H), (0, 0, 0))
        x = (FB_W - new_w) // 2
        y = (FB_H - new_h) // 2
        canvas.paste(img, (x, y))
        img = canvas

    return img


def read_duration():
    """Read current slide duration from the IPC file."""
    try:
        val = DURATION_FILE.read_text().strip()
        d = int(val)
        return max(1, d)
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
        Path('/tmp/frame_current_photo.txt').write_text(path)
    except Exception:
        pass


def main():
    global _quit

    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} <file-list>', file=sys.stderr)
        sys.exit(2)

    list_path = sys.argv[1]

    # Check for numpy (fast path) vs pure-Python (slow path)
    try:
        import numpy as np
        to_565 = image_to_565_fast
        print('fbviewer: using numpy fast path', file=sys.stderr)
    except ImportError:
        to_565 = image_to_565
        print('fbviewer: numpy not available, using pure-Python conversion '
              '(slower but works)', file=sys.stderr)

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

    # Resume support: if RESUME_FILE exists, start from that photo
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
        # Check stop flag
        if STOP_FLAG.exists():
            print('fbviewer: stop flag, exiting', file=sys.stderr)
            break

        # Reload list if it changed on disk
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

        # Wrap index
        if idx >= len(photos):
            idx = 0

        photo_path = photos[idx]

        # Display the image
        try:
            img = fit_image(photo_path)
            raw = to_565(img)
            del img
            fb.seek(0)
            fb.write(raw)
            del raw
            import gc; gc.collect()
            write_current_photo(photo_path)
        except Exception as e:
            print(f'fbviewer: skip {photo_path}: {e}', file=sys.stderr)
            idx += 1
            continue

        # Sleep in short increments so we can respond to stop/duration changes
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
