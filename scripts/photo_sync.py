#!/usr/bin/env python3
"""
PapaFrame Photo Sync — mirrors photos from the NAS to local storage, resized.

Reads folder pairs from sync_config.json. For each pair it lists both sides
and works from the difference, so a photo is never skipped because of its
timestamp:

  * on the source but not the frame  → resized to fit SYNC_MAX_WIDTH x
    SYNC_MAX_HEIGHT (never upscaled) and saved as JPEG. EXIF (date taken,
    GPS) is kept, the orientation is applied to the pixels, and the copy
    gets the source file's modification time.
  * on the frame but no longer on the source → moved to the trash folder
    and recorded in sync_trash.json. Nothing is deleted here; the admin page
    lists trashed photos so they can be deleted for good or restored.
  * on both → the copy's modification time is set to the source's if they
    differ (older syncs stamped copies with the time of the sync). A copy
    that is 0 bytes (left by a failed sync) is copied again.

Files are matched by relative path without extension, case-insensitively:
source "2023/IMG_1.HEIC" is the frame's "2023/IMG_1.jpg".

Modes:
  photo_sync.py             sync every pair
  photo_sync.py --preview   report the differences only (writes
                            sync_preview.json); changes nothing
  photo_sync.py --approve-trash DEST
                            allow trashing more than the safety limit
                            for that destination folder (admin "approve")

Every action is logged to sync_log.json (entries older than a year are
pruned). Progress is flushed every 25 files so the dashboard stays current.
"""

import fcntl
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:
    HEIF_SUPPORTED = False

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_SH = Path(os.environ.get('PAPAFRAME_CONFIG', PROJECT_DIR / 'config.sh'))
SYNC_CONFIG = PROJECT_DIR / 'sync_config.json'
SYNC_LOG = PROJECT_DIR / 'sync_log.json'
SYNC_PREVIEW = PROJECT_DIR / 'sync_preview.json'
SYNC_TRASH = PROJECT_DIR / 'sync_trash.json'
SYNC_LOCK = PROJECT_DIR / '.sync.lock'

IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.bmp', '.gif',
    '.tiff', '.tif', '.webp',
}
if HEIF_SUPPORTED:
    IMAGE_EXTENSIONS |= {'.heic', '.heif'}
SKIP_NAMES = {'desktop.ini', 'Thumbs.db', '.DS_Store', '.picasa.ini'}
LOG_RETENTION_DAYS = 365
FLUSH_EVERY = 25
SAMPLE_LIMIT = 500
# Refuse to trash more than this share of a folder in one run. A source that
# comes back empty or half-listed (NAS share hiccup, wrong path) must not
# send the whole frame library to the trash.
TRASH_MAX_FRACTION = 0.05
TRASH_MIN_ALLOWED = 50


def load_settings():
    """Sync settings from config.sh (same simple KEY=value parser as server.py)."""
    cfg = {}
    if CONFIG_SH.exists():
        for line in CONFIG_SH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            val = val.split('#', 1)[0].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
                val = val[1:-1]
            cfg[key.strip()] = val

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        'max_size': (_int('SYNC_MAX_WIDTH', 1920, 320, 7680),
                     _int('SYNC_MAX_HEIGHT', 1080, 240, 4320)),
        'quality': _int('SYNC_JPEG_QUALITY', 85, 1, 100),
        'trash_dir': os.path.expandvars(os.path.expanduser(
            cfg.get('SYNC_TRASH_DIR', '').strip())),
    }


def load_sync_config():
    if not SYNC_CONFIG.exists():
        return []
    with open(SYNC_CONFIG) as f:
        data = json.load(f)
    return data.get('folders', [])


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def load_sync_log():
    data = _load_json(SYNC_LOG, [])
    return data if isinstance(data, list) else []


def save_sync_log(entries):
    _save_json(SYNC_LOG, entries)


def prune_old_entries(entries):
    cutoff = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
    return [e for e in entries if e.get('timestamp', '') >= cutoff]


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def match_key(rel):
    """Key that pairs a source file with its copy: relative path, no
    extension, lower-cased (the copy is always .jpg, and exFAT is
    case-insensitive)."""
    return os.path.splitext(rel)[0].lower()


def list_images(root):
    """{match_key: (relpath, mtime, size)} for every image under root, plus a
    {extension: count} of skipped non-image files. Hidden files and folders
    are ignored. Raises OSError if root itself cannot be listed."""
    images, skipped = {}, {}
    root = str(root)
    os.listdir(root)  # surface an unreadable root as an error, not as "empty"
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for name in filenames:
            if name.startswith('.') or name in SKIP_NAMES:
                continue
            full = os.path.join(dirpath, name)
            if not is_image(name):
                ext = os.path.splitext(name)[1].lower() or '(none)'
                skipped[ext] = skipped.get(ext, 0) + 1
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root)
            images[match_key(rel)] = (rel, st.st_mtime, st.st_size)
    return images, skipped


def mount_point(path):
    p = Path(path).resolve()
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return p


def trash_root(destination, settings):
    """Trash folder for a destination. Defaults to .papaframe-trash at the
    root of the destination's filesystem, so trashing is a cheap rename and
    the trash sits outside the photo folders the slideshow scans."""
    if settings['trash_dir']:
        return Path(settings['trash_dir'])
    return mount_point(destination) / '.papaframe-trash'


def diff_pair(source, destination):
    """Compare one folder pair. Returns a dict describing the differences;
    on error the dict has an 'error' key and nothing else is reliable."""
    result = {'source': str(source), 'destination': str(destination)}
    if not Path(source).is_dir():
        result['error'] = f'source folder not found: {source}'
        return result
    try:
        src, skipped = list_images(source)
    except OSError as e:
        result['error'] = f'cannot read source: {e}'
        return result
    if not src:
        result['error'] = 'source folder has no photos (is the NAS mounted?)'
        return result
    try:
        dst, _ = list_images(destination) if Path(destination).is_dir() else ({}, {})
    except OSError as e:
        result['error'] = f'cannot read destination: {e}'
        return result

    both = src.keys() & dst.keys()
    # 0-byte copies are left-overs from failed syncs: copy them again.
    empty = {k for k in both if dst[k][2] == 0 and src[k][2] > 0}
    to_copy = sorted(src[k][0] for k in (src.keys() - dst.keys()) | empty)
    orphans = sorted(dst[k][0] for k in dst.keys() - src.keys())
    retime = [(dst[k][0], src[k][1]) for k in both - empty
              if abs(dst[k][1] - src[k][1]) > 2]
    unsupported = {e: n for e, n in skipped.items() if e in ('.heic', '.heif')}

    limit = max(TRASH_MIN_ALLOWED, int(len(dst) * TRASH_MAX_FRACTION))
    result.update({
        'source_photos': len(src),
        'frame_photos': len(dst),
        'to_copy': len(to_copy),
        'to_copy_bytes': sum(src[match_key(r)][2] for r in to_copy),
        'to_copy_list': to_copy,
        'empty_on_frame': len(empty),
        'empty_list': sorted(dst[k][0] for k in empty),
        'to_trash': len(orphans),
        'to_trash_list': orphans,
        'trash_blocked': len(orphans) > limit,
        'trash_limit': limit,
        'timestamps_to_fix': len(retime),
        'retime_list': retime,
        'skipped_by_ext': dict(sorted(skipped.items(), key=lambda x: -x[1])),
        'unsupported_heic': sum(unsupported.values()),
    })
    return result


def resize_copy(src_file, dst_jpg, settings):
    """Write a resized JPEG of src_file to dst_jpg. Returns (old, new) size
    strings. Keeps EXIF (date taken, GPS); orientation is baked into the
    pixels so the copy displays upright everywhere."""
    with Image.open(src_file) as img:
        original_size = f'{img.width}x{img.height}'
        img = ImageOps.exif_transpose(img)
        exif = img.getexif()
        if 0x0112 in exif:          # Orientation — pixels are already upright
            exif[0x0112] = 1
        img.thumbnail(settings['max_size'], Image.LANCZOS)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        tmp = dst_jpg.with_name(dst_jpg.name + '.part')
        img.save(tmp, 'JPEG', quality=settings['quality'], optimize=True,
                 exif=exif.tobytes())
        tmp.replace(dst_jpg)
        return original_size, f'{img.width}x{img.height}'


def _unique_path(path):
    if not path.exists():
        return path
    for n in range(1, 1000):
        cand = path.with_name(f'{path.stem}.{n}{path.suffix}')
        if not cand.exists():
            return cand
    raise OSError(f'no free name for {path}')


def apply_pair(diff, settings, log_entries, trash_entries):
    """Carry out one pair's differences. Returns (copied, trashed, retimed)."""
    source = Path(diff['source'])
    destination = Path(diff['destination'])
    folder = source.name
    copied = trashed = retimed = pending = 0

    def flush(force=False):
        nonlocal pending
        if pending and (force or pending >= FLUSH_EVERY):
            save_sync_log(log_entries)
            pending = 0

    for rel in diff['to_copy_list']:
        src_file = source / rel
        dst_jpg = destination / Path(rel).with_suffix('.jpg')
        entry = {'timestamp': datetime.now().isoformat(), 'source': str(src_file),
                 'filename': src_file.name, 'folder': folder}
        try:
            dst_jpg.parent.mkdir(parents=True, exist_ok=True)
            mtime = src_file.stat().st_mtime
            try:
                old, new = resize_copy(src_file, dst_jpg, settings)
                out = dst_jpg
            except Exception:
                # Pillow could not decode it (malformed headers etc.) — keep
                # the original bytes so the photo still reaches the frame.
                dst_jpg.with_name(dst_jpg.name + '.part').unlink(missing_ok=True)
                out = destination / rel
                shutil.copyfile(src_file, out)
                old, new = 'unknown', 'copied as-is'
            os.utime(out, (mtime, mtime))
            copied += 1
            entry.update(destination=str(out), original_size=old, new_size=new,
                         action='synced')
            print(f'  + {rel}  ({old} -> {new})')
        except Exception as e:
            entry.update(action='error', error=str(e))
            print(f'  ERROR: {src_file}: {e}')
        log_entries.append(entry)
        pending += 1
        flush()

    # A 0-byte left-over saved under another extension (e.g. x.png when the
    # new copy is x.jpg) would shadow the good copy — remove it.
    for rel in diff.get('empty_list', []):
        stale = destination / rel
        try:
            if stale.suffix.lower() != '.jpg' and stale.stat().st_size == 0:
                stale.unlink()
        except OSError:
            pass

    for rel, mtime in diff['retime_list']:
        try:
            os.utime(destination / rel, (mtime, mtime))
            retimed += 1
        except OSError as e:
            print(f'  could not fix timestamp of {rel}: {e}')

    if diff['to_trash'] and diff['trash_blocked']:
        print(f'  NOT trashing {diff["to_trash"]} photo(s): more than the '
              f'safety limit of {diff["trash_limit"]} for this folder. Check '
              f'that the source is complete, then review with --preview.')
    elif diff['to_trash']:
        troot = trash_root(destination, settings)
        mount = mount_point(destination)
        for rel in diff['to_trash_list']:
            src_path = destination / rel
            try:
                target = _unique_path(troot / src_path.relative_to(mount))
                target.parent.mkdir(parents=True, exist_ok=True)
                size = src_path.stat().st_size
                shutil.move(str(src_path), str(target))
            except Exception as e:
                print(f'  could not trash {rel}: {e}')
                continue
            trashed += 1
            now = datetime.now().isoformat()
            trash_entries.append({
                'id': f'{now}|{src_path}', 'original': str(src_path),
                'trashed': str(target), 'size': size, 'trashed_at': now,
                'missing_from': str(source / rel),
            })
            log_entries.append({'timestamp': now, 'source': str(source / rel),
                                'destination': str(target), 'filename': src_path.name,
                                'folder': folder, 'action': 'trashed'})
            pending += 1
            print(f'  - {rel}  (no longer on source, moved to trash)')
        _prune_empty_dirs(destination, diff['to_trash_list'])
        _save_json(SYNC_TRASH, trash_entries)

    flush(force=True)
    return copied, trashed, retimed


def _prune_empty_dirs(root, rels):
    """Remove folders left empty by trashing (never root itself)."""
    root = Path(root)
    for d in sorted({(root / r).parent for r in rels}, key=lambda p: -len(p.parts)):
        while d != root and root in d.parents:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def _strip_lists(diff):
    """Preview/report form of a diff: capped sample lists, no internals."""
    out = {k: v for k, v in diff.items()
           if k not in ('to_copy_list', 'to_trash_list', 'retime_list', 'empty_list')}
    if 'error' not in diff:
        out['to_copy_list'] = diff['to_copy_list'][:SAMPLE_LIMIT]
        out['to_trash_list'] = diff['to_trash_list'][:SAMPLE_LIMIT]
    return out


def main():
    import argparse
    parser = argparse.ArgumentParser(description='PapaFrame Photo Sync')
    parser.add_argument('--preview', action='store_true',
                        help='Report differences only; change nothing')
    parser.add_argument('--approve-trash', action='append', default=[], metavar='DEST',
                        help='Allow trashing past the safety limit for this destination')
    parser.add_argument('--full', action='store_true',
                        help='Accepted for compatibility; every run compares in full')
    args = parser.parse_args()

    mode = 'Preview' if args.preview else 'Sync'
    # One run at a time: the server can be restarted while a sync it started
    # keeps running (KillMode=process), and must not start a second one.
    lock = open(SYNC_LOCK, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f'Another photo sync is already running — {mode.lower()} skipped.')
        sys.exit(0)
    print(f'PapaFrame Photo {mode} — {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'Config: {SYNC_CONFIG}   HEIC support: {"yes" if HEIF_SUPPORTED else "no"}')

    settings = load_settings()
    folders = load_sync_config()
    if not folders:
        print('No sync folders configured. Use the admin page or edit sync_config.json.')
        sys.exit(0)

    log_entries = prune_old_entries(load_sync_log())
    trash_entries = _load_json(SYNC_TRASH, [])
    report = {'generated_at': datetime.now().isoformat(), 'mode': mode.lower(),
              'heic_supported': HEIF_SUPPORTED, 'pairs': []}
    totals = [0, 0, 0]

    for pair in folders:
        src, dst = pair.get('source', ''), pair.get('destination', '')
        if not src or not dst:
            print(f'  Skipping incomplete pair: {pair}')
            continue
        print(f'\n{src}  ->  {dst}')
        diff = diff_pair(src, dst)
        if 'error' not in diff and diff['trash_blocked'] and dst in args.approve_trash:
            print(f'  trashing {diff["to_trash"]} photo(s) past the safety limit (approved)')
            diff['trash_blocked'] = False
        if 'error' in diff:
            print(f'  WARNING: {diff["error"]} — skipped')
        else:
            print(f'  {diff["source_photos"]} on source, {diff["frame_photos"]} on frame: '
                  f'{diff["to_copy"]} to copy, {diff["to_trash"]} no longer on source, '
                  f'{diff["timestamps_to_fix"]} timestamps to fix')
            if not args.preview:
                done = apply_pair(diff, settings, log_entries, trash_entries)
                diff['done'] = dict(zip(('copied', 'trashed', 'retimed'), done))
                totals = [a + b for a, b in zip(totals, done)]
                print(f'  {done[0]} copied, {done[1]} trashed, {done[2]} timestamps fixed')
        report['pairs'].append(_strip_lists(diff))

    report['finished_at'] = datetime.now().isoformat()
    if args.preview:
        _save_json(SYNC_PREVIEW, report)
        print(f'\nPreview written to {SYNC_PREVIEW}')
    else:
        save_sync_log(log_entries)
        report['totals'] = dict(zip(('copied', 'trashed', 'retimed'), totals))
        _save_json(PROJECT_DIR / 'sync_last_run.json', report)
        print(f'\nDone. {totals[0]} copied, {totals[1]} trashed, '
              f'{totals[2]} timestamps fixed. Log has {len(log_entries)} entries.')


if __name__ == '__main__':
    main()
