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
PHOTO_DIRS="$HOME/Pictures"

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
FBI_VT=auto

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

