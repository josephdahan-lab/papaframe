#!/usr/bin/env python3
"""
PapaFrame Photo Sync — syncs new photos from NAS to USB with resize.

Reads folder pairs from sync_config.json, copies any image file present
in the source but missing from the destination, resizes it to fit within
1920x1080 (preserving aspect ratio, never upscaling), and saves as JPEG.
Logs every action to sync_log.json and prunes entries older than one year.
Progress is flushed to the log every 25 files so the dashboard stays current.
"""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageOps

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SYNC_CONFIG = PROJECT_DIR / 'sync_config.json'
SYNC_LOG = PROJECT_DIR / 'sync_log.json'
SYNC_MARKER = PROJECT_DIR / '.sync_last_run'

IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.bmp', '.gif',
    '.tiff', '.tif', '.webp',
}
MAX_SIZE = (1920, 1080)
JPEG_QUALITY = 85
LOG_RETENTION_DAYS = 365
FLUSH_EVERY = 25


def load_sync_config():
    if not SYNC_CONFIG.exists():
        return []
    with open(SYNC_CONFIG) as f:
        data = json.load(f)
    return data.get('folders', [])


def load_sync_log():
    if not SYNC_LOG.exists():
        return []
    try:
        with open(SYNC_LOG) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


def save_sync_log(entries):
    tmp = SYNC_LOG.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(entries, f, indent=2)
    tmp.replace(SYNC_LOG)


def prune_old_entries(entries):
    cutoff = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
    return [e for e in entries if e.get('timestamp', '') >= cutoff]


def is_image(path):
    return path.suffix.lower() in IMAGE_EXTENSIONS


def _get_last_run_time():
    if SYNC_MARKER.exists():
        return SYNC_MARKER.stat().st_mtime
    return 0


def _touch_marker():
    SYNC_MARKER.touch()


def sync_folder(source, destination, log_entries, since=0):
    source = Path(source)
    destination = Path(destination)

    if not source.exists():
        print(f'  WARNING: source not found: {source}')
        return 0
    if not source.is_dir():
        print(f'  WARNING: source is not a directory: {source}')
        return 0

    skip_names = {'desktop.ini', 'Thumbs.db', '.DS_Store', '.picasa.ini'}

    synced = 0
    skipped_old = 0
    pending = 0
    for src_file in source.rglob('*'):
        if not src_file.is_file():
            continue
        if src_file.name.startswith('.') or src_file.name in skip_names:
            continue
        if any(p.startswith('.') for p in src_file.relative_to(source).parts[:-1]):
            continue
        if not is_image(src_file):
            continue

        if since > 0:
            try:
                if src_file.stat().st_mtime < since:
                    skipped_old += 1
                    continue
            except OSError:
                continue

        rel = src_file.relative_to(source)
        dst_jpg = destination / rel.with_suffix('.jpg')
        dst_original = destination / rel

        if dst_jpg.exists() or (dst_original.exists() and dst_original != dst_jpg):
            continue

        try:
            dst_jpg.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            print(f'  SKIP (permission): {rel} — {e}')
            continue

        try:
            with Image.open(src_file) as img:
                img = ImageOps.exif_transpose(img)
                original_size = f'{img.width}x{img.height}'

                img.thumbnail(MAX_SIZE, Image.LANCZOS)

                if img.mode not in ('RGB',):
                    img = img.convert('RGB')

                img.save(dst_jpg, 'JPEG', quality=JPEG_QUALITY, optimize=True)
                new_size = f'{img.width}x{img.height}'

            synced += 1
            pending += 1
            log_entries.append({
                'timestamp': datetime.now().isoformat(),
                'source': str(src_file),
                'destination': str(dst_jpg),
                'filename': src_file.name,
                'folder': source.name,
                'original_size': original_size,
                'new_size': new_size,
                'action': 'synced',
            })
            print(f'  + {rel}  ({original_size} -> {new_size})')

        except Exception:
            dst_copy = destination / rel
            try:
                shutil.copy2(src_file, dst_copy)
                synced += 1
                pending += 1
                log_entries.append({
                    'timestamp': datetime.now().isoformat(),
                    'source': str(src_file),
                    'destination': str(dst_copy),
                    'filename': src_file.name,
                    'folder': source.name,
                    'original_size': 'unknown',
                    'new_size': 'copied as-is',
                    'action': 'synced',
                })
                print(f'  + {rel}  (copied as-is, Pillow could not decode)')
            except Exception as e2:
                print(f'  ERROR: {src_file}: {e2}')
                pending += 1
                log_entries.append({
                    'timestamp': datetime.now().isoformat(),
                    'source': str(src_file),
                    'filename': src_file.name,
                    'folder': source.name,
                    'action': 'error',
                    'error': str(e2),
                })

        if pending >= FLUSH_EVERY:
            save_sync_log(log_entries)
            pending = 0

    if pending > 0:
        save_sync_log(log_entries)

    if skipped_old:
        print(f'  ({skipped_old} older files skipped)')
    return synced


def main():
    import argparse
    parser = argparse.ArgumentParser(description='PapaFrame Photo Sync')
    parser.add_argument('--full', action='store_true',
                        help='Full scan (ignore last-run marker)')
    args = parser.parse_args()

    print(f'PapaFrame Photo Sync — {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'Config: {SYNC_CONFIG}')

    folders = load_sync_config()
    if not folders:
        print('No sync folders configured. Use the admin page or edit sync_config.json.')
        sys.exit(0)

    since = 0 if args.full else _get_last_run_time()
    if since > 0:
        print(f'Incremental: only files newer than {datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S")}')
    else:
        print('Full scan (no previous run marker)')

    log_entries = load_sync_log()
    log_entries = prune_old_entries(log_entries)

    total_synced = 0
    for pair in folders:
        src = pair.get('source', '')
        dst = pair.get('destination', '')
        if not src or not dst:
            print(f'  Skipping incomplete pair: {pair}')
            continue
        print(f'\n{src}  ->  {dst}')
        count = sync_folder(src, dst, log_entries, since=since)
        total_synced += count
        print(f'  {count} new file(s)')

    save_sync_log(log_entries)
    _touch_marker()
    print(f'\nDone. {total_synced} file(s) synced. Log has {len(log_entries)} entries.')


if __name__ == '__main__':
    main()
