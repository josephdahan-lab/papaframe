#!/usr/bin/env python3
"""
build_location_cache.py — host-side scanner that builds the photo-location
cache shared by every PapaFrame display.

Runs on the machine that owns the photo store (typically the SMB host that
the Pis mount from). Walks the photo folders with local-disk I/O, reads GPS
from EXIF, reverse-geocodes to ISO alpha-2 country codes, and writes a TSV
the Pi servers consume verbatim — saving each Pi the hours of CIFS scanning
the same data would cost over the network.

Output format (one entry per line, identical to what server.py used to write):

    <absolute photo path><TAB><country-code or "-">

The path stored is exactly what the Pis see at /mnt/plex/... — make sure the
share root on the host and the Pi mount point are the same absolute path, or
pass --strip-prefix / --add-prefix.

Typical invocation on hpenvy:

    python3 build_location_cache.py \\
        --photo-dirs /mnt/plex/Pictures \\
        --output     /mnt/plex/.papaframe/location_cache.tsv

Cron line (daily at 03:00) — incremental, so only new photos hit EXIF:

    0 3 * * * /home/joseph/papaframe/tools/build_location_cache.py \\
                --photo-dirs /mnt/plex/Pictures \\
                --output     /mnt/plex/.papaframe/location_cache.tsv \\
                >> /home/joseph/papaframe-location.log 2>&1
"""
import argparse
import os
import sys
import time
from pathlib import Path

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
except ImportError:
    sys.exit("Missing dependency: install Pillow (pip install Pillow)")

try:
    import reverse_geocoder as rg
except ImportError:
    sys.exit("Missing dependency: install reverse_geocoder (pip install reverse_geocoder)")

NO_LOC = '-'           # Sentinel for "no GPS / unreadable" — matches server.py
PHOTO_EXTS = ('.jpg', '.jpeg', '.png')
CHUNK = 5000           # Flush + log progress every N photos


def log(msg):
    print(msg, flush=True, file=sys.stderr)


def find_photos(roots):
    """Yield absolute paths of every image file under each root."""
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            log(f'  skip (not a dir): {root}')
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(PHOTO_EXTS):
                    yield os.path.join(dirpath, f)


def load_existing(path):
    """Read the previous cache. Missing/corrupt entries are silently dropped."""
    cache = {}
    if not path.exists():
        return cache
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line:
                continue
            p, _, cc = line.rpartition('\t')
            if p and cc:
                cache[p] = cc
    return cache


def read_gps(path):
    """Return (lat, lon) from EXIF, or None if absent / unreadable."""
    try:
        with Image.open(path) as img:
            exif = img._getexif() or {}
            for tag_id, value in exif.items():
                if TAGS.get(tag_id) != 'GPSInfo':
                    continue
                gps = value
                if not (isinstance(gps, dict) and 2 in gps and 4 in gps):
                    return None
                lat = float(gps[2][0]) + float(gps[2][1]) / 60 + float(gps[2][2]) / 3600
                lon = float(gps[4][0]) + float(gps[4][1]) / 60 + float(gps[4][2]) / 3600
                if gps.get(1) == 'S':
                    lat = -lat
                if gps.get(3) == 'W':
                    lon = -lon
                return (lat, lon)
    except Exception:
        return None
    return None


def write_atomic(out_path, cache):
    """Write the TSV via tmp+rename so concurrent Pi readers never see a partial file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        for p, cc in cache.items():
            f.write(f'{p}\t{cc}\n')
    tmp.replace(out_path)


def rewrite_path(p, strip, add):
    """Optional path translation (only needed if host paths differ from Pi paths)."""
    if strip and p.startswith(strip):
        p = p[len(strip):]
    if add:
        p = add + p
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--photo-dirs', required=True,
                    help='Colon-separated list of folders to scan (recursive)')
    ap.add_argument('--output', required=True,
                    help='Where to write the cache TSV (atomic via .tmp + rename)')
    ap.add_argument('--strip-prefix', default='',
                    help='Strip this prefix from each path before writing '
                         '(use when host paths differ from Pi paths)')
    ap.add_argument('--add-prefix', default='',
                    help='Prepend this prefix to each path (applied after --strip-prefix)')
    ap.add_argument('--force-rescan', action='store_true',
                    help='Ignore any existing cache and re-scan every photo')
    args = ap.parse_args()

    roots = [r for r in args.photo_dirs.split(':') if r.strip()]
    out_path = Path(args.output)
    t0 = time.time()

    log(f'PapaFrame location-cache builder')
    log(f'  photo-dirs: {roots}')
    log(f'  output:     {out_path}')
    if args.strip_prefix or args.add_prefix:
        log(f'  path rewrite: strip {args.strip_prefix!r}, add {args.add_prefix!r}')

    existing = {} if args.force_rescan else load_existing(out_path)
    log(f'  existing entries: {len(existing):,}')

    log('Walking photo folders...')
    photos = list(find_photos(roots))
    log(f'  found: {len(photos):,} image files')

    # Keep only entries whose photo still exists, drop the rest.
    photos_set = set(photos)
    existing = {p: cc for p, cc in existing.items() if p in photos_set}

    to_scan = [p for p in photos if p not in existing]
    log(f'  cached: {len(existing):,}  to-scan: {len(to_scan):,}')

    if not to_scan:
        log('Nothing new to scan — writing cache (in case prefix args changed)')
    else:
        log(f'Reading EXIF for {len(to_scan):,} new photos...')

    chunk_coords = []
    chunk_paths = []
    scanned = 0
    new_with_gps = 0
    new_no_gps = 0

    def flush_geocode():
        """Reverse-geocode this chunk's coords and merge into the cache."""
        nonlocal new_with_gps
        if not chunk_coords:
            return
        try:
            results = rg.search(chunk_coords, mode=2 if len(chunk_coords) > 1 else 1)
            for p, r in zip(chunk_paths, results):
                existing[p] = (r.get('cc') or NO_LOC).upper()
                new_with_gps += 1
        except Exception as e:
            log(f'  reverse_geocoder failed: {e} — marking chunk as no-location')
            for p in chunk_paths:
                existing[p] = NO_LOC

    for p in to_scan:
        gps = read_gps(p)
        if gps is None:
            existing[p] = NO_LOC
            new_no_gps += 1
        else:
            chunk_coords.append(gps)
            chunk_paths.append(p)
        scanned += 1
        if scanned % CHUNK == 0:
            flush_geocode()
            chunk_coords = []
            chunk_paths = []
            # Persist the cache on every chunk so a long scan survives a kill
            # and Pis pulling the file mid-scan see partial progress rather
            # than nothing. Atomic rename keeps readers from seeing torn files.
            write_atomic(out_path, existing)
            elapsed = time.time() - t0
            rate = scanned / max(0.01, elapsed)
            eta_s = (len(to_scan) - scanned) / max(0.01, rate)
            log(f'  progress: {scanned:,}/{len(to_scan):,} '
                f'({100*scanned/len(to_scan):.1f}%) '
                f'— {rate:.0f}/s, ETA {eta_s/60:.1f} min')

    flush_geocode()

    # Optional path translation just before write.
    if args.strip_prefix or args.add_prefix:
        rewritten = {}
        for p, cc in existing.items():
            rewritten[rewrite_path(p, args.strip_prefix, args.add_prefix)] = cc
        existing = rewritten

    write_atomic(out_path, existing)

    elapsed = time.time() - t0
    total_with_gps = sum(1 for cc in existing.values() if cc != NO_LOC)
    total_no_gps = len(existing) - total_with_gps
    log('Done.')
    log(f'  scanned this run: {scanned:,} '
        f'(+{new_with_gps:,} with GPS, +{new_no_gps:,} no GPS)')
    log(f'  cache total: {len(existing):,} '
        f'({total_with_gps:,} with GPS, {total_no_gps:,} no GPS)')
    log(f'  elapsed: {elapsed:.1f}s')
    log(f'  output size: {out_path.stat().st_size / (1024*1024):.1f} MB')


if __name__ == '__main__':
    main()
