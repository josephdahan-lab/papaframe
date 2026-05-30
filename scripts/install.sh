#!/bin/bash
# PapaFrame system installer for Raspberry Pi (or any Debian-based Linux).
#
# What this does:
#   1. Installs apt packages (fbi, python venv tools; Pillow build deps only
#      on boards that need them — see "Hardware-aware" below)
#   2. Adds the install user to the `video` group (needed for /dev/fb0, /dev/dri)
#   3. Creates .venv next to server.py and pip-installs requirements.txt
#   4. Installs scripts/papaframe-screen → /usr/local/bin and the sudoers rule
#   5. Sets the default systemd target to multi-user (no graphical login)
#   6. Configures autologin for the install user on tty1
#   7. Adds an exec line to ~/.bash_profile so tty1 launches the slideshow
#   8. Installs and enables a systemd unit for the Flask web server
#
# Skipped — do these yourself:
#   • Raspberry Pi OS install + first boot (use Raspberry Pi Imager and set
#     hostname / Wi-Fi / SSH there).
#   • Mounting a network share for photos (see INSTALL.md "Photos on a NAS").
#   • Editing config.sh (PHOTO_DIRS, schedule, port). See INSTALL.md.
#
# Run from inside the cloned repo:
#   sudo bash scripts/install.sh
#
# Idempotent — safe to re-run.
#
# Hardware-aware — the installer detects the board (arch / RAM) and adapts:
#   • armv6 (original Pi Zero / Pi 1): no prebuilt pip wheels exist, so it
#     installs Pillow build deps + Debian's python3-* packages, builds the
#     venv with --system-site-packages, grows swap for the from-source Pillow
#     build, and skips reverse_geocoder (its scipy dependency has no armv6
#     wheels and will not build on a Pi Zero).
#   • Everything newer (Pi Zero 2 W, Pi 3/4/5): installs straight from wheels.

set -e

if [ "$EUID" -ne 0 ]; then
    echo "This installer needs root. Re-run with: sudo bash scripts/install.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_USER="${SUDO_USER:-$USER}"
if [ "$TARGET_USER" = "root" ]; then
    echo "Refusing to install for root — run via 'sudo' as a regular user." >&2
    exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -d "$TARGET_HOME" ] || { echo "Cannot find home dir for $TARGET_USER" >&2; exit 1; }

# ── Hardware detection ──────────────────────────────────────────────────────
# Decides which install path to take. armv6 (original Pi Zero / Pi 1) has no
# prebuilt pip wheels for Pillow or scipy; everything newer installs from
# wheels with no compiling.
ARCH="$(uname -m)"

PI_MODEL="unknown board"
for f in /sys/firmware/devicetree/base/model /proc/device-tree/model; do
    if [ -r "$f" ]; then
        PI_MODEL="$(tr -d '\0' < "$f")"
        break
    fi
done

RAM_MB=$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) / 1024 ))

# Decide between the full server (Flask + PIL + psutil) and the lite server
# (stdlib only, ~20 MB RSS). The lite server is used on boards where the full
# server OOMs or is too slow: armv6l (Pi Zero / Pi 1) and any board with
# ≤700 MB RAM. The lite server provides: slideshow control, schedule,
# landing page with controls and basic info — but no cache filling, no
# thumbnails, no location index, no admin config editor.
USE_LITE=0
if [ "$ARCH" = "armv6l" ] || [ "$RAM_MB" -le 700 ]; then
    USE_LITE=1
fi
# Override: if the user explicitly set PAPAFRAME_FULL=1, use full regardless.
if [ "${PAPAFRAME_FULL:-0}" = "1" ]; then
    USE_LITE=0
fi

if [ "$USE_LITE" -eq 1 ]; then
    HAS_WHEELS=0
    WANT_GEOCODER=0
    SERVER_SCRIPT="server_lite.py"
    PLAN_DESC="lite server — stdlib only, no venv needed"
    UI_HINT="lite server + lite UI (auto: armv6 or ≤700 MB RAM)"
elif [ "$ARCH" = "armv6l" ]; then
    HAS_WHEELS=0
    WANT_GEOCODER=0
    SERVER_SCRIPT="server.py"
    PLAN_DESC="armv6 — build Pillow from source, reuse Debian Python packages"
    UI_HINT="full dashboard"
else
    HAS_WHEELS=1
    WANT_GEOCODER=1
    SERVER_SCRIPT="server.py"
    PLAN_DESC="prebuilt wheels available — straight pip install"
    UI_HINT="full dashboard"
fi

# Low-RAM boards need swap so a from-source Pillow build is not OOM-killed.
if [ "$RAM_MB" -lt 1024 ]; then LOW_RAM=1; else LOW_RAM=0; fi

# Display stack — informational only; config.sh FBI_DEVICE=auto handles both.
if [ -e /dev/fb0 ]; then
    DISPLAY_HINT="/dev/fb0 present (legacy framebuffer)"
elif [ -d /dev/dri ]; then
    DISPLAY_HINT="KMS/DRM only, no /dev/fb0"
else
    DISPLAY_HINT="no framebuffer or DRM device — check the display cable/driver"
fi

# Grow swap to at least <MB> via dphys-swapfile (Raspberry Pi OS default).
ensure_swap() {
    local want_mb="$1" cur_mb
    cur_mb=$(( $(awk '/^SwapTotal:/ {print $2}' /proc/meminfo) / 1024 ))
    if [ "$cur_mb" -ge "$want_mb" ]; then
        echo "       Swap is ${cur_mb} MB (≥ ${want_mb} MB) — leaving alone."
        return
    fi
    if ! command -v dphys-swapfile >/dev/null; then
        echo "       WARNING: ${cur_mb} MB swap and no dphys-swapfile to grow it."
        echo "                The Pillow build may run out of memory — add swap"
        echo "                manually, then re-run this installer."
        return
    fi
    echo "       Growing swap ${cur_mb} → ${want_mb} MB for the Pillow build…"
    dphys-swapfile swapoff || true
    sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${want_mb}/" /etc/dphys-swapfile
    dphys-swapfile setup
    dphys-swapfile swapon
}

echo "═══════════════════════════════════════════════════"
echo "  PapaFrame installer"
echo "    repo:    $REPO_ROOT"
echo "    user:    $TARGET_USER  (home: $TARGET_HOME)"
echo "    board:   $PI_MODEL"
echo "    arch:    $ARCH, ${RAM_MB} MB RAM"
echo "    display: $DISPLAY_HINT"
echo "    server:  $SERVER_SCRIPT"
echo "    web UI:  $UI_HINT"
echo "    plan:    $PLAN_DESC"
echo "═══════════════════════════════════════════════════"
echo

# ── 1. apt packages ─────────────────────────────────────────────────────────
echo "[1/8] Installing system packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

if [ "$USE_LITE" -eq 1 ]; then
    # Lite server is stdlib-only — no pip packages needed, no venv.
    APT_PKGS=(fbi python3 git curl ca-certificates)
    echo "       Lite server — minimal packages, no Python dependencies."
else
    APT_PKGS=(fbi python3 python3-venv python3-pip git curl ca-certificates)
    if [ "$HAS_WHEELS" -eq 0 ]; then
        # No wheels → Pillow compiles from source: needs headers + build tooling.
        # Debian's python3-* packages are reused by the venv (step 3) so the heavy
        # libraries do not all have to build from scratch.
        APT_PKGS+=(python3-dev libjpeg-dev zlib1g-dev libfreetype-dev)
        APT_PKGS+=(python3-pil python3-flask python3-psutil python3-pycountry)
        echo "       armv6 board — adding Pillow build deps + Debian Python packages."
    else
        echo "       Wheels available — skipping Pillow build dependencies."
    fi
fi
apt-get install -y --no-install-recommends "${APT_PKGS[@]}"

# ── 2. video group ──────────────────────────────────────────────────────────
echo "[2/8] Adding $TARGET_USER to video group…"
usermod -aG video "$TARGET_USER"

# ── 3. Python venv + requirements ───────────────────────────────────────────
if [ "$USE_LITE" -eq 1 ]; then
    echo "[3/8] Skipping venv — lite server uses system Python only."
else
    echo "[3/8] Creating Python venv at $REPO_ROOT/.venv…"
    VENV_ARGS=()
    if [ "$HAS_WHEELS" -eq 0 ]; then
        # Let the venv see Debian's python3-pil/flask/psutil/pycountry instead of
        # building them all from source.
        VENV_ARGS+=(--system-site-packages)
    fi
    if [ ! -d "$REPO_ROOT/.venv" ]; then
        sudo -u "$TARGET_USER" python3 -m venv "${VENV_ARGS[@]}" "$REPO_ROOT/.venv"
    fi

    # A from-source Pillow build OOM-kills on a 512 MB board without swap.
    if [ "$HAS_WHEELS" -eq 0 ] && [ "$LOW_RAM" -eq 1 ]; then
        ensure_swap 512
    fi

    PIP="$REPO_ROOT/.venv/bin/pip"
    # --retries/--timeout: a single dropped connection on Pi Wi-Fi should not abort
    # the whole install. --prefer-binary: take a wheel over an sdist when offered.
    PIP_NET=(--retries 10 --timeout 120 --prefer-binary)
    echo "       Upgrading pip + wheel…"
    sudo -u "$TARGET_USER" "$PIP" install --upgrade --quiet "${PIP_NET[@]}" pip wheel

    REQ_FILE="$REPO_ROOT/requirements.txt"
    if [ "$WANT_GEOCODER" -eq 0 ]; then
        # reverse_geocoder pulls scipy, which has no armv6 wheels and will not
        # build on a Pi Zero. server.py already runs fine without it.
        echo "       Skipping reverse_geocoder (needs scipy — no armv6 wheels)."
        REQ_FILE="$(mktemp)"
        grep -vi '^reverse_geocoder' "$REPO_ROOT/requirements.txt" > "$REQ_FILE"
        chmod 0644 "$REQ_FILE"
    fi
    if [ "$HAS_WHEELS" -eq 0 ]; then
        echo "       Installing Python requirements (Pillow builds from source — slow)…"
    else
        echo "       Installing Python requirements…"
    fi
    sudo -u "$TARGET_USER" "$PIP" install --quiet "${PIP_NET[@]}" -r "$REQ_FILE"
    if [ "$WANT_GEOCODER" -eq 0 ]; then rm -f "$REQ_FILE"; fi
fi

# ── 4. Screen helper + CLI + sudoers ───────────────────────────────────────
echo "[4/8] Installing /usr/local/bin/papaframe-screen + papaframe CLI + sudoers rule…"
install -m 0755 "$REPO_ROOT/scripts/papaframe-screen" /usr/local/bin/papaframe-screen
# CLI wrapper — bake in the repo root so it works from any cwd.
sed "s|PAPAFRAME_ROOT:-.*}|PAPAFRAME_ROOT:-$REPO_ROOT}|" \
    "$REPO_ROOT/scripts/papaframe" > /usr/local/bin/papaframe
chmod 0755 /usr/local/bin/papaframe
SUDOERS=/etc/sudoers.d/papaframe-screen
cat > "$SUDOERS" <<EOF
$TARGET_USER ALL=(root) NOPASSWD: /usr/local/bin/papaframe-screen on, /usr/local/bin/papaframe-screen off
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS" >/dev/null

# ── 5. Boot to console (no graphical target) ────────────────────────────────
echo "[5/8] Setting default boot target to multi-user (console)…"
systemctl set-default multi-user.target >/dev/null

# ── 6. Autologin on tty1 ────────────────────────────────────────────────────
echo "[6/8] Configuring autologin for $TARGET_USER on tty1…"
DROPIN_DIR=/etc/systemd/system/getty@tty1.service.d
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $TARGET_USER --noclear %I \$TERM
EOF
systemctl daemon-reload

# ── 7. .bash_profile slideshow exec ─────────────────────────────────────────
echo "[7/8] Wiring slideshow autostart in $TARGET_HOME/.bash_profile…"
PROFILE="$TARGET_HOME/.bash_profile"
MARKER="# papaframe: launch slideshow when logged in on tty1"
EXEC_LINE="[ \"\$(tty)\" = '/dev/tty1' ] && exec bash $REPO_ROOT/scripts/start_frame.sh"
if [ ! -f "$PROFILE" ] || ! grep -Fq "$MARKER" "$PROFILE"; then
    NEW_FILE=0
    [ ! -f "$PROFILE" ] && NEW_FILE=1
    {
        # If we're creating .bash_profile fresh, source ~/.profile and ~/.bashrc
        # so the user's existing login-shell init isn't silently shadowed.
        if [ "$NEW_FILE" -eq 1 ]; then
            echo '[ -f ~/.profile ] && . ~/.profile'
            echo '[ -f ~/.bashrc ]  && . ~/.bashrc'
            echo ''
        fi
        echo "$MARKER"
        echo "$EXEC_LINE"
    } >> "$PROFILE"
    chown "$TARGET_USER:$TARGET_USER" "$PROFILE"
    if [ "$NEW_FILE" -eq 1 ]; then
        echo "       Created $PROFILE (sources ~/.profile and ~/.bashrc)."
    else
        echo "       Appended exec line to existing $PROFILE."
    fi
else
    echo "       Already present — leaving alone."
fi

# ── 8. Systemd unit for the web server ──────────────────────────────────────
echo "[8/8] Installing papaframe-server systemd unit…"
UNIT=/etc/systemd/system/papaframe-server.service

if [ "$USE_LITE" -eq 1 ]; then
    PYTHON_BIN="/usr/bin/python3"
    MEMORY_LIMITS="MemoryMax=80M
MemoryHigh=50M"
else
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python3"
    MEMORY_LIMITS=""
fi

cat > "$UNIT" <<EOF
[Unit]
Description=PapaFrame web server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$REPO_ROOT
ExecStart=$PYTHON_BIN $REPO_ROOT/$SERVER_SCRIPT
Restart=on-failure
RestartSec=5
$MEMORY_LIMITS

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable papaframe-server.service >/dev/null

echo
echo "═══════════════════════════════════════════════════"
echo "  Done."
echo "═══════════════════════════════════════════════════"
echo
echo "Next steps:"
echo "  1. Edit $REPO_ROOT/config.sh — set PHOTO_DIRS to where your photos are."
echo "  2. (Optional) Mount a network share for photos. See INSTALL.md."
echo "  3. Reboot to start the slideshow + web server:"
echo "       sudo reboot"
echo
echo "After reboot:"
echo "  • Slideshow runs on the attached display (tty1)."
echo "  • Web UI at http://\$(hostname -I | awk '{print \$1}'):8000/"
echo "  • Logs:  journalctl -u papaframe-server -f"
echo "          tail -f $REPO_ROOT/frame_display.log"
