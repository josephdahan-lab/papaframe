# ═══════════════════════════════════════════════════════════════════
#  PapaFrame configuration
# ═══════════════════════════════════════════════════════════════════
# This is the ONE file to edit when moving PapaFrame to another
# picture frame. Both server.py and start_frame.sh read these values.
#
# Syntax rules:
#   KEY="value"           (quote values that contain spaces or /)
#   KEY=123               (numbers do not need quotes)
#   # anything after a hash is a comment
#
# After editing, restart the server (python3 server.py) for changes
# to take effect. Changes to PHOTO_DIRS also require rebuilding the
# photo list — delete SOURCE_FILE and restart the slideshow, or click
# "Rebuild photo list" on the admin page.
# ═══════════════════════════════════════════════════════════════════

# ── Photo library ─────────────────────────────────────────────────
# Folders that contain pictures. Colon-separated, like $PATH.
# All listed folders are scanned recursively for .jpg/.jpeg/.png files.
# ~ and $VARS are expanded by both server.py and start_frame.sh.
# Example (multiple roots):
#   PHOTO_DIRS="$HOME/Pictures:/mnt/nas/Photos"
#PHOTO_DIRS="$HOME/Pictures"
PHOTO_DIRS="/mnt/plex/Pictures"

# ── Photo cache ───────────────────────────────────────────────────
# PapaFrame can keep a local cache of upcoming photos, resized down to
# the display. The slideshow shows cached copies when they exist and
# falls back to PHOTO_DIRS otherwise — switching between the two is
# seamless and keeps the slideshow running even if the photo store
# (e.g. a NAS) goes offline. The cache is filled during the nightly
# screen-off window (see "Screen schedule" — set in the web UI).

# Cache budget in megabytes. The cache never grows past this; the
# oldest entries are evicted first. Set to 0 to disable caching.
CACHE_SIZE_MB=12288

# Compress cached photos. "yes" resizes each photo to fit 1920x1080
# and re-encodes it as JPEG — far smaller, so the budget holds more
# photos. "no" copies originals verbatim at full size.
CACHE_COMPRESS="yes"

# JPEG quality (1-100) for compressed cache entries. Ignored when
# CACHE_COMPRESS="no". 82 is a good size/quality balance.
CACHE_QUALITY=82

# Where cached photos are stored. Relative paths resolve from the repo
# root, so the default lands at ~/papaframe/cache.
CACHE_DIR="cache"

# ── Shared photo-location cache ───────────────────────────────────
# Per-photo GPS → country lookups for the location filter are expensive
# (every photo's EXIF read over CIFS) and produce identical results on
# every Pi sharing the same photo store. So the host that owns the photos
# can build the cache once (see tools/build_location_cache.py) and drop
# it on the share itself — every Pi then just copies that file at startup
# instead of scanning. On a Pi Zero this turns hours of CIFS reads into
# a sub-second file copy and unblocks the location filter even in lite UI.
#
#   "auto" — look at <mount>/.papaframe/location_cache.tsv for each
#            PHOTO_DIR's mountpoint (recommended)
#   "/some/path/cache.tsv" — explicit path
#   ""     — disable sharing; each Pi falls back to scanning locally
SHARED_LOCATION_CACHE="auto"

# ── Web UI ────────────────────────────────────────────────────────
# Which dashboard to serve at /. The full UI loads Leaflet + Chart.js and
# polls aggressively, which is too heavy for a Pi Zero. The lite UI is a
# single dependency-free page that keeps every control but drops the map,
# CPU chart, and photo thumbnails.
#   "auto" — lite on Pi Zero / low-RAM boards, full elsewhere (recommended)
#   "yes"  — force the lite UI everywhere
#   "no"   — force the full UI everywhere
LITE_UI="auto"

# ── Slideshow timing ──────────────────────────────────────────────
# Default seconds per photo when the UI does not specify one.
DEFAULT_DURATION=30

# How often (seconds) the background loop reshuffles the slideshow.
RESHUFFLE_INTERVAL=900

# ── Display environment ───────────────────────────────────────────
# FORCE_VIEWER: Image viewer to use. Set to "auto" for automatic detection
# based on environment, or explicitly set to: fbi (framebuffer), feh, eog, display
# - "auto"  : Auto-detect based on desktop environment (recommended)
# - "fbi"   : Linux framebuffer (no X11 required, works console-only)
# - "feh"   : Works on both framebuffer and X11 desktops
# - "eog"   : Eye of GNOME (X11/GNOME only)
# - "display": ImageMagick display (X11 only)
FORCE_VIEWER="auto"

# Virtual terminal number for fbi (only used if fbi is selected).
# "auto" picks the first writable VT (1-7). Set to a number (e.g. 1) to force.
FBI_VT="auto"

# DRM device for fbi. "auto" detects the card with a connected HDMI:
#  - Pi Zero / Pi 3: single card → /dev/dri/card0
#  - Pi 4 / Pi 5:    two cards, but only card1 has dumb-buffer support
# Override with /dev/dri/card0 or /dev/dri/card1 if auto-detect picks wrong.
# Leave empty ("") to let fbi choose its own default.
FBI_DEVICE="auto"

# ── Web server ────────────────────────────────────────────────────
# Bind address. "0.0.0.0" listens on every network interface.
SERVER_HOST="0.0.0.0"

# TCP port for the web UI (http://<frame-ip>:SERVER_PORT/).
SERVER_PORT=8000

# ── File paths ────────────────────────────────────────────────────
# All paths below: relative paths resolve from the repo root (the dir
# holding server.py). Absolute paths are used as-is. ~ and $VARS expand.

# Master photo list. Regenerated from PHOTO_DIRS when missing.
SOURCE_FILE="photo_list.txt"

# Slideshow launcher script invoked by the server.
FRAME_SCRIPT="scripts/start_frame.sh"

# Server log file.
LOG_FILE="frame_display.log"

