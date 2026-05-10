#!/usr/bin/env python3
"""
PapaFrame Server
Controls and monitors a Raspberry Pi slideshow display.
Integrates with start_frame.sh via control files in /tmp.
"""

import json
import os
import re
import socket
import psutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image
from PIL.ExifTags import TAGS
import logging

# Optional offline reverse geocoder — the feature is disabled if missing.
try:
    import reverse_geocoder as _rg
    _RG_AVAILABLE = True
except Exception:
    _rg = None
    _RG_AVAILABLE = False

# Optional ISO country name lookup.
try:
    import pycountry as _pycountry
except Exception:
    _pycountry = None

# ── Config loader ─────────────────────────────────────────────────
# Parses a shell-style KEY="value" config file shared with start_frame.sh.
def load_config(path):
    cfg = {}
    if not path.exists():
        return cfg
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, val = line.partition('=')
        val = val.split('#', 1)[0].strip()
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        cfg[key.strip()] = val
    return cfg

CONFIG_PATH = Path(os.environ.get('PAPAFRAME_CONFIG',
                                  Path(__file__).parent / 'config.sh'))
CONFIG = load_config(CONFIG_PATH)

# Schema of keys editable from the admin page. Order controls form layout.
# Each entry: (key, type, label, help text)
CONFIG_SCHEMA = [
    ('PHOTO_DIRS',        'str', 'Photo folders',
     'Colon-separated list of folders to scan recursively for photos.'),
    ('DEFAULT_DURATION',  'int', 'Default duration (seconds)',
     'Seconds each photo is shown when no override is set.'),
    ('RESHUFFLE_INTERVAL', 'int', 'Reshuffle interval (seconds)',
     'How often the slideshow list is reshuffled in the background.'),
    ('FORCE_VIEWER',      'str', 'Image viewer',
     'auto (recommended), fbi (framebuffer), feh, eog, or display.'),
    ('FBI_VT',            'int', 'Virtual terminal for fbi',
     'Usually 1-7; auto-detection finds the best available.'),
    ('SERVER_HOST',       'str', 'Server bind address',
     '0.0.0.0 listens on every interface; 127.0.0.1 is local only.'),
    ('SERVER_PORT',       'int', 'Server port',
     'TCP port the web UI listens on.'),
    ('SOURCE_FILE',       'str', 'Master photo list path',
     'File that stores the full list of photos (rebuilt if missing).'),
    ('FRAME_SCRIPT',      'str', 'Slideshow launcher script',
     'Path to start_frame.sh — the bash script that runs the viewer.'),
    ('LOG_FILE',          'str', 'Server log file',
     'Log output path (relative to server.py if not absolute).'),
]

def write_config(path, updates):
    """Rewrite config.sh in place, replacing only the values for known keys.
    Preserves comments, blank lines, and unknown keys."""
    if not path.exists():
        raise FileNotFoundError(f'config file not found: {path}')
    lines = path.read_text().splitlines()
    key_re = re.compile(r'^(?P<key>[A-Z_][A-Z0-9_]*)=')
    out = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith('#') or '=' not in stripped:
            out.append(line)
            continue
        m = key_re.match(stripped)
        if not m or m.group('key') not in updates:
            out.append(line)
            continue
        key = m.group('key')
        val = updates[key]
        # Quote strings; leave pure digits bare.
        if isinstance(val, int) or (isinstance(val, str) and val.isdigit()):
            out.append(f'{key}={val}')
        else:
            safe = str(val).replace('"', '\\"')
            out.append(f'{key}="{safe}"')
    path.write_text('\n'.join(out) + '\n')

def _cfg(key, default):
    return CONFIG.get(key, default)

def _cfg_int(key, default):
    try:
        return int(CONFIG.get(key, default))
    except (TypeError, ValueError):
        return default

REPO_ROOT = Path(__file__).resolve().parent

def _cfg_path(key, default):
    """Read a path from config, expand ~/$VARS, and resolve relative paths
    against the repo root (the dir holding server.py)."""
    raw = os.path.expandvars(os.path.expanduser(CONFIG.get(key, default)))
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p

# ── Environment Detection ──────────────────────────────────────────
def detect_display_environment():
    """Detect the current display environment.
    Returns: 'x11', 'wayland', or 'console'"""
    if os.environ.get('DISPLAY') and os.environ.get('XAUTHORITY'):
        return 'x11'
    if os.environ.get('WAYLAND_DISPLAY'):
        return 'wayland'
    if Path('/dev/tty1').exists():
        return 'console'
    return 'unknown'

def check_framebuffer_available():
    """Check if framebuffer devices are accessible."""
    for fb_device in ['/dev/fb0', '/dev/fb1', '/dev/fb2']:
        try:
            if Path(fb_device).exists() and os.access(fb_device, os.W_OK):
                return True
        except Exception:
            pass
    return False

def detect_available_viewers():
    """Detect which image viewers are available on the system."""
    viewers = []
    for viewer in ['fbi', 'feh', 'eog', 'display']:
        try:
            result = subprocess.run(['which', viewer], 
                                  capture_output=True, 
                                  timeout=2)
            if result.returncode == 0:
                viewers.append(viewer)
        except Exception:
            pass
    return viewers

def get_environment_info():
    """Get complete environment detection information."""
    display_env = detect_display_environment()
    fb_available = check_framebuffer_available()
    available_viewers = detect_available_viewers()
    
    return {
        'display_environment': display_env,
        'framebuffer_available': fb_available,
        'available_viewers': available_viewers,
        'forced_viewer': _cfg('FORCE_VIEWER', 'auto'),
        'vt_number': _cfg_int('FBI_VT', 1),
        'is_root': os.geteuid() == 0 if hasattr(os, 'geteuid') else False,
    }

# ── Setup ──────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='static', static_url_path='')
logging.basicConfig(filename=_cfg('LOG_FILE', 'frame_display.log'),
                    level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── User-tunable settings (from config.sh) ────────────────────────
PHOTOS_DIRS     = [Path(os.path.expandvars(os.path.expanduser(p)))
                   for p in _cfg('PHOTO_DIRS', '$HOME/Pictures').split(':') if p]
DEFAULT_DURATION = _cfg_int('DEFAULT_DURATION', 5)
SERVER_HOST     = _cfg('SERVER_HOST', '0.0.0.0')
SERVER_PORT     = _cfg_int('SERVER_PORT', 8000)
SOURCE_FILE     = _cfg_path('SOURCE_FILE', 'photo_list.txt')
FRAME_SCRIPT    = _cfg_path('FRAME_SCRIPT', 'scripts/start_frame.sh')

# ── Internal IPC files (shared with start_frame.sh) ───────────────
LIVE_LIST       = Path('/tmp/current_slideshow.txt')
DURATION_FILE   = Path('/tmp/frame_duration_override.txt')
STOP_FLAG       = Path('/tmp/frame_stop_requested')
RESUME_FILE     = Path('/tmp/frame_resume_photo.txt')
YEAR_FILTER     = Path('/tmp/frame_year_filter.txt')
LOCATION_FILTER = Path('/tmp/frame_location_filter.txt')
FILTERED_LIST   = Path('/tmp/frame_filtered_list.txt')
SLIDESHOW_STATE = Path('/tmp/frame_slideshow_state.json')

# Persistent location cache: one line per photo, "<path>\t<cc>".
# "cc" is an ISO-3166-1 alpha-2 country code, or "-" for no GPS / unknown.
LOCATION_CACHE = Path(SOURCE_FILE.parent / 'location_cache.tsv')

# Sentinel country code used for photos with no GPS info.
NO_LOC = '-'

# Persisted schedule config — survives server restarts.
SCHEDULE_CONFIG = Path(SOURCE_FILE.parent / 'schedule.json')

# In-memory caches (rebuilt in background)
state = {
    'year_index': {},
    'year_index_ready': False,
    'location_counts': {},       # cc -> count
    'location_paths':  {},       # cc -> list[path]
    'location_ready':  False,
    'schedule_enabled': True,     # Daily schedule for screen on/off
    'screen_off_time': (23, 59),  # (hour, minute) — 11:59 PM
    'screen_on_time': (6, 0),     # (hour, minute) — 6:00 AM
    'slideshow_paused_by_schedule': False,  # True while inside a scheduled off-window
    'screen_scheduled_off': False,           # True while scheduler holds the screen off
    'slideshow_was_running_before_schedule': False,  # snapshot taken at pause time
    'last_scheduled_state': None,            # 'on' | 'off' | None — last applied transition
}

# ── Control-file helpers ──────────────────────────────────────────
def _read_file(path, default=''):
    """Read a small control file, returning default on any error."""
    try:
        return path.read_text().strip() if path.exists() else default
    except Exception:
        return default

def _write_file(path, content):
    """Write a small control file."""
    try:
        path.write_text(str(content))
    except Exception as e:
        logger.error(f'Failed to write {path}: {e}')

VIEWER_NAMES = ('fbi', 'feh', 'eog', 'display')

def get_viewer_pid():
    """Find the running image-viewer process (started by start_frame.sh).
    start_frame.sh picks one of fbi/feh/eog/display based on the detected
    environment, so we match any of them."""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] in VIEWER_NAMES:
                return proc.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

def get_frame_script_pid():
    """Find the running start_frame.sh process."""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline') or []
            if any('start_frame.sh' in arg for arg in cmdline):
                return proc.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

def is_running():
    """Check if the slideshow is currently running."""
    return get_viewer_pid() is not None

def get_current_duration():
    """Read the active duration from the control file, default from config."""
    val = _read_file(DURATION_FILE, str(DEFAULT_DURATION))
    try:
        return int(val)
    except ValueError:
        return DEFAULT_DURATION

def get_slideshow_state():
    """Read the slideshow state JSON written by start_frame.sh."""
    try:
        if SLIDESHOW_STATE.exists():
            return json.loads(SLIDESHOW_STATE.read_text())
    except Exception:
        pass
    return {}

def get_current_year_filter():
    """Read the active year filter from the control file."""
    val = _read_file(YEAR_FILTER)
    if val and val.isdigit() and len(val) == 4:
        return int(val)
    return None

def get_current_location_filter():
    """Read the active location filter (country code or NO_LOC) or None."""
    val = _read_file(LOCATION_FILTER)
    return val or None

# ── Photo list helpers ────────────────────────────────────────────
def get_photos():
    """Get the full photo list from the source file."""
    try:
        if SOURCE_FILE.exists():
            return [l for l in SOURCE_FILE.read_text().splitlines() if l.strip()]
    except Exception as e:
        logger.error(f'Error reading source file: {e}')
    return []

def get_live_photos():
    """Get the current shuffled slideshow list."""
    try:
        if LIVE_LIST.exists():
            return [l for l in LIVE_LIST.read_text().splitlines() if l.strip()]
    except Exception as e:
        logger.error(f'Error reading live list: {e}')
    return []

def get_photo_year(path):
    """Extract year from photo EXIF data or fallback to file mtime."""
    try:
        with Image.open(path) as img:
            exif = img._getexif() or {}
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTime':
                    return int(value[:4])
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).year
    except Exception:
        return datetime.now().year

def get_photo_exif(path):
    """Extract EXIF data from photo."""
    try:
        with Image.open(path) as img:
            exif = img._getexif() or {}
            data = {}
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTime':
                    data['date_taken'] = str(value)
                elif tag == 'Model':
                    data['camera'] = str(value).rstrip('\x00')[:50]
                elif tag == 'ExposureTime':
                    data['shutter'] = f"1/{int(1/float(value))}" if float(value) < 1 else f"{float(value):.1f}s"
                elif tag == 'FNumber':
                    data['aperture'] = f"{float(value):.1f}"
                elif tag == 'ISOSpeedRatings':
                    data['iso'] = int(value[0]) if isinstance(value, tuple) else int(value)
                elif tag == 'GPSInfo':
                    try:
                        gps = value
                        if isinstance(gps, dict) and 2 in gps and 4 in gps:
                            lat = float(gps[2][0]) + float(gps[2][1])/60 + float(gps[2][2])/3600
                            lon = float(gps[4][0]) + float(gps[4][1])/60 + float(gps[4][2])/3600
                            if gps.get(1) == 'S': lat = -lat
                            if gps.get(3) == 'W': lon = -lon
                            data['gps'] = {'lat': lat, 'lon': lon}
                    except Exception:
                        pass
            return data
    except Exception:
        return {}

YEAR_RE = re.compile(r'/((?:19|20)\d{2})/')

def build_year_index():
    """Build year index by extracting years from folder paths in photo_list.txt.
    This matches how start_frame.sh filters: grep '/$year/' photo_list.txt."""
    def _build():
        state['year_index_ready'] = False
        index = {}
        photos = get_photos()
        for p in photos:
            m = YEAR_RE.search(p)
            if m:
                year = int(m.group(1))
                index[year] = index.get(year, 0) + 1
        state['year_index'] = index
        state['year_index_ready'] = True
        logger.info(f'Year index built: {len(index)} years from {len(photos)} photos')
    t = threading.Thread(target=_build, daemon=True)
    t.start()

# ── Location index ────────────────────────────────────────────────
# Caches a country code per photo so we can filter the slideshow by country.
# Reading GPS EXIF for ~500k photos takes hours, so results are persisted
# to LOCATION_CACHE and only new photos are scanned on rebuild.

def _get_photo_gps(path):
    """Return (lat, lon) tuple from EXIF, or None if absent/unreadable."""
    try:
        with Image.open(path) as img:
            exif = img._getexif() or {}
            for tag_id, value in exif.items():
                if TAGS.get(tag_id) != 'GPSInfo':
                    continue
                gps = value
                if not (isinstance(gps, dict) and 2 in gps and 4 in gps):
                    return None
                lat = float(gps[2][0]) + float(gps[2][1])/60 + float(gps[2][2])/3600
                lon = float(gps[4][0]) + float(gps[4][1])/60 + float(gps[4][2])/3600
                if gps.get(1) == 'S': lat = -lat
                if gps.get(3) == 'W': lon = -lon
                return (lat, lon)
    except Exception:
        return None
    return None

def _load_location_cache():
    """Read the persisted path→cc map. Missing file = empty cache."""
    cache = {}
    if not LOCATION_CACHE.exists():
        return cache
    try:
        with LOCATION_CACHE.open('r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line:
                    continue
                path, _, cc = line.rpartition('\t')
                if path and cc:
                    cache[path] = cc
    except Exception as e:
        logger.error(f'Failed to load location cache: {e}')
    return cache

def _save_location_cache(cache):
    """Write the path→cc map atomically."""
    try:
        tmp = LOCATION_CACHE.with_suffix('.tsv.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            for path, cc in cache.items():
                f.write(f'{path}\t{cc}\n')
        tmp.replace(LOCATION_CACHE)
    except Exception as e:
        logger.error(f'Failed to save location cache: {e}')

def country_name(cc):
    """Best-effort ISO alpha-2 → human name, falling back to the code."""
    if cc == NO_LOC:
        return 'No Location'
    if _pycountry:
        try:
            c = _pycountry.countries.get(alpha_2=cc)
            if c:
                return c.name
        except Exception:
            pass
    return cc

def _refresh_location_state(cache):
    """Rebuild in-memory buckets from the path→cc cache."""
    counts = {}
    paths_by_cc = {}
    for p, cc in cache.items():
        counts[cc] = counts.get(cc, 0) + 1
        paths_by_cc.setdefault(cc, []).append(p)
    state['location_counts'] = counts
    state['location_paths']  = paths_by_cc

# Cache is flushed to disk and exposed to the UI every this many new photos,
# so a server restart mid-scan doesn't throw away progress.
LOCATION_CHUNK = 2000

def build_location_index(force=False):
    """Background build of the country→photos index.

    Uses LOCATION_CACHE for photos already classified, and only reads EXIF for
    newly-seen paths. The cache is persisted and in-memory state is refreshed
    every LOCATION_CHUNK photos, so long scans survive server restarts and
    partial results are visible in the UI while the scan continues."""
    def _build():
        state['location_ready'] = False
        if not _RG_AVAILABLE:
            logger.warning('reverse_geocoder not installed — location index disabled')
            state['location_ready'] = True
            return

        photos = get_photos()
        cache = {} if force else _load_location_cache()

        # Drop stale entries up front so buckets never show removed photos.
        photo_set = set(photos)
        cache = {p: cc for p, cc in cache.items() if p in photo_set}

        # Publish whatever we already know before scanning anything new —
        # this is what lets the UI show country buttons immediately after a
        # restart when the cache from a previous run is present.
        _refresh_location_state(cache)
        if cache:
            state['location_ready'] = True

        to_scan = [p for p in photos if p not in cache]
        logger.info(f'Location index: {len(photos)} total, '
                    f'{len(cache)} cached, {len(to_scan)} to scan')

        def flush(chunk_coords, chunk_paths):
            """Geocode this chunk, merge into the cache, persist, and publish."""
            if chunk_coords:
                try:
                    results = _rg.search(
                        chunk_coords,
                        mode=2 if len(chunk_coords) > 1 else 1,
                    )
                    for p, r in zip(chunk_paths, results):
                        cache[p] = (r.get('cc') or NO_LOC).upper()
                except Exception as e:
                    logger.error(f'reverse_geocoder failed: {e}')
                    for p in chunk_paths:
                        cache[p] = NO_LOC
            _save_location_cache(cache)
            _refresh_location_state(cache)
            state['location_ready'] = True

        chunk_coords = []
        chunk_paths  = []
        seen_since_flush = 0
        for p in to_scan:
            gps = _get_photo_gps(p)
            if gps is None:
                cache[p] = NO_LOC
            else:
                chunk_coords.append(gps)
                chunk_paths.append(p)
            seen_since_flush += 1
            if seen_since_flush >= LOCATION_CHUNK:
                flush(chunk_coords, chunk_paths)
                chunk_coords = []
                chunk_paths  = []
                seen_since_flush = 0
                logger.info(
                    f'Location index progress: '
                    f'{len(cache)}/{len(photos)} '
                    f'({100*len(cache)/max(1,len(photos)):.1f}%)'
                )

        # Final flush for the remainder.
        flush(chunk_coords, chunk_paths)
        logger.info(f'Location index complete: {len(state["location_counts"])} buckets')

    t = threading.Thread(target=_build, daemon=True)
    t.start()

# ── Daily Schedule: Screen on/off + Slideshow pause/resume ────────────
def _load_schedule_config():
    """Load persisted schedule settings into `state`. Silent on first run."""
    try:
        if not SCHEDULE_CONFIG.exists():
            return
        data = json.loads(SCHEDULE_CONFIG.read_text())
        if 'enabled' in data:
            state['schedule_enabled'] = bool(data['enabled'])
        if 'screen_off_time' in data:
            h, m = data['screen_off_time']
            state['screen_off_time'] = (int(h), int(m))
        if 'screen_on_time' in data:
            h, m = data['screen_on_time']
            state['screen_on_time'] = (int(h), int(m))
    except Exception as e:
        logger.error(f'Failed to load schedule config: {e}')

def _save_schedule_config():
    """Persist schedule settings so they survive server restarts."""
    try:
        SCHEDULE_CONFIG.write_text(json.dumps({
            'enabled': bool(state.get('schedule_enabled', True)),
            'screen_off_time': list(state.get('screen_off_time', (23, 59))),
            'screen_on_time':  list(state.get('screen_on_time',  (6,  0))),
        }))
    except Exception as e:
        logger.error(f'Failed to save schedule config: {e}')

def _should_be_off(now_hm, off_hm, on_hm):
    """True if the schedule says the screen should be off at `now_hm`.
    Handles overnight windows (off > on, e.g. 23:59 → 06:00)."""
    if off_hm == on_hm:
        return False
    if off_hm < on_hm:
        return off_hm <= now_hm < on_hm
    return now_hm >= off_hm or now_hm < on_hm

def _set_screen(want):
    """Toggle the HDMI display; logs and swallows errors so the scheduler keeps running."""
    try:
        subprocess.run(
            ['sudo', '-n', '/usr/local/bin/papaframe-screen', want],
            capture_output=True, timeout=5, check=False,
        )
    except Exception as e:
        logger.error(f'Schedule screen-{want} failed: {e}')

def _launch_frame_script():
    """Spawn start_frame.sh in a fresh session (used to resume after a scheduled pause)."""
    STOP_FLAG.unlink(missing_ok=True)
    try:
        subprocess.Popen(
            ['bash', str(FRAME_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.error(f'Schedule slideshow resume failed: {e}')

def _apply_schedule_state(desired):
    """Drive screen + slideshow to match `desired` ('on' or 'off'). Idempotent;
    only the bits that need to change are touched. STOP_FLAG fully exits
    start_frame.sh, so resume must relaunch it — we do, but only if the
    slideshow was actually running when the off-window began."""
    if desired == 'off':
        running_now = is_running() or get_frame_script_pid() is not None
        # Only snapshot on the *transition* into the off-window, not on
        # repeated ticks within it (the slideshow will already be stopped).
        if not state.get('screen_scheduled_off', False):
            state['slideshow_was_running_before_schedule'] = running_now
        if running_now:
            _write_file(STOP_FLAG, '1')
        _set_screen('off')
        state['screen_scheduled_off'] = True
        state['slideshow_paused_by_schedule'] = True
    else:
        _set_screen('on')
        if (state.get('slideshow_paused_by_schedule', False)
                and state.get('slideshow_was_running_before_schedule', False)
                and not get_frame_script_pid()):
            _launch_frame_script()
        state['screen_scheduled_off'] = False
        state['slideshow_paused_by_schedule'] = False
        state['slideshow_was_running_before_schedule'] = False

def _run_scheduler():
    """Background scheduler that drives the screen and slideshow to match the
    daily schedule. Recomputes the desired state each tick (rather than
    edge-triggering on a specific minute) so a missed minute doesn't skip the
    day, and so toggling/reconfiguring the schedule applies immediately."""
    while True:
        try:
            if state.get('schedule_enabled', True):
                now = datetime.now()
                now_hm = (now.hour, now.minute)
                off_hm = state.get('screen_off_time', (23, 59))
                on_hm  = state.get('screen_on_time',  (6,  0))
                desired = 'off' if _should_be_off(now_hm, off_hm, on_hm) else 'on'
                if desired != state.get('last_scheduled_state'):
                    logger.info(f'Schedule transition: -> {desired} '
                                f'(off={off_hm[0]:02d}:{off_hm[1]:02d}, '
                                f'on={on_hm[0]:02d}:{on_hm[1]:02d})')
                    _apply_schedule_state(desired)
                    state['last_scheduled_state'] = desired
        except Exception as e:
            logger.error(f'Scheduler error: {e}')
        time.sleep(30)  # Tick every 30s — minute-accurate without thrashing.

def _start_scheduler():
    """Start the background scheduler thread."""
    _load_schedule_config()
    t = threading.Thread(target=_run_scheduler, daemon=True)
    t.start()
    off = state['screen_off_time']
    on  = state['screen_on_time']
    logger.info(f'Daily scheduler started (off {off[0]:02d}:{off[1]:02d}, '
                f'on {on[0]:02d}:{on[1]:02d}, enabled={state["schedule_enabled"]})')

def _write_filtered_list(cc):
    """Write the location-filtered photo list to FILTERED_LIST."""
    paths = state['location_paths'].get(cc, [])
    FILTERED_LIST.write_text('\n'.join(paths) + ('\n' if paths else ''))
    return len(paths)

# ── Serve static files ─────────────────────────────────────────────
@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

@app.route('/admin')
def serve_admin():
    return send_from_directory('static', 'admin.html')

# ── API: Config (admin page) ───────────────────────────────────────
@app.route('/api/config', methods=['GET'])
def api_config_get():
    """Return the current config values and schema for the admin page."""
    cfg = load_config(CONFIG_PATH)
    fields = []
    for key, typ, label, help_text in CONFIG_SCHEMA:
        fields.append({
            'key': key,
            'type': typ,
            'label': label,
            'help': help_text,
            'value': cfg.get(key, ''),
        })
    return jsonify({
        'path': str(CONFIG_PATH),
        'fields': fields,
    })

@app.route('/api/config', methods=['POST'])
def api_config_set():
    """Update values in config.sh. Most changes need a server restart."""
    data = request.json or {}
    updates = {}
    for key, typ, _label, _help in CONFIG_SCHEMA:
        if key not in data:
            continue
        raw = data[key]
        if typ == 'int':
            try:
                updates[key] = int(raw)
            except (TypeError, ValueError):
                return jsonify({'error': f'{key} must be an integer'}), 400
        else:
            updates[key] = str(raw)
    if not updates:
        return jsonify({'error': 'no known keys provided'}), 400
    try:
        write_config(CONFIG_PATH, updates)
    except Exception as e:
        logger.error(f'Failed to write config: {e}')
        return jsonify({'error': str(e)}), 500
    logger.info(f'Config updated: {list(updates.keys())}')
    return jsonify({'success': True, 'updated': list(updates.keys())})

@app.route('/api/config/rebuild', methods=['POST'])
def api_config_rebuild():
    """Delete the master photo list so start_frame.sh rebuilds it from
    PHOTO_DIRS on the next slideshow start."""
    try:
        SOURCE_FILE.unlink(missing_ok=True)
        logger.info(f'Deleted {SOURCE_FILE} for rebuild')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Status ────────────────────────────────────────────────────
@app.route('/api/status', methods=['GET'])
def api_status():
    """Get slideshow status by checking the viewer process and control files."""
    viewer_pid = get_viewer_pid()
    running = viewer_pid is not None
    duration = get_current_duration()
    ss = get_slideshow_state()
    env_info = get_environment_info()

    return jsonify({
        'running': running,
        'pid': viewer_pid,
        'duration': duration if running else None,
        'started_at': ss.get('started_at'),
        'viewer': ss.get('viewer'),
        'environment': env_info,
        'paused_by_schedule': bool(state.get('slideshow_paused_by_schedule', False)),
        'screen_scheduled_off': bool(state.get('screen_scheduled_off', False)),
        'schedule_enabled': bool(state.get('schedule_enabled', True)),
    })

@app.route('/api/hostname', methods=['GET'])
def api_hostname():
    """Get the system hostname."""
    return jsonify({
        'hostname': socket.gethostname(),
    })

@app.route('/api/environment', methods=['GET'])
def api_environment():
    """Get detailed environment detection information for display configuration."""
    env_info = get_environment_info()
    return jsonify({
        'environment': env_info,
        'timestamp': datetime.now().isoformat(),
    })

@app.route('/api/start', methods=['POST'])
def api_start():
    """Start the slideshow by launching start_frame.sh."""
    if is_running():
        return jsonify({'error': 'Slideshow already running'}), 400

    data = request.json or {}
    duration = data.get('duration', DEFAULT_DURATION)

    # Write duration before starting
    _write_file(DURATION_FILE, duration)
    # Clear any leftover stop flag
    STOP_FLAG.unlink(missing_ok=True)

    # Check if the bash script is already running
    if get_frame_script_pid():
        logger.info('start_frame.sh already running, viewer should restart on its own')
        return jsonify({'success': True})

    try:
        subprocess.Popen(
            ['bash', str(FRAME_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info(f'Launched start_frame.sh with duration {duration}s')
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f'Failed to launch start_frame.sh: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop slideshow by writing the stop flag (start_frame.sh monitors it)."""
    if not is_running() and not get_frame_script_pid():
        return jsonify({'error': 'Slideshow not running'}), 400

    _write_file(STOP_FLAG, '1')
    logger.info('Stop flag written')
    return jsonify({'success': True})

@app.route('/api/restart', methods=['POST'])
def api_restart():
    """Restart slideshow: stop, then start."""
    data = request.json or {}
    duration = data.get('duration', DEFAULT_DURATION)

    # Write duration for the new session
    _write_file(DURATION_FILE, duration)

    # Kill the viewer directly so the bash loop restarts it with new settings
    viewer_pid = get_viewer_pid()
    if viewer_pid:
        try:
            viewer_proc = psutil.Process(viewer_pid)
            viewer_name = viewer_proc.name()
            viewer_proc.terminate()
            logger.info(f'Killed {viewer_name} PID {viewer_pid} for restart')
        except Exception as e:
            logger.error(f'Failed to kill viewer: {e}')

    # If the bash script isn't running, launch it
    if not get_frame_script_pid():
        STOP_FLAG.unlink(missing_ok=True)
        try:
            subprocess.Popen(
                ['bash', str(FRAME_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info('Launched start_frame.sh for restart')
        except Exception as e:
            logger.error(f'Failed to launch start_frame.sh: {e}')
            return jsonify({'error': str(e)}), 500

    return jsonify({'success': True})

@app.route('/api/setduration', methods=['POST'])
def api_setduration():
    """Change duration via control file. The bash script detects the change
    and kills/restarts fbi automatically."""
    data = request.json or {}
    new_dur = data.get('duration', 5)
    _write_file(DURATION_FILE, new_dur)
    logger.info(f'Duration set to {new_dur}s')
    return jsonify({'success': True})

# ── API: Photo info ────────────────────────────────────────────────
@app.route('/api/photoinfo', methods=['GET'])
def api_photoinfo():
    """Get photo library info from the source list."""
    photos = get_photos()
    jpg_count = sum(1 for p in photos if p.lower().endswith(('.jpg', '.jpeg')))
    png_count = sum(1 for p in photos if p.lower().endswith('.png'))
    other_count = len(photos) - jpg_count - png_count

    # Get year range from the index if available, otherwise skip
    years = list(state['year_index'].keys()) if state['year_index_ready'] else []
    return jsonify({
        'count': len(photos),
        'year_min': min(years) if years else None,
        'year_max': max(years) if years else None,
        'jpg': jpg_count,
        'png': png_count,
        'other': other_count,
    })

@app.route('/api/photo/thumb', methods=['GET'])
def api_photo_thumb():
    """Get photo thumbnail."""
    path = request.args.get('path')
    if not path:
        return jsonify({'error': 'Invalid path'}), 400
    resolved = Path(path).resolve()
    if not any(resolved.is_relative_to(d) for d in PHOTOS_DIRS):
        return jsonify({'error': 'Invalid path'}), 400

    try:
        with Image.open(path) as img:
            img.thumbnail((200, 200))
            img_path = f'/tmp/thumb_{hash(path)}.jpg'
            img.save(img_path, 'JPEG')
            return send_from_directory('/tmp', f'thumb_{hash(path)}.jpg')
    except Exception as e:
        logger.error(f'Thumbnail error: {e}')
        return jsonify({'error': str(e)}), 400

@app.route('/api/currentphoto', methods=['GET'])
def api_currentphoto():
    """Get current, previous, and next photo based on elapsed time since fbi started.
    fbi advances through the filelist at a fixed interval, so:
        index = floor((now - started_at) / duration) % total"""
    photos = get_live_photos()
    if not photos:
        return jsonify({
            'current': None,
            'previous': None,
            'next': None,
            'index': 0,
            'total': 0,
            'error': 'No photos',
        })

    # Calculate current position from elapsed time
    ss = get_slideshow_state()
    started_at = ss.get('started_at', 0)
    duration = ss.get('duration', DEFAULT_DURATION)
    total = len(photos)

    if started_at and duration and is_running():
        elapsed = time.time() - started_at
        idx = int(elapsed / duration) % total
    else:
        idx = 0

    def photo_details(path):
        try:
            with Image.open(path) as img:
                return {
                    'path': path,
                    'filename': Path(path).name,
                    'album': Path(path).parent.name,
                    **get_photo_exif(path),
                    'size_bytes': Path(path).stat().st_size,
                    'dimensions': {'w': img.width, 'h': img.height},
                }
        except Exception as e:
            logger.error(f'Error reading photo {path}: {e}')
            return {'path': path, 'error': str(e)}

    prev_idx = (idx - 1) % total
    next_idx = (idx + 1) % total

    return jsonify({
        'current': photo_details(photos[idx]),
        'previous': photo_details(photos[prev_idx]) if total > 1 else None,
        'next': photo_details(photos[next_idx]) if total > 1 else None,
        'index': idx,
        'total': total,
    })

# ── API: Year filter ───────────────────────────────────────────────
@app.route('/api/years', methods=['GET'])
def api_years():
    """Get year filter info."""
    years = sorted(
        [{'year': y, 'count': c} for y, c in state['year_index'].items()],
        key=lambda x: x['year'], reverse=True,
    )
    total = sum(x['count'] for x in years)
    return jsonify({
        'years': years,
        'total': total,
        'active': get_current_year_filter(),
        'ready': state['year_index_ready'],
    })

@app.route('/api/setfilter', methods=['POST'])
def api_setfilter():
    """Set year filter via control file. The bash script reads it on next reshuffle,
    and we kill fbi so the bash loop reshuffles immediately."""
    data = request.json or {}
    year = data.get('year')

    if year:
        _write_file(YEAR_FILTER, year)
    else:
        YEAR_FILTER.unlink(missing_ok=True)

    # Year and location filters are mutually exclusive.
    LOCATION_FILTER.unlink(missing_ok=True)
    FILTERED_LIST.unlink(missing_ok=True)

    # Kill fbi so the bash script reshuffles with the new filter
    viewer_pid = get_viewer_pid()
    if viewer_pid:
        try:
            psutil.Process(viewer_pid).terminate()
        except Exception:
            pass

    logger.info(f'Year filter set to {year}')
    return jsonify({'success': True})

@app.route('/api/rebuildyears', methods=['POST'])
def api_rebuildyears():
    """Rebuild year index."""
    build_year_index()
    return jsonify({'success': True})

# ── API: Location filter ──────────────────────────────────────────
@app.route('/api/locations', methods=['GET'])
def api_locations():
    """Return the country buckets for the location filter UI."""
    counts = state['location_counts']
    items = []
    total = 0
    no_loc_count = 0
    for cc, count in counts.items():
        if cc == NO_LOC:
            no_loc_count = count
            continue
        items.append({'cc': cc, 'name': country_name(cc), 'count': count})
        total += count
    items.sort(key=lambda x: x['count'], reverse=True)
    return jsonify({
        'countries':    items,
        'total':        total + no_loc_count,  # all photos regardless of GPS
        'no_location':  no_loc_count,
        'active':       get_current_location_filter(),
        'ready':        state['location_ready'],
        'available':    _RG_AVAILABLE,
    })

@app.route('/api/setlocationfilter', methods=['POST'])
def api_setlocationfilter():
    """Set (or clear) the location filter and reshuffle."""
    data = request.json or {}
    cc = data.get('cc')  # country code, NO_LOC, or None/"" for all

    if cc:
        if not state['location_ready']:
            return jsonify({'error': 'Location index not ready yet'}), 400
        if cc not in state['location_paths']:
            return jsonify({'error': f'Unknown location: {cc}'}), 400
        count = _write_filtered_list(cc)
        _write_file(LOCATION_FILTER, cc)
        logger.info(f'Location filter set to {cc} ({count} photos)')
    else:
        LOCATION_FILTER.unlink(missing_ok=True)
        FILTERED_LIST.unlink(missing_ok=True)
        logger.info('Location filter cleared')

    # Location and year filters are mutually exclusive.
    YEAR_FILTER.unlink(missing_ok=True)

    # Kill fbi so start_frame.sh reshuffles with the new filter.
    viewer_pid = get_viewer_pid()
    if viewer_pid:
        try:
            psutil.Process(viewer_pid).terminate()
        except Exception:
            pass

    return jsonify({'success': True})

@app.route('/api/rebuildlocations', methods=['POST'])
def api_rebuildlocations():
    """Rebuild location index (incremental by default)."""
    data = request.json or {}
    build_location_index(force=bool(data.get('force')))
    return jsonify({'success': True})

# ── API: Screen power ─────────────────────────────────────────────
@app.route('/api/screen', methods=['POST'])
def api_screen():
    """Blank or wake the HDMI display by toggling the DRM connector status.
    We run headless on the framebuffer, so no X tools (xset DPMS) are available;
    the root-only write is delegated to /usr/local/bin/papaframe-screen."""
    data = request.json or {}
    want = (data.get('state') or '').lower()
    if want not in ('on', 'off'):
        return jsonify({'error': "state must be 'on' or 'off'"}), 400

    try:
        res = subprocess.run(
            ['sudo', '-n', '/usr/local/bin/papaframe-screen', want],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or 'papaframe-screen failed')
    except Exception as e:
        logger.error(f'Screen control failed: {e}')
        return jsonify({'error': str(e)}), 500

    logger.info(f'Screen set to {want}')
    return jsonify({'success': True, 'state': want})

# ── API: Daily Schedule ────────────────────────────────────────────
@app.route('/api/schedule/status', methods=['GET'])
def api_schedule_status():
    """Get the current schedule configuration."""
    screen_off = state.get('screen_off_time', (23, 59))
    screen_on = state.get('screen_on_time', (6, 0))
    return jsonify({
        'enabled': state.get('schedule_enabled', True),
        'screen_off_time': f'{screen_off[0]:02d}:{screen_off[1]:02d}',
        'screen_on_time': f'{screen_on[0]:02d}:{screen_on[1]:02d}',
        'slideshow_paused_by_schedule': state.get('slideshow_paused_by_schedule', False),
    })

def _parse_hm(label, raw):
    """Parse 'HH:MM' into (hour, minute), raising ValueError on bad input."""
    parts = str(raw).strip().split(':')
    if len(parts) != 2:
        raise ValueError(f'{label} must be in HH:MM format')
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f'{label}: invalid time values')
    return (h, m)

@app.route('/api/schedule/configure', methods=['POST'])
def api_schedule_configure():
    """Update schedule times. Times should be in HH:MM format. The change takes
    effect on the next scheduler tick (within ~30s); we also force a
    re-evaluation by clearing last_scheduled_state."""
    data = request.json or {}
    try:
        if 'screen_off_time' in data:
            state['screen_off_time'] = _parse_hm('screen_off_time', data['screen_off_time'])
        if 'screen_on_time' in data:
            state['screen_on_time'] = _parse_hm('screen_on_time', data['screen_on_time'])
        # Force the scheduler to re-evaluate against the new times.
        state['last_scheduled_state'] = None
        _save_schedule_config()
        logger.info(f"Schedule updated: off at {state['screen_off_time'][0]:02d}:{state['screen_off_time'][1]:02d}, "
                   f"on at {state['screen_on_time'][0]:02d}:{state['screen_on_time'][1]:02d}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f'Schedule configure failed: {e}')
        return jsonify({'error': str(e)}), 400

@app.route('/api/schedule/enable', methods=['POST'])
def api_schedule_enable():
    """Enable the daily schedule and force an immediate re-evaluation."""
    state['schedule_enabled'] = True
    state['last_scheduled_state'] = None
    _save_schedule_config()
    logger.info('Schedule enabled')
    return jsonify({'success': True, 'enabled': True})

@app.route('/api/schedule/disable', methods=['POST'])
def api_schedule_disable():
    """Disable the daily schedule. If we're currently inside a scheduled
    off-window, restore screen+slideshow so the user isn't left stuck."""
    state['schedule_enabled'] = False
    if state.get('screen_scheduled_off', False):
        _apply_schedule_state('on')
    state['last_scheduled_state'] = None
    _save_schedule_config()
    logger.info('Schedule disabled')
    return jsonify({'success': True, 'enabled': False})

# ── API: System stats ──────────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get system stats."""
    cpu = psutil.cpu_percent(interval=0.1)
    disk = psutil.disk_usage('/')
    uptime = int(time.time() - psutil.boot_time())

    return jsonify({
        'cpu': cpu,
        'uptime': uptime,
        'storage': {
            'total': disk.total,
            'used': disk.used,
            'free': disk.free,
            'percent': disk.percent,
        },
    })

# ── API: Session GPS tracking ──────────────────────────────────────
# Cache: maps photo path -> GPS point dict (or None). Cleared on session clear.
_gps_cache = {}

def _get_current_index():
    """Calculate which photo fbi is currently showing based on elapsed time."""
    photos = get_live_photos()
    if not photos:
        return 0, photos
    ss = get_slideshow_state()
    started_at = ss.get('started_at', 0)
    duration = ss.get('duration', DEFAULT_DURATION)
    if started_at and duration and is_running():
        elapsed = time.time() - started_at
        return int(elapsed / duration) % len(photos), photos
    return 0, photos

@app.route('/api/sessionpoints', methods=['GET'])
def api_sessionpoints():
    """Get GPS points from photos already shown in the current session.
    Uses elapsed time to determine how far fbi has advanced through the list,
    then returns GPS data for photos 0..current_index."""
    idx, photos = _get_current_index()
    if not photos:
        return jsonify([])

    # Only scan photos that have already been displayed (0 to idx inclusive)
    shown = photos[:idx + 1]
    points = []
    for p in shown:
        # Check cache first
        if p in _gps_cache:
            cached = _gps_cache[p]
            if cached is not None:
                points.append(cached)
            continue
        # Read EXIF for GPS
        exif = get_photo_exif(p)
        if 'gps' in exif:
            point = {
                'lat': exif['gps']['lat'],
                'lon': exif['gps']['lon'],
                'filename': Path(p).name,
                'album': Path(p).parent.name,
                'camera': exif.get('camera'),
                'date_taken': exif.get('date_taken'),
            }
            _gps_cache[p] = point
            points.append(point)
        else:
            _gps_cache[p] = None

    # Current photo GPS (already cached from the loop above)
    current_gps = _gps_cache.get(photos[idx])

    return jsonify({'points': points, 'current': current_gps})

@app.route('/api/clearsession', methods=['POST'])
def api_clearsession():
    """Clear session — clear GPS cache and trigger a reshuffle."""
    _gps_cache.clear()
    viewer_pid = get_viewer_pid()
    if viewer_pid:
        try:
            psutil.Process(viewer_pid).terminate()
        except Exception:
            pass
    logger.info('Session cleared, GPS cache flushed, fbi killed to trigger reshuffle')
    return jsonify({'success': True})

# ── Entry point ────────────────────────────────────────────────────
if __name__ == '__main__':
    logger.info('Building year index in background...')
    build_year_index()
    logger.info('Building location index in background...')
    build_location_index()
    logger.info('Starting daily scheduler...')
    _start_scheduler()
    logger.info(f'Server starting on http://{SERVER_HOST}:{SERVER_PORT}')
    print(f'Serving http://{SERVER_HOST}:{SERVER_PORT}')
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, use_reloader=False)
