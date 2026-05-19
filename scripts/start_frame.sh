#!/bin/bash
exec > /tmp/start_frame_debug.log 2>&1
# Resolve script + repo root so relative paths work regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Load shared PapaFrame config (edit that file, not this one) ──────────────
CONFIG_FILE="${PAPAFRAME_CONFIG:-$REPO_ROOT/config.sh}"
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
fi

# ── Environment detection (console vs X11, framebuffer, writable VT) ─────────
detect_desktop_environment() { [ -n "$DISPLAY" ] && echo "x11" || echo "console"; }
detect_framebuffer() {
    if [ -c /dev/fb0 ]; then
        echo "/dev/fb0"
    elif ls /dev/dri/card* >/dev/null 2>&1; then
        # KMS-only (no legacy fbdev): fbi can still draw via DRM
        echo "drm"
    else
        echo "none"
    fi
}
detect_available_vt() {
    for v in "${1:-1}" 1 2 3 4 5 6 7; do
        [ -w "/dev/tty$v" ] && { echo "$v"; return 0; }
    done
    echo ""
}
# Find the DRM card whose HDMI is connected. On Pi Zero/3 this is card0; on
# Pi 4/5 the only card with dumb-buffer support is card1 (card0's KMS lacks
# it and fbi fails with "drm: no dumb buffer support"). Empty if no HDMI is
# currently connected — fbi will then run without -device.
detect_drm_device() {
    local connector card
    for connector in /sys/class/drm/card*-HDMI-*/status; do
        [ -f "$connector" ] || continue
        [ "$(cat "$connector" 2>/dev/null)" = "connected" ] || continue
        card="${connector#/sys/class/drm/}"
        card="${card%%-*}"
        echo "/dev/dri/$card"
        return 0
    done
    return 1
}

# Resolve config values: relative paths anchor at the repo root (matches
# the same rule applied in server.py via _cfg_path).
resolve_repo_path() {
    case "$1" in
        /*) echo "$1" ;;
        *)  echo "$REPO_ROOT/$1" ;;
    esac
}
SOURCE_FILE="$(resolve_repo_path "${SOURCE_FILE:-photo_list.txt}")"
CACHE_DIR="$(resolve_repo_path "${CACHE_DIR:-cache}")"
CACHE_MANIFEST="$CACHE_DIR/manifest.tsv"
PHOTO_DIRS="${PHOTO_DIRS:-$HOME/Pictures}"
RESHUFFLE_INTERVAL="${RESHUFFLE_INTERVAL:-900}"
DEFAULT_DURATION="${DEFAULT_DURATION:-5}"
DURATION="${DURATION:-$DEFAULT_DURATION}"

# DRM device for fbi: "auto" picks the card with a connected HDMI; explicit
# /dev/dri/cardN values are passed through; empty means "let fbi choose".
# When auto, detection is re-run before every fbi launch (see the loop below):
# the HDMI display card can be enumerated late — seconds to minutes after this
# script starts — and a one-shot detection at startup would otherwise pin fbi
# to the wrong card (or none) for the whole session.
if [ -z "${FBI_DEVICE+x}" ] || [ "$FBI_DEVICE" = "auto" ]; then
    FBI_DEVICE_AUTO=1
    FBI_DEVICE="$(detect_drm_device || true)"
else
    FBI_DEVICE_AUTO=0
fi

# ── Environment detection and viewer selection ────────────────────────────────
FORCE_VIEWER="${FORCE_VIEWER:-auto}"  # Options: auto, fbi, feh, eog, display
DESKTOP_ENV=$(detect_desktop_environment)
FRAMEBUFFER_DEVICE=$(detect_framebuffer)
AVAILABLE_VT=$(detect_available_vt "${FBI_VT:-1}")
SELECTED_VIEWER=""

# Determine which viewer to use
if [ "$FORCE_VIEWER" != "auto" ]; then
    SELECTED_VIEWER="$FORCE_VIEWER"
else
    # Auto-select based on environment
    case "$DESKTOP_ENV" in
        console)
            # Console-only: use framebuffer viewer
            if [ "$FRAMEBUFFER_DEVICE" != "none" ] && command -v fbi > /dev/null 2>&1; then
                SELECTED_VIEWER="fbi"
            elif command -v feh > /dev/null 2>&1; then
                SELECTED_VIEWER="feh"
            else
                echo "ERROR: No suitable image viewer found for console mode!" >&2
                echo "       Please install 'fbi' or 'feh'" >&2
                exit 1
            fi
            ;;
        x11)
            # X11 desktop available
            if command -v eog > /dev/null 2>&1; then
                SELECTED_VIEWER="eog"
            elif command -v feh > /dev/null 2>&1; then
                SELECTED_VIEWER="feh"
            elif command -v fbi > /dev/null 2>&1; then
                SELECTED_VIEWER="fbi"
            else
                echo "ERROR: No suitable image viewer found!" >&2
                exit 1
            fi
            ;;
        *)
            echo "WARNING: Unknown desktop environment, defaulting to console mode" >&2
            if command -v fbi > /dev/null 2>&1; then
                SELECTED_VIEWER="fbi"
            elif command -v feh > /dev/null 2>&1; then
                SELECTED_VIEWER="feh"
            fi
            ;;
    esac
fi

echo "Environment: $DESKTOP_ENV | Framebuffer: $FRAMEBUFFER_DEVICE | VT: $AVAILABLE_VT | Viewer: $SELECTED_VIEWER"

LIVE_LIST="/tmp/current_slideshow.txt"
# DISPLAY_LIST mirrors LIVE_LIST but with cached copies substituted in where
# the photo cache has them. The viewer reads DISPLAY_LIST; the server keeps
# reading LIVE_LIST (originals) so photo metadata (EXIF date/GPS) stays intact.
DISPLAY_LIST="/tmp/frame_display_list.txt"
RESHUFFLE_PID_FILE="/tmp/frame_reshuffle.pid"
DURATION_FILE="/tmp/frame_duration_override.txt"
STOP_FLAG="/tmp/frame_stop_requested"
RESUME_FILE="/tmp/frame_resume_photo.txt"
YEAR_FILTER="/tmp/frame_year_filter.txt"
LOCATION_FILTER="/tmp/frame_location_filter.txt"
FILTERED_LIST="/tmp/frame_filtered_list.txt"
# Set by atomic_shuffle when it rebuilt SOURCE_FILE from disk (full rescan).
# The monitor loop sees this and kicks fbi so the new list becomes visible.
RESCAN_FLAG="/tmp/frame_rescan_reload"

rm -f "$STOP_FLAG"

# ── Cleanup old reshuffle loop ────────────────────────────────────────────────
if [ -f "$RESHUFFLE_PID_FILE" ]; then
    OLD_PID=$(cat "$RESHUFFLE_PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ]; then
        kill -- "-$OLD_PID" 2>/dev/null || kill "$OLD_PID" 2>/dev/null
    fi
    rm -f "$RESHUFFLE_PID_FILE"
fi
pkill -f "sleep $RESHUFFLE_INTERVAL" 2>/dev/null

# ── Atomically shuffle the photo list ────────────────────────────────────────
atomic_shuffle() {
    local tmp="${LIVE_LIST}.tmp"
    local filter=""
    [ -f "$YEAR_FILTER" ] && filter=$(cat "$YEAR_FILTER" 2>/dev/null | tr -d '[:space:]')

    # Location filter takes precedence: the server pre-writes FILTERED_LIST
    # with only the paths matching the selected country (or "no location").
    if [ -f "$LOCATION_FILTER" ] && [ -f "$FILTERED_LIST" ]; then
        shuf "$FILTERED_LIST" > "$tmp"
        echo "Shuffled $(wc -l < "$tmp") photos for location $(cat "$LOCATION_FILTER" 2>/dev/null)"
    elif [[ "$filter" =~ ^(19|20)[0-9]{2}$ ]]; then
        # Year-filtered shuffle: grep paths that contain /YYYY/ or YYYY anywhere
        if [ -f "$SOURCE_FILE" ]; then
            grep "/$filter/" "$SOURCE_FILE" | shuf > "$tmp"
            # If the folder-path grep found nothing, widen to any occurrence in path
            if [ ! -s "$tmp" ]; then
                grep "$filter" "$SOURCE_FILE" | shuf > "$tmp"
            fi
        fi
        echo "Shuffled $(wc -l < "$tmp") photos for year $filter"
    else
        # Full shuffle
        if [ -s "$SOURCE_FILE" ]; then
            shuf "$SOURCE_FILE" > "$tmp"
        else
            # Rebuild master list from every directory in PHOTO_DIRS (colon-separated)
            : > "$tmp"
            IFS=':' read -r -a _dirs <<< "$PHOTO_DIRS"
            for d in "${_dirs[@]}"; do
                [ -d "$d" ] && find "$d" -type f \
                    \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" \) >> "$tmp"
            done
            shuf "$tmp" -o "$tmp"
            cp "$tmp" "$SOURCE_FILE"
            # Signal the monitor loop that fbi should reload with the new list.
            : > "$RESCAN_FLAG"
        fi
    fi
    # Overwrite content in-place (avoids sticky-bit mv issues between users)
    cat "$tmp" > "$LIVE_LIST" && rm -f "$tmp"
}

# ── Drop paths from LIVE_LIST that no longer exist ───────────────────────────
# fbi has no option to silence "Loading FAILED" — it renders that text to the
# framebuffer whenever it can't open a file. Keeping missing paths out of the
# filelist is the only way to hide it. The sweep runs ~10 min for ~360k paths
# at -P 4, so we only run it in the background reshuffle loop, not inline in
# atomic_shuffle where it would delay startup.
filter_missing() {
    local tmp="${LIVE_LIST}.filtered"
    # -P 4 (not 32): each worker does a blocking stat() over the SMB mount, and
    # a stat() against a slow or stalled share parks the process in
    # uninterruptible D state. 32 of those at once drives the load average past
    # 30 and starves the network stack — the Pi then looks like it dropped its
    # WiFi connection. 4 keeps the blast radius small. timeout caps a stalled
    # sweep instead of letting it grind for hours.
    timeout 1200 xargs -d '\n' -P 4 -n 200 -a "$LIVE_LIST" \
        sh -c 'for p; do [ -f "$p" ] && printf "%s\n" "$p"; done' _ > "$tmp" 2>/dev/null
    local rc=$?
    # timeout exits 124 when it had to kill the sweep: $tmp is then a partial
    # list, and applying it would silently drop still-valid photos. Discard it.
    if [ "$rc" -eq 124 ]; then
        echo "filter_missing: SMB sweep timed out — keeping existing list"
        rm -f "$tmp"
        return
    fi
    # Only replace if the filter produced a non-empty result — an empty output
    # means the mount is down, and we'd rather keep the stale list than wipe it.
    if [ -s "$tmp" ]; then
        local before after
        before=$(wc -l < "$LIVE_LIST")
        after=$(wc -l < "$tmp")
        cat "$tmp" > "$LIVE_LIST"
        # Keep SOURCE_FILE in sync so the next shuffle doesn't reintroduce gone files.
        [ -f "$SOURCE_FILE" ] && cat "$tmp" > "$SOURCE_FILE"
        echo "Filtered missing photos: $before → $after"
    fi
    rm -f "$tmp"
}

# ── Rotate LIVE_LIST so the resume photo is first ────────────────────────────
# Called right before each fbi launch. If RESUME_FILE exists, fbi starts at
# that photo instead of line 1. RESUME_FILE is deleted after use.
rotate_to_resume() {
    [ -f "$RESUME_FILE" ] || return
    local RESUME_PHOTO
    RESUME_PHOTO=$(cat "$RESUME_FILE" 2>/dev/null)
    rm -f "$RESUME_FILE"
    [ -z "$RESUME_PHOTO" ] && return

    local LINE_NUM
    LINE_NUM=$(grep -nxF "$RESUME_PHOTO" "$LIVE_LIST" 2>/dev/null | head -1 | cut -d: -f1)
    [ -z "$LINE_NUM" ] && return
    [ "$LINE_NUM" -le 1 ] 2>/dev/null && return

    local TMP="${LIVE_LIST}.rot"
    tail -n +"$LINE_NUM" "$LIVE_LIST" > "$TMP" 2>/dev/null && \
    head -n $((LINE_NUM - 1)) "$LIVE_LIST" >> "$TMP" 2>/dev/null && \
    cat "$TMP" > "$LIVE_LIST" && rm -f "$TMP" && \
    echo "Resuming from photo at line $LINE_NUM: $RESUME_PHOTO"
}

# ── Substitute cached photos into DISPLAY_LIST ───────────────────────────────
# The viewer reads DISPLAY_LIST. Each line is either the original photo path
# or — when the photo cache holds a copy — the cache path. Rebuilding this
# before every viewer launch is what makes the cache/photo-store switch
# seamless: it is just a different path string in the list, never a viewer
# restart. With no cache (or no manifest) DISPLAY_LIST is a copy of LIVE_LIST.
apply_cache_mapping() {
    if [ -s "$CACHE_MANIFEST" ] && [ -s "$LIVE_LIST" ]; then
        awk -F'\t' '
            NR==FNR { if (NF>=2 && $2!="") m[$1]=$2; next }
            { print ($0 in m) ? m[$0] : $0 }
        ' "$CACHE_MANIFEST" "$LIVE_LIST" > "$DISPLAY_LIST" 2>/dev/null \
            || cat "$LIVE_LIST" > "$DISPLAY_LIST" 2>/dev/null
    else
        cat "$LIVE_LIST" > "$DISPLAY_LIST" 2>/dev/null
    fi
}

# ── Initial list setup ────────────────────────────────────────────────────────
if [ -s "$SOURCE_FILE" ]; then
    echo "Found existing list. Shuffling..."
else
    echo "photo_list.txt missing or empty. Generating from Plex..."
fi
atomic_shuffle

# ── Background reshuffle every 15 minutes ────────────────────────────────────
(
  while true; do
    sleep "$RESHUFFLE_INTERVAL"
    atomic_shuffle
    filter_missing
    echo "Slideshow list reshuffled at $(date)"
  done
) &
RESHUFFLE_BG_PID=$!
echo "$RESHUFFLE_BG_PID" > "$RESHUFFLE_PID_FILE"

# ── Main slideshow loop ───────────────────────────────────────────────────────
# Viewer runs in the background; the inner monitor loop checks control files
# every 2 s so we can react to stop/duration-change without needing an external
# kill signal. Behavior depends on selected viewer (fbi, feh, eog, etc.)

# FBI_VT may be "auto" (use the detected writable VT), unset, or a number.
# If a number is set but its tty isn't writable, fall back to AVAILABLE_VT.
if [ -z "$FBI_VT" ] || [ "$FBI_VT" = "auto" ]; then
    FBI_VT="$AVAILABLE_VT"
elif [ ! -w "/dev/tty$FBI_VT" ] && [ -n "$AVAILABLE_VT" ]; then
    echo "WARNING: /dev/tty$FBI_VT not writable, using detected VT $AVAILABLE_VT instead" >&2
    FBI_VT="$AVAILABLE_VT"
fi
while true; do

    # Stop requested before next launch?
    if [ -f "$STOP_FLAG" ]; then
        rm -f "$STOP_FLAG"
        echo "Stop requested at $(date). Exiting."
        kill "$RESHUFFLE_BG_PID" 2>/dev/null
        exit 0
    fi

    # Read current duration
    if [ -f "$DURATION_FILE" ]; then
        CUR_DURATION=$(cat "$DURATION_FILE" 2>/dev/null)
        [[ "$CUR_DURATION" =~ ^[0-9]+$ ]] || CUR_DURATION="${DURATION:-5}"
    else
        CUR_DURATION="${DURATION:-5}"
    fi

    # Rotate list to resume photo (if set by server before kill/stop)
    rotate_to_resume

    # Refresh DISPLAY_LIST so the viewer picks up any newly cached photos
    # (and any LIVE_LIST changes from the background reshuffle / resume).
    apply_cache_mapping

    # Write state for web UI
    VIEWER_START=$(date +%s)
    printf '{"started_at":%s,"duration":%s,"list_path":"%s","viewer":"%s"}\n' \
        "$VIEWER_START" "$CUR_DURATION" "$LIVE_LIST" "$SELECTED_VIEWER" \
        > /tmp/frame_slideshow_state.json

    # Re-detect the DRM card on every launch when FBI_DEVICE is "auto". The
    # HDMI display card can appear after this script started; without -device
    # fbi falls back to card0 (the V3D render node — no dumb buffers) and exits
    # immediately, producing a ~5 s crash loop. Re-detecting here lets the
    # slideshow self-heal within one cycle once the right card shows up.
    if [ "$FBI_DEVICE_AUTO" = "1" ]; then
        FBI_DEVICE="$(detect_drm_device || true)"
    fi

    # Launch appropriate viewer based on environment
    case "$SELECTED_VIEWER" in
        fbi)
            # Framebuffer image viewer - runs on Linux virtual terminal without X.
            # FBI_DEVICE is auto-detected each cycle (or pinned in config.sh).
            # Empty means "let fbi pick the default card" — correct only on
            # single-card Pis; multi-card Pis (4/5) must name the HDMI card.
            fbi ${FBI_DEVICE:+-device "$FBI_DEVICE"} \
                -a -noverbose -readahead \
                -t "$CUR_DURATION" \
                -T "$FBI_VT" \
                -l "$DISPLAY_LIST" </dev/tty"$FBI_VT" 2>/dev/null &
            VIEWER_PID=$!
            echo "fbi started (PID $VIEWER_PID) on vt$FBI_VT, device=${FBI_DEVICE:-default}, ${CUR_DURATION}s per photo"
            ;;
        feh)
            # Feh - works on framebuffer or X11
            if [ "$DESKTOP_ENV" = "x11" ]; then
                export DISPLAY="${DISPLAY:-:0}"
                feh --slideshow "$CUR_DURATION" \
                    --on-last-slide quit \
                    --fullscreen \
                    -f "$DISPLAY_LIST" 2>/dev/null &
            else
                # Fallback for console: feh on framebuffer
                feh -Z --slideshow "$CUR_DURATION" \
                    --on-last-slide quit \
                    -f "$DISPLAY_LIST" 2>/dev/null &
            fi
            VIEWER_PID=$!
            echo "feh started (PID $VIEWER_PID) at ${CUR_DURATION}s per photo"
            ;;
        eog)
            # Eye of GNOME - X11 only
            export DISPLAY="${DISPLAY:-:0}"
            eog --slideshow \
                --slideshow-timeout="$((CUR_DURATION * 1000))" \
                "$(head -1 "$DISPLAY_LIST")" 2>/dev/null &
            VIEWER_PID=$!
            echo "eog started (PID $VIEWER_PID) at ${CUR_DURATION}s per photo"
            ;;
        display)
            # ImageMagick display - X11 only
            export DISPLAY="${DISPLAY:-:0}"
            display -geometry +0+0 \
                -delay "$CUR_DURATION"x1 \
                "@$DISPLAY_LIST" 2>/dev/null &
            VIEWER_PID=$!
            echo "display started (PID $VIEWER_PID) at ${CUR_DURATION}s per photo"
            ;;
        *)
            echo "ERROR: Unknown viewer '$SELECTED_VIEWER'" >&2
            kill "$RESHUFFLE_BG_PID" 2>/dev/null
            exit 1
            ;;
    esac

    # Monitor viewer while it runs
    while kill -0 "$VIEWER_PID" 2>/dev/null; do
        sleep 2

        # Stop requested while viewer is running?
        if [ -f "$STOP_FLAG" ]; then
            echo "Stop requested. Killing $SELECTED_VIEWER PID $VIEWER_PID."
            kill "$VIEWER_PID" 2>/dev/null
            wait "$VIEWER_PID" 2>/dev/null
            rm -f "$STOP_FLAG"
            kill "$RESHUFFLE_BG_PID" 2>/dev/null
            exit 0
        fi

        # Rescan rebuilt the master list? Restart viewer so the new list is loaded.
        if [ -f "$RESCAN_FLAG" ]; then
            rm -f "$RESCAN_FLAG"
            echo "Photo list was rescanned — restarting viewer to pick up new photos."
            kill "$VIEWER_PID" 2>/dev/null
            wait "$VIEWER_PID" 2>/dev/null
            break
        fi

        # Duration changed? Kill viewer; outer loop restarts with new value + resume position.
        if [ -f "$DURATION_FILE" ]; then
            NEW_DUR=$(cat "$DURATION_FILE" 2>/dev/null)
            if [[ "$NEW_DUR" =~ ^[0-9]+$ ]] && [ "$NEW_DUR" != "$CUR_DURATION" ]; then
                echo "Duration changed from ${CUR_DURATION}s to ${NEW_DUR}s — restarting viewer."
                # Record the currently-showing photo so the next launch resumes there
                # instead of starting over from line 1 of the shuffled list.
                TOTAL=$(wc -l < "$LIVE_LIST" 2>/dev/null)
                if [ -n "$TOTAL" ] && [ "$TOTAL" -gt 0 ] && [ "$CUR_DURATION" -gt 0 ]; then
                    ELAPSED=$(( $(date +%s) - VIEWER_START ))
                    IDX=$(( (ELAPSED / CUR_DURATION) % TOTAL ))
                    sed -n "$((IDX + 1))p" "$LIVE_LIST" > "$RESUME_FILE"
                fi
                kill "$VIEWER_PID" 2>/dev/null
                wait "$VIEWER_PID" 2>/dev/null
                break
            fi
        fi
    done

    echo "$SELECTED_VIEWER (PID $VIEWER_PID) exited at $(date). Restarting in 2s..."
    sleep 2
done
