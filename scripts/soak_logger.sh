#!/bin/bash
# ── PapaFrame soak-test logger ────────────────────────────────────
# Runs alongside papamonitor. Captures process health, screen state,
# reboots, and papaframe-specific errors every 2 minutes.
# Log: ~/papaframe/soak_test.log
set -euo pipefail

LOGFILE="$HOME/papaframe/soak_test.log"
BOOT_ID_FILE="$HOME/papaframe/.soak_boot_id"
INTERVAL=120  # seconds

# ── Detect reboot ─────────────────────────────────────────────────
current_boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo "unknown")
if [ -f "$BOOT_ID_FILE" ]; then
    prev_boot_id=$(cat "$BOOT_ID_FILE")
    if [ "$current_boot_id" != "$prev_boot_id" ]; then
        boot_time=$(who -b 2>/dev/null | awk '{print $3, $4}' || uptime -s 2>/dev/null)
        prev_uptime=""
        [ -f "$HOME/papaframe/.soak_last_uptime" ] && prev_uptime=$(cat "$HOME/papaframe/.soak_last_uptime")
        echo "$(date '+%F %T') | *** REBOOT DETECTED *** boot_time=$boot_time prev_uptime=$prev_uptime prev_boot=$prev_boot_id" >> "$LOGFILE"

        # Capture last few lines of kernel log around the crash/shutdown
        echo "$(date '+%F %T') | REBOOT_JOURNAL_PREV_BOOT:" >> "$LOGFILE"
        journalctl -b -1 --no-pager -n 30 -p warning 2>/dev/null | sed 's/^/  | /' >> "$LOGFILE" || echo "  | (no previous boot journal)" >> "$LOGFILE"
    fi
fi
echo "$current_boot_id" > "$BOOT_ID_FILE"

# ── Helper: check if a process is alive ───────────────────────────
proc_status() {
    local name="$1" pattern="$2"
    local pid
    pid=$(pgrep -f "$pattern" 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        local rss
        rss=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
        echo "${name}=up(pid=$pid,rss=${rss}KB)"
    else
        echo "${name}=DOWN"
    fi
}

# ── Helper: screen/DPMS state ────────────────────────────────────
screen_state() {
    # Check DPMS property via modetest (read-only, quick)
    local dpms
    dpms=$(modetest -M vc4 -c -p 2>/dev/null | awk '/DPMS/{getline; getline; print $2}')
    local fbcon_bound
    fbcon_bound=$(cat /sys/class/vtconsole/vtcon1/bind 2>/dev/null || echo "?")
    echo "dpms=$dpms,fbcon=$fbcon_bound"
}

# ── Helper: papaframe API status ─────────────────────────────────
api_status() {
    local resp
    resp=$(curl -sf --max-time 5 http://localhost:8000/api/status 2>/dev/null)
    if [ -z "$resp" ]; then
        echo "api=UNREACHABLE"
        return
    fi
    local running paused sched_off
    running=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('running','?'))" 2>/dev/null)
    paused=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('paused_by_schedule','?'))" 2>/dev/null)
    sched_off=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('screen_scheduled_off','?'))" 2>/dev/null)
    echo "api=ok,running=$running,paused=$paused,sched_off=$sched_off"
}

# ── Helper: recent errors from papaframe server log ──────────────
recent_errors() {
    local count
    count=$(grep -c -iE 'error|exception|traceback|fail' "$HOME/papaframe/frame_display.log" 2>/dev/null || echo 0)
    local last_err
    last_err=$(grep -iE 'error|exception|traceback|fail' "$HOME/papaframe/frame_display.log" 2>/dev/null | tail -1 | cut -c1-120)
    echo "err_count=$count,last_err='${last_err:-none}'"
}

# ── Helper: network health ────────────────────────────────────────
net_health() {
    local gw_ok=0 dns_ok=0
    ping -c1 -W3 192.168.1.1 >/dev/null 2>&1 && gw_ok=1
    ping -c1 -W3 8.8.8.8 >/dev/null 2>&1 && dns_ok=1
    echo "gw=$gw_ok,inet=$dns_ok"
}

# ── Main loop ─────────────────────────────────────────────────────
echo "$(date '+%F %T') | soak_logger started (pid $$, interval=${INTERVAL}s)" >> "$LOGFILE"

while true; do
    ts=$(date '+%F %T')
    uptime_s=$(awk '{print int($1)}' /proc/uptime)
    # Save uptime so we can report it after a crash
    echo "$uptime_s" > "$HOME/papaframe/.soak_last_uptime"

    server=$(proc_status "server" "server\.py")
    frame=$(proc_status "frame_sh" "start_frame\.sh")
    fbi_proc=$(proc_status "fbi" "fbi.*frame_display")
    monitor_proc=$(proc_status "monitor" "monitor\.sh")

    screen=$(screen_state)
    api=$(api_status)
    net=$(net_health)

    # Count tty1 processes (detect getty restart churn)
    tty1_procs=$(pgrep -ct tty1 2>/dev/null || echo 0)

    # Schedule flag
    sched_flag="off_flag=$([ -f /tmp/frame_schedule_off ] && echo 'yes' || echo 'no')"

    printf "%s | uptime=%ds | %s | %s | %s | %s | %s | %s | %s | tty1_procs=%s\n" \
        "$ts" "$uptime_s" \
        "$server" "$frame" "$fbi_proc" "$monitor_proc" \
        "$screen" "$api" "$net" "$tty1_procs" >> "$LOGFILE"

    # Log errors separately if any processes are down unexpectedly
    if echo "$server" | grep -q "DOWN"; then
        echo "$ts | *** ALERT: server.py is DOWN ***" >> "$LOGFILE"
    fi

    # Check for OOM kills since last check
    oom=$(dmesg -T 2>/dev/null | grep -c "Out of memory" || echo 0)
    if [ "$oom" -gt 0 ]; then
        echo "$ts | *** OOM KILLS DETECTED ($oom) ***" >> "$LOGFILE"
        dmesg -T 2>/dev/null | grep "Out of memory" | tail -3 | sed "s/^/$ts |   /" >> "$LOGFILE"
    fi

    sleep "$INTERVAL"
done
