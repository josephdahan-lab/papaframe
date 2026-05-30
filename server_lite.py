#!/usr/bin/env python3
"""
PapaFrame Server — Lite Edition
Lightweight web server for Pi Zero / low-resource boards.
Uses only the Python standard library (no Flask, PIL, psutil, pycountry).
Target RSS: ~18-20 MB on armv6l with 326k-photo library.
"""

import ctypes
import gc
import hashlib
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

def _cfg(key, default):
    return CONFIG.get(key, default)

def _cfg_int(key, default):
    try:
        return int(CONFIG.get(key, default))
    except (TypeError, ValueError):
        return default

REPO_ROOT = Path(__file__).resolve().parent

def _cfg_path(key, default):
    raw = os.path.expandvars(os.path.expanduser(CONFIG.get(key, default)))
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p

# ── Low-resource detection ─────────────────────────────────────────
def _detect_low_resource():
    try:
        model = Path('/proc/device-tree/model').read_text(
            errors='replace').strip('\x00 \n')
    except OSError:
        model = ''
    if 'Zero' in model or 'Pi 1' in model:
        return True, f'board model "{model}"'
    if os.uname().machine == 'armv6l':
        return True, 'armv6 CPU (Pi Zero / Pi 1)'
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    kb = int(line.split()[1])
                    if kb <= 700 * 1024:
                        return True, f'{kb // 1024} MB RAM'
                    break
    except OSError:
        pass
    return False, ''

LOW_RESOURCE, LOW_RESOURCE_REASON = _detect_low_resource()

# ── Setup ─────────────────────────────────────────────────────────
logging.basicConfig(filename=_cfg('LOG_FILE', 'frame_display.log'),
                    level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── User-tunable settings (from config.sh) ────────────────────────
PHOTOS_DIRS     = [Path(os.path.expandvars(os.path.expanduser(p)))
                   for p in _cfg('PHOTO_DIRS', '$HOME/Pictures').split(':') if p]
DEFAULT_DURATION = _cfg_int('DEFAULT_DURATION', 30)
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
SCHEDULE_OFF_FLAG = Path('/tmp/frame_schedule_off')

# Persisted schedule config
SCHEDULE_CONFIG = Path(SOURCE_FILE.parent / 'schedule.json')

# In-memory state
state = {
    'year_index': {},
    'year_index_ready': False,
    'schedule_enabled': True,
    'screen_off_time': (23, 59),
    'screen_on_time': (6, 0),
    'slideshow_paused_by_schedule': False,
    'screen_scheduled_off': False,
    'slideshow_was_running_before_schedule': False,
    'last_scheduled_state': None,
}

# ── Memory management ────────────────────────────────────────────
def _malloc_trim():
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception:
        pass

# ── Control-file helpers ──────────────────────────────────────────
def _read_file(path, default=''):
    try:
        return path.read_text().strip() if path.exists() else default
    except Exception:
        return default

def _write_file(path, content):
    try:
        path.write_text(str(content))
    except Exception as e:
        logger.error(f'Failed to write {path}: {e}')

# ── Process detection (no psutil — walk /proc) ────────────────────
VIEWER_NAMES = (b'fbi', b'feh', b'eog', b'display')
_pid_cache = {'viewer': None, 'viewer_ts': 0, 'script': None, 'script_ts': 0}
_PID_CACHE_TTL = 10

def _proc_cmdline(pid):
    """Read /proc/<pid>/cmdline, return list of byte strings."""
    try:
        data = Path(f'/proc/{pid}/cmdline').read_bytes()
        if not data:
            return []
        return data.rstrip(b'\x00').split(b'\x00')
    except OSError:
        return []

def _proc_comm(pid):
    """Read /proc/<pid>/comm, return bytes."""
    try:
        return Path(f'/proc/{pid}/comm').read_bytes().strip()
    except OSError:
        return b''

def _pid_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False

def _scan_viewer_pid():
    """Walk /proc to find the running image-viewer process."""
    try:
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            comm = _proc_comm(pid)
            if comm in VIEWER_NAMES:
                return pid
            cmdline = _proc_cmdline(pid)
            if any(b'fbviewer.py' in arg for arg in cmdline):
                return pid
    except OSError:
        pass
    return None

def get_viewer_pid():
    now = time.time()
    if now - _pid_cache['viewer_ts'] < _PID_CACHE_TTL:
        pid = _pid_cache['viewer']
        if pid is None or _pid_exists(pid):
            return pid
    pid = _scan_viewer_pid()
    _pid_cache['viewer'] = pid
    _pid_cache['viewer_ts'] = now
    return pid

def _scan_frame_script_pid():
    try:
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            cmdline = _proc_cmdline(pid)
            if any(b'start_frame.sh' in arg for arg in cmdline):
                return pid
    except OSError:
        pass
    return None

def get_frame_script_pid():
    now = time.time()
    if now - _pid_cache['script_ts'] < _PID_CACHE_TTL:
        pid = _pid_cache['script']
        if pid is None or _pid_exists(pid):
            return pid
    pid = _scan_frame_script_pid()
    _pid_cache['script'] = pid
    _pid_cache['script_ts'] = now
    return pid

def is_running():
    return get_viewer_pid() is not None

def get_current_duration():
    val = _read_file(DURATION_FILE, str(DEFAULT_DURATION))
    try:
        return int(val)
    except ValueError:
        return DEFAULT_DURATION

def get_slideshow_state():
    try:
        if SLIDESHOW_STATE.exists():
            return json.loads(SLIDESHOW_STATE.read_text())
    except Exception:
        pass
    return {}

def get_current_year_filter():
    val = _read_file(YEAR_FILTER)
    if val and val.isdigit() and len(val) == 4:
        return int(val)
    return None

# ── Photo list helpers (streaming — no bulk loads) ────────────────
def _iter_photo_paths():
    try:
        if SOURCE_FILE.exists():
            with SOURCE_FILE.open('r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    p = line.rstrip('\n')
                    if p:
                        yield p
    except Exception as e:
        logger.error(f'Error reading source file: {e}')

def _live_photo_count():
    try:
        if not LIVE_LIST.exists():
            return 0
        n = 0
        with LIVE_LIST.open('rb') as f:
            for line in f:
                if line.strip():
                    n += 1
        return n
    except Exception:
        return 0

def _live_photo_at(idx):
    try:
        if not LIVE_LIST.exists():
            return None, 0
        n = 0
        result = None
        with LIVE_LIST.open('r', encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.rstrip('\n')
                if not p:
                    continue
                if n == idx:
                    result = p
                n += 1
        if result is None and n > 0:
            return _live_photo_at(idx % n)
        return result, n
    except Exception:
        return None, 0

def _live_photo_slice(start, end):
    try:
        if not LIVE_LIST.exists():
            return [], 0
        n = 0
        result = []
        with LIVE_LIST.open('r', encoding='utf-8', errors='replace') as f:
            for line in f:
                p = line.rstrip('\n')
                if not p:
                    continue
                if start <= n < end:
                    result.append(p)
                n += 1
        return result, n
    except Exception:
        return [], 0

# ── Year index (regex on path — no PIL) ───────────────────────────
YEAR_RE = re.compile(r'/((?:19|20)\d{2})/')

def build_year_index():
    def _build():
        state['year_index_ready'] = False
        index = {}
        count = 0
        for p in _iter_photo_paths():
            count += 1
            m = YEAR_RE.search(p)
            if m:
                year = int(m.group(1))
                index[year] = index.get(year, 0) + 1
        state['year_index'] = index
        state['year_index_ready'] = True
        logger.info(f'Year index built: {len(index)} years from {count} photos')
        gc.collect()
        _malloc_trim()
    t = threading.Thread(target=_build, daemon=True)
    t.start()

# ── System stats (no psutil — read /proc) ─────────────────────────
def _read_cpu_percent():
    """Approximate CPU usage over a 0.1s sample from /proc/stat."""
    try:
        def read_stat():
            with open('/proc/stat') as f:
                parts = f.readline().split()
            # user nice system idle iowait irq softirq steal
            idle = int(parts[4]) + int(parts[5])  # idle + iowait
            total = sum(int(x) for x in parts[1:])
            return idle, total
        idle1, total1 = read_stat()
        time.sleep(0.1)
        idle2, total2 = read_stat()
        d_total = total2 - total1
        d_idle = idle2 - idle1
        if d_total == 0:
            return 0.0
        return round((1.0 - d_idle / d_total) * 100, 1)
    except Exception:
        return 0.0

def _read_boot_time():
    """Read system boot time from /proc/stat."""
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('btime '):
                    return int(line.split()[1])
    except Exception:
        pass
    return int(time.time())

_BOOT_TIME = _read_boot_time()

def _read_disk_usage(path='/'):
    """Read disk usage via os.statvfs."""
    try:
        st = os.statvfs(path)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used = total - free
        pct = round(used / total * 100, 1) if total else 0.0
        return {'total': total, 'used': used, 'free': free, 'percent': pct}
    except Exception:
        return {'total': 0, 'used': 0, 'free': 0, 'percent': 0.0}

# ── Cache status (read-only — no filling on lite) ─────────────────
def _cache_settings():
    cfg = load_config(CONFIG_PATH)
    def _int(key, default):
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
    raw_dir = os.path.expandvars(os.path.expanduser(
        cfg.get('CACHE_DIR', 'cache')))
    cdir = Path(raw_dir)
    if not cdir.is_absolute():
        cdir = REPO_ROOT / cdir
    return {
        'size_mb':  max(0, _int('CACHE_SIZE_MB', 2048)),
        'compress': str(cfg.get('CACHE_COMPRESS', 'yes')).strip().lower()
                    in ('yes', 'true', '1', 'on'),
        'quality':  min(100, max(1, _int('CACHE_QUALITY', 82))),
        'dir':      cdir,
    }

def _cache_usage(cdir):
    total = count = 0
    try:
        for f in cdir.iterdir():
            if (not f.is_file() or f.name == 'manifest.tsv'
                    or f.name.endswith('.tmp')):
                continue
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                pass
    except OSError:
        pass
    return total, count

def _mount_fstype(path):
    try:
        target = str(Path(path).resolve())
    except OSError:
        target = str(path)
    best_mp, best_fs = '', None
    try:
        with open('/proc/mounts', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp, fs = parts[1], parts[2]
                if target == mp or target.startswith(mp.rstrip('/') + '/'):
                    if len(mp) >= len(best_mp):
                        best_mp, best_fs = mp, fs
    except OSError:
        pass
    return best_fs

NETWORK_FSTYPES = frozenset({
    'cifs', 'smb', 'smb2', 'smb3', 'smbfs',
    'nfs', 'nfs4', 'nfsv4',
    'fuse.sshfs', 'fuse.rclone', 'fuse.davfs',
    'autofs',
})

def _path_is_network(path):
    fs = _mount_fstype(path)
    return fs is not None and fs in NETWORK_FSTYPES

def _photo_dirs_are_local():
    if not PHOTOS_DIRS:
        return True
    return not any(_path_is_network(p) for p in PHOTOS_DIRS)

_total_photos_state = {'mtime': 0.0, 'count': 0}

def _total_photo_count():
    try:
        mt = SOURCE_FILE.stat().st_mtime
    except OSError:
        _total_photos_state['mtime'] = 0.0
        _total_photos_state['count'] = 0
        return 0
    if mt == _total_photos_state['mtime']:
        return _total_photos_state['count']
    n = 0
    try:
        with SOURCE_FILE.open('rb') as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        pass
    _total_photos_state['mtime'] = mt
    _total_photos_state['count'] = n
    return n

_manifest_state = {'mtime': 0.0, 'count': 0}

def _cache_manifest_count(cdir):
    """Count entries in manifest without loading them into a set."""
    mf = cdir / 'manifest.tsv'
    try:
        mt = mf.stat().st_mtime
    except OSError:
        _manifest_state['mtime'] = 0.0
        _manifest_state['count'] = 0
        return 0
    if mt == _manifest_state['mtime']:
        return _manifest_state['count']
    n = 0
    try:
        with mf.open('r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if '\t' in line:
                    n += 1
    except OSError:
        pass
    _manifest_state['mtime'] = mt
    _manifest_state['count'] = n
    return n

# ── DRM-based screen power ───────────────────────────────────────
# Same approach as the full server: a subprocess holds the DRM fd open.
_dpms_proc = None
_dpms_lock = threading.Lock()

_DPMS_OFF_SCRIPT = r'''
import os, signal, sys, time, ctypes, fcntl, struct

fd = os.open("/dev/dri/card0", os.O_RDWR)
try:
    fcntl.ioctl(fd, 0x0000641e, 0)       # DRM_IOCTL_SET_MASTER
except OSError:
    pass

libdrm = ctypes.CDLL("libdrm.so.2")

libdrm.drmModeObjectGetProperties.restype = ctypes.c_void_p
libdrm.drmModeGetProperty.restype = ctypes.c_void_p

def find_dpms(fd, conn_id):
    class drmModeConnector(ctypes.Structure):
        _fields_ = [
            ("connector_id", ctypes.c_uint32),
            ("encoder_id", ctypes.c_uint32),
            ("connector_type", ctypes.c_uint32),
            ("connector_type_id", ctypes.c_uint32),
            ("connection", ctypes.c_uint32),
            ("mmWidth", ctypes.c_uint32),
            ("mmHeight", ctypes.c_uint32),
            ("subpixel", ctypes.c_uint32),
            ("count_modes", ctypes.c_int),
            ("modes", ctypes.c_void_p),
            ("count_props", ctypes.c_int),
            ("props", ctypes.c_void_p),
            ("prop_values", ctypes.c_void_p),
            ("count_encoders", ctypes.c_int),
            ("encoders", ctypes.c_void_p),
        ]
    libdrm.drmModeGetConnector.restype = ctypes.POINTER(drmModeConnector)
    conn = libdrm.drmModeGetConnector(fd, conn_id)
    if not conn:
        return None
    c = conn.contents
    for i in range(c.count_props):
        pid = ctypes.cast(c.props + i * 4,
                          ctypes.POINTER(ctypes.c_uint32))[0]
        pp = libdrm.drmModeGetProperty(fd, pid)
        if pp:
            name = ctypes.string_at(pp + 8, 32).split(b"\x00")[0]
            libdrm.drmModeFreeProperty(pp)
            if name == b"DPMS":
                libdrm.drmModeFreeConnector(conn)
                return pid
    libdrm.drmModeFreeConnector(conn)
    return None

CONN_ID = 33
dpms_id = find_dpms(fd, CONN_ID)
if dpms_id is None:
    sys.stderr.write("dpms-helper: DPMS property not found\n")
    os.close(fd)
    sys.exit(1)

ret = libdrm.drmModeConnectorSetProperty(fd, CONN_ID, dpms_id, 3)
if ret != 0:
    sys.stderr.write(f"dpms-helper: SetProperty returned {ret}\n")
    os.close(fd)
    sys.exit(1)

sys.stdout.write("OK\n")
sys.stdout.flush()

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
signal.signal(signal.SIGINT,  lambda *_: sys.exit(0))
while True:
    time.sleep(3600)
'''

def _kill_viewer_and_wait(timeout=5):
    pid = get_viewer_pid()
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_exists(pid):
                break
            time.sleep(0.2)
    except ProcessLookupError:
        pass
    _pid_cache['viewer'] = None
    _pid_cache['viewer_ts'] = 0

def _set_screen(want):
    global _dpms_proc
    try:
        if want == 'off':
            _kill_viewer_and_wait()
            with _dpms_lock:
                if _dpms_proc is not None and _dpms_proc.poll() is None:
                    return
                proc = subprocess.Popen(
                    [sys.executable, '-c', _DPMS_OFF_SCRIPT],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
                try:
                    line = proc.stdout.readline()
                    if b'OK' in line:
                        _dpms_proc = proc
                        logger.info(f'Screen off: DPMS helper running '
                                    f'(pid={proc.pid})')
                    else:
                        err = proc.stderr.read().decode(errors='replace')
                        proc.kill()
                        logger.error(f'DPMS helper failed: {err}')
                except Exception as e:
                    proc.kill()
                    logger.error(f'DPMS helper start error: {e}')
        elif want == 'on':
            with _dpms_lock:
                if _dpms_proc is not None:
                    _dpms_proc.terminate()
                    try:
                        _dpms_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _dpms_proc.kill()
                    _dpms_proc = None
                    logger.info('Screen on: DPMS helper killed, fd released')
            subprocess.run(
                ['sudo', '-n', '/usr/local/bin/papaframe-screen', 'on'],
                capture_output=True, timeout=10, check=False,
            )
    except Exception as e:
        logger.error(f'Screen {want} failed: {e}')

# ── Daily Schedule ────────────────────────────────────────────────
def _load_schedule_config():
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
    try:
        SCHEDULE_CONFIG.write_text(json.dumps({
            'enabled': bool(state.get('schedule_enabled', True)),
            'screen_off_time': list(state.get('screen_off_time', (23, 59))),
            'screen_on_time':  list(state.get('screen_on_time',  (6,  0))),
        }))
    except Exception as e:
        logger.error(f'Failed to save schedule config: {e}')

def _should_be_off(now_hm, off_hm, on_hm):
    if off_hm == on_hm:
        return False
    if off_hm < on_hm:
        return off_hm <= now_hm < on_hm
    return now_hm >= off_hm or now_hm < on_hm

def _launch_frame_script():
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
    if desired == 'off':
        running_now = is_running() or get_frame_script_pid() is not None
        if not state.get('screen_scheduled_off', False):
            state['slideshow_was_running_before_schedule'] = running_now
        _write_file(SCHEDULE_OFF_FLAG, '1')
        if running_now:
            _write_file(STOP_FLAG, '1')
        _set_screen('off')
        state['screen_scheduled_off'] = True
        state['slideshow_paused_by_schedule'] = True
    else:
        SCHEDULE_OFF_FLAG.unlink(missing_ok=True)
        _set_screen('on')
        if (state.get('slideshow_paused_by_schedule', False)
                and state.get('slideshow_was_running_before_schedule', False)
                and not get_frame_script_pid()):
            _launch_frame_script()
        state['screen_scheduled_off'] = False
        state['slideshow_paused_by_schedule'] = False
        state['slideshow_was_running_before_schedule'] = False

def _run_scheduler():
    while True:
        try:
            if state.get('schedule_enabled', True):
                now = datetime.now()
                now_hm = (now.hour, now.minute)
                off_hm = state.get('screen_off_time', (23, 59))
                on_hm  = state.get('screen_on_time',  (6,  0))
                desired = 'off' if _should_be_off(now_hm, off_hm, on_hm) \
                          else 'on'
                helper_alive = (_dpms_proc is not None
                                and _dpms_proc.poll() is None)
                need_reapply = (desired == 'off'
                                and desired == state.get('last_scheduled_state')
                                and not helper_alive)
                if desired != state.get('last_scheduled_state') or need_reapply:
                    if need_reapply:
                        logger.warning(
                            'DPMS helper died — re-blanking screen')
                    else:
                        logger.info(
                            f'Schedule transition: -> {desired} '
                            f'(off={off_hm[0]:02d}:{off_hm[1]:02d}, '
                            f'on={on_hm[0]:02d}:{on_hm[1]:02d})')
                    _apply_schedule_state(desired)
                    state['last_scheduled_state'] = desired
        except Exception as e:
            logger.error(f'Scheduler error: {e}')
        time.sleep(30)

def _start_scheduler():
    _load_schedule_config()
    t = threading.Thread(target=_run_scheduler, daemon=True)
    t.start()
    off = state['screen_off_time']
    on  = state['screen_on_time']
    logger.info(f'Daily scheduler started (off {off[0]:02d}:{off[1]:02d}, '
                f'on {on[0]:02d}:{on[1]:02d}, '
                f'enabled={state["schedule_enabled"]})')

# ── HH:MM parser ─────────────────────────────────────────────────
def _parse_hm(label, raw):
    parts = str(raw).strip().split(':')
    if len(parts) != 2:
        raise ValueError(f'{label} must be in HH:MM format')
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f'{label}: invalid time values')
    return (h, m)

# ── HTTP request handler ─────────────────────────────────────────
class PapaFrameHandler(BaseHTTPRequestHandler):
    """Lightweight request handler mapping URL paths to API methods."""

    # Suppress default per-request log lines — we log important events only.
    def log_message(self, fmt, *args):
        pass

    # ── Routing helpers ───────────────────────────────────────────
    def _json_response(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def _serve_static(self, rel_path, content_type=None):
        """Serve a file from the static/ directory."""
        static_dir = REPO_ROOT / 'static'
        fpath = (static_dir / rel_path).resolve()
        # Prevent directory traversal
        if not str(fpath).startswith(str(static_dir)):
            self.send_error(403)
            return
        if not fpath.is_file():
            self.send_error(404)
            return
        if content_type is None:
            ext = fpath.suffix.lower()
            content_type = {
                '.html': 'text/html; charset=utf-8',
                '.css':  'text/css; charset=utf-8',
                '.js':   'application/javascript; charset=utf-8',
                '.json': 'application/json',
                '.svg':  'image/svg+xml',
                '.png':  'image/png',
                '.jpg':  'image/jpeg',
                '.ico':  'image/x-icon',
            }.get(ext, 'application/octet-stream')
        data = fpath.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── GET routes ────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        # Static pages
        if path in ('', '/'):
            return self._serve_static('index-lite.html')
        if path == '/lite':
            return self._serve_static('index-lite.html')
        if path == '/full':
            return self._serve_static('index.html')
        if path == '/admin':
            return self._serve_static('admin.html')

        # API endpoints
        if path == '/api/status':
            return self._api_status()
        if path == '/api/hostname':
            return self._json_response(
                {'hostname': socket.gethostname()})
        if path == '/api/version':
            from version import __version__
            return self._json_response(
                {'version': __version__, 'variant': 'lite'})
        if path == '/api/uimode':
            return self._api_uimode()
        if path == '/api/stats':
            return self._api_stats()
        if path == '/api/currentphoto':
            return self._api_currentphoto()
        if path == '/api/photoinfo':
            return self._api_photoinfo()
        if path == '/api/years':
            return self._api_years()
        if path == '/api/schedule/status':
            return self._api_schedule_status()
        if path == '/api/cache/status':
            return self._api_cache_status()

        # Static file fallback (CSS, JS, images, favicon, etc.)
        # Strip leading / so it's relative to static/
        rel = path.lstrip('/')
        if rel:
            static_path = REPO_ROOT / 'static' / rel
            if static_path.is_file():
                return self._serve_static(rel)

        self.send_error(404)

    # ── POST routes ───────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')
        data = self._read_json_body()

        routes = {
            '/api/start':              self._api_start,
            '/api/stop':               self._api_stop,
            '/api/restart':            self._api_restart,
            '/api/screen':             self._api_screen,
            '/api/setduration':        self._api_setduration,
            '/api/setfilter':          self._api_setfilter,
            '/api/rebuildyears':       self._api_rebuildyears,
            '/api/schedule/configure': self._api_schedule_configure,
            '/api/schedule/enable':    self._api_schedule_enable,
            '/api/schedule/disable':   self._api_schedule_disable,
            '/api/cache/fill':         self._api_cache_fill,
        }

        handler = routes.get(path)
        if handler:
            return handler(data)
        self.send_error(404)

    # ── API implementations ───────────────────────────────────────

    def _api_status(self):
        viewer_pid = get_viewer_pid()
        running = viewer_pid is not None
        duration = get_current_duration()
        ss = get_slideshow_state()
        self._json_response({
            'running': running,
            'pid': viewer_pid,
            'duration': duration if running else None,
            'started_at': ss.get('started_at'),
            'viewer': ss.get('viewer'),
            'paused_by_schedule': bool(
                state.get('slideshow_paused_by_schedule', False)),
            'screen_scheduled_off': bool(
                state.get('screen_scheduled_off', False)),
            'schedule_enabled': bool(
                state.get('schedule_enabled', True)),
        })

    def _api_uimode(self):
        self._json_response({
            'mode':         'lite',
            'low_resource': LOW_RESOURCE,
            'reason':       LOW_RESOURCE_REASON or 'lite server',
            'configured':   CONFIG.get('LITE_UI', 'auto'),
        })

    def _api_stats(self):
        cpu = _read_cpu_percent()
        disk = _read_disk_usage('/')
        uptime = int(time.time() - _BOOT_TIME)
        self._json_response({
            'cpu': cpu,
            'uptime': uptime,
            'storage': disk,
        })

    def _api_currentphoto(self):
        total = _live_photo_count()
        if total == 0:
            return self._json_response({
                'current': None, 'previous': None, 'next': None,
                'index': 0, 'total': 0, 'error': 'No photos',
            })
        ss = get_slideshow_state()
        started_at = ss.get('started_at', 0)
        duration = ss.get('duration', DEFAULT_DURATION)
        if started_at and duration and is_running():
            elapsed = time.time() - started_at
            idx = int(elapsed / duration) % total
        else:
            idx = 0
        prev_idx = (idx - 1) % total
        next_idx = (idx + 1) % total

        need = sorted(set([prev_idx, idx, next_idx]))
        if need[-1] - need[0] < total:
            paths_slice, _ = _live_photo_slice(need[0], need[-1] + 1)
            path_map = {need[0] + i: p
                        for i, p in enumerate(paths_slice)}
        else:
            path_map = {}
            for n in need:
                p, _ = _live_photo_at(n)
                path_map[n] = p

        def photo_details(path):
            if path is None:
                return None
            try:
                return {
                    'path': path,
                    'filename': Path(path).name,
                    'album': Path(path).parent.name,
                }
            except Exception:
                return {'path': path, 'error': 'unreadable'}

        self._json_response({
            'current': photo_details(path_map.get(idx)),
            'previous': photo_details(path_map.get(prev_idx))
                        if total > 1 else None,
            'next': photo_details(path_map.get(next_idx))
                    if total > 1 else None,
            'index': idx,
            'total': total,
        })

    def _api_photoinfo(self):
        total = jpg_count = png_count = 0
        for p in _iter_photo_paths():
            total += 1
            low = p.lower()
            if low.endswith(('.jpg', '.jpeg')):
                jpg_count += 1
            elif low.endswith('.png'):
                png_count += 1
        years = list(state['year_index'].keys()) \
                if state['year_index_ready'] else []
        self._json_response({
            'count': total,
            'year_min': min(years) if years else None,
            'year_max': max(years) if years else None,
            'jpg': jpg_count,
            'png': png_count,
            'other': total - jpg_count - png_count,
        })

    def _api_years(self):
        years = sorted(
            [{'year': y, 'count': c}
             for y, c in state['year_index'].items()],
            key=lambda x: x['year'], reverse=True,
        )
        total = sum(x['count'] for x in years)
        self._json_response({
            'years': years,
            'total': total,
            'active': get_current_year_filter(),
            'ready': state['year_index_ready'],
        })

    def _api_schedule_status(self):
        screen_off = state.get('screen_off_time', (23, 59))
        screen_on = state.get('screen_on_time', (6, 0))
        self._json_response({
            'enabled': state.get('schedule_enabled', True),
            'screen_off_time':
                f'{screen_off[0]:02d}:{screen_off[1]:02d}',
            'screen_on_time':
                f'{screen_on[0]:02d}:{screen_on[1]:02d}',
            'slideshow_paused_by_schedule':
                state.get('slideshow_paused_by_schedule', False),
        })

    def _api_cache_status(self):
        s = _cache_settings()
        used_bytes, count = _cache_usage(s['dir'])
        manifest_count = _cache_manifest_count(s['dir'])
        total_photos = _total_photo_count()
        local = _photo_dirs_are_local()
        photo_dirs_info = [{
            'path': str(p),
            'fstype': _mount_fstype(p),
            'is_network': _path_is_network(p),
        } for p in PHOTOS_DIRS]
        coverage = (manifest_count / total_photos * 100.0) \
                   if total_photos else 0.0
        MB = 1024 * 1024
        self._json_response({
            'enabled':            s['size_mb'] > 0,
            'effective_enabled':  s['size_mb'] > 0 and not local,
            'photo_dirs_local':   local,
            'photo_dirs':         photo_dirs_info,
            'size_mb':            s['size_mb'],
            'used_mb':            round(used_bytes / MB, 1),
            'count':              count,
            'manifest_count':     manifest_count,
            'total_photos':       total_photos,
            'coverage_pct':       round(coverage, 1),
            'compress':           s['compress'],
            'quality':            s['quality'],
            'dir':                str(s['dir']),
            'filling':            False,
        })

    # ── POST handlers ─────────────────────────────────────────────

    def _api_start(self, data):
        if is_running():
            return self._json_response(
                {'error': 'Slideshow already running'}, 400)
        duration = max(20, int(data.get('duration', DEFAULT_DURATION)))
        _write_file(DURATION_FILE, duration)
        STOP_FLAG.unlink(missing_ok=True)
        if get_frame_script_pid():
            logger.info('start_frame.sh already running')
            return self._json_response({'success': True})
        try:
            subprocess.Popen(
                ['bash', str(FRAME_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info(f'Launched start_frame.sh with duration {duration}s')
            return self._json_response({'success': True})
        except Exception as e:
            logger.error(f'Failed to launch start_frame.sh: {e}')
            return self._json_response({'error': str(e)}, 500)

    def _api_stop(self, data):
        if not is_running() and not get_frame_script_pid():
            return self._json_response(
                {'error': 'Slideshow not running'}, 400)
        _write_file(STOP_FLAG, '1')
        logger.info('Stop flag written')
        return self._json_response({'success': True})

    def _api_restart(self, data):
        duration = data.get('duration', DEFAULT_DURATION)
        _write_file(DURATION_FILE, duration)
        viewer_pid = get_viewer_pid()
        if viewer_pid:
            try:
                os.kill(viewer_pid, signal.SIGTERM)
                logger.info(f'Killed viewer PID {viewer_pid} for restart')
            except Exception as e:
                logger.error(f'Failed to kill viewer: {e}')
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
                return self._json_response({'error': str(e)}, 500)
        return self._json_response({'success': True})

    def _api_screen(self, data):
        want = (data.get('state') or '').lower()
        if want not in ('on', 'off'):
            return self._json_response(
                {'error': "state must be 'on' or 'off'"}, 400)
        try:
            if want == 'off':
                _write_file(STOP_FLAG, '1')
            _set_screen(want)
            if want == 'on':
                STOP_FLAG.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f'Screen control failed: {e}')
            return self._json_response({'error': str(e)}, 500)
        logger.info(f'Screen set to {want}')
        return self._json_response({'success': True, 'state': want})

    def _api_setduration(self, data):
        new_dur = max(20, int(data.get('duration', 20)))
        _write_file(DURATION_FILE, new_dur)
        logger.info(f'Duration set to {new_dur}s')
        return self._json_response({'success': True})

    def _api_setfilter(self, data):
        year = data.get('year')
        if year:
            _write_file(YEAR_FILTER, year)
        else:
            YEAR_FILTER.unlink(missing_ok=True)
        LOCATION_FILTER.unlink(missing_ok=True)
        FILTERED_LIST.unlink(missing_ok=True)
        viewer_pid = get_viewer_pid()
        if viewer_pid:
            try:
                os.kill(viewer_pid, signal.SIGTERM)
            except Exception:
                pass
        logger.info(f'Year filter set to {year}')
        return self._json_response({'success': True})

    def _api_rebuildyears(self, data):
        build_year_index()
        return self._json_response({'success': True})

    def _api_schedule_configure(self, data):
        try:
            if 'screen_off_time' in data:
                state['screen_off_time'] = _parse_hm(
                    'screen_off_time', data['screen_off_time'])
            if 'screen_on_time' in data:
                state['screen_on_time'] = _parse_hm(
                    'screen_on_time', data['screen_on_time'])
            state['last_scheduled_state'] = None
            _save_schedule_config()
            logger.info(
                f"Schedule updated: off at "
                f"{state['screen_off_time'][0]:02d}:"
                f"{state['screen_off_time'][1]:02d}, "
                f"on at "
                f"{state['screen_on_time'][0]:02d}:"
                f"{state['screen_on_time'][1]:02d}")
            return self._json_response({'success': True})
        except Exception as e:
            logger.error(f'Schedule configure failed: {e}')
            return self._json_response({'error': str(e)}, 400)

    def _api_schedule_enable(self, data):
        state['schedule_enabled'] = True
        state['last_scheduled_state'] = None
        _save_schedule_config()
        logger.info('Schedule enabled')
        return self._json_response({'success': True, 'enabled': True})

    def _api_schedule_disable(self, data):
        state['schedule_enabled'] = False
        if state.get('screen_scheduled_off', False):
            _apply_schedule_state('on')
        state['last_scheduled_state'] = None
        _save_schedule_config()
        logger.info('Schedule disabled')
        return self._json_response({'success': True, 'enabled': False})

    def _api_cache_fill(self, data):
        # Lite server doesn't support cache filling (needs PIL)
        return self._json_response(
            {'error': 'Cache filling is not available on the lite server. '
                      'Use the full server or fill from another Pi.'},
            501)


# ── Entry point ────────────────────────────────────────────────────
def main():
    logger.info(f'PapaFrame Lite starting '
                f'(low_resource={LOW_RESOURCE}, '
                f'reason={LOW_RESOURCE_REASON or "n/a"})')
    logger.info('Building year index in background...')
    build_year_index()
    logger.info('Starting daily scheduler...')
    _start_scheduler()
    logger.info(f'Server starting on http://{SERVER_HOST}:{SERVER_PORT}')
    print(f'PapaFrame Lite · http://{SERVER_HOST}:{SERVER_PORT}')

    server = ThreadingHTTPServer(
        (SERVER_HOST, SERVER_PORT), PapaFrameHandler)

    # Graceful shutdown on SIGTERM (systemd stop)
    def _shutdown(signum, frame):
        logger.info('Received signal, shutting down...')
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info('Server stopped.')


if __name__ == '__main__':
    import argparse
    from version import __version__

    parser = argparse.ArgumentParser(description='PapaFrame Server (Lite)')
    parser.add_argument('--version', action='version',
                        version=f'PapaFrame {__version__} (lite)')
    parser.parse_args()

    main()
