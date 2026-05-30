#!/bin/bash
# ── PapaFrame soak-test email report ──────────────────────────────
# Analyzes soak_test.log + monitor.log and emails a summary.
# Designed to run via cron every 6–12 hours.
set -euo pipefail

EMAIL_TO="joseph.dahan@gmail.com"
HOSTNAME=$(hostname)
SOAK_LOG="$HOME/papaframe/soak_test.log"
MON_LOG="$HOME/monitor/monitor.log"
LAST_REPORT_FILE="$HOME/papaframe/.soak_last_report_line"

# ── Gather data ───────────────────────────────────────────────────
UPTIME=$(uptime -p)
UPTIME_SINCE=$(uptime -s)
BOOT_COUNT=$(journalctl --list-boots 2>/dev/null | wc -l)
DISK=$(df -h / | awk 'NR==2{print $5 " used (" $4 " avail)"}')

# Temperature: last 30 readings from monitor.log
TEMPS=$(tail -30 "$MON_LOG" 2>/dev/null | grep -oP 'temp=\K[0-9.]+' || echo "n/a")
TEMP_MIN=$(echo "$TEMPS" | sort -n | head -1)
TEMP_MAX=$(echo "$TEMPS" | sort -n | tail -1)
TEMP_LAST=$(echo "$TEMPS" | tail -1)

# Memory: last 30 readings
MEM_PCTS=$(tail -30 "$MON_LOG" 2>/dev/null | grep -oP 'mem=.*?\(\K[0-9]+' || echo "n/a")
MEM_MIN=$(echo "$MEM_PCTS" | sort -n | head -1)
MEM_MAX=$(echo "$MEM_PCTS" | sort -n | tail -1)
MEM_LAST=$(echo "$MEM_PCTS" | tail -1)

# WiFi: last 30 readings
WIFI_VALS=$(tail -30 "$MON_LOG" 2>/dev/null | grep -oP 'wifi=\K-?[0-9]+' || echo "n/a")
WIFI_LAST=$(echo "$WIFI_VALS" | tail -1)

# Process status right now
proc_check() {
    pgrep -f "$1" >/dev/null 2>&1 && echo "UP" || echo "DOWN"
}
SERVER_STATUS=$(proc_check "server\.py")
FBI_STATUS=$(proc_check "fbi.*frame_display")
FRAME_STATUS=$(proc_check "start_frame\.sh")
SOAK_STATUS=$(proc_check "soak_logger")
MONITOR_STATUS=$(proc_check "monitor\.sh")

# Soak log analysis: count events since last report
LAST_LINE=0
[ -f "$LAST_REPORT_FILE" ] && LAST_LINE=$(cat "$LAST_REPORT_FILE" 2>/dev/null || echo 0)
CURRENT_LINES=$(wc -l < "$SOAK_LOG" 2>/dev/null || echo 0)

soak_since() { tail -n +"$((LAST_LINE+1))" "$SOAK_LOG" 2>/dev/null | grep -c "$1" 2>/dev/null || true; }
REBOOTS=$(soak_since "REBOOT DETECTED"); REBOOTS=${REBOOTS:-0}
OOMS=$(soak_since "OOM KILLS");           OOMS=${OOMS:-0}
ALERTS=$(soak_since "ALERT");             ALERTS=${ALERTS:-0}
SERVER_DOWNS=$(soak_since "server=DOWN"); SERVER_DOWNS=${SERVER_DOWNS:-0}
FBI_DOWNS=$(soak_since "fbi=DOWN");       FBI_DOWNS=${FBI_DOWNS:-0}

# Reboot details if any
REBOOT_DETAIL=""
if [ "$REBOOTS" -gt 0 ]; then
    REBOOT_DETAIL=$(tail -n +"$((LAST_LINE+1))" "$SOAK_LOG" 2>/dev/null | grep "REBOOT DETECTED" | tail -3)
fi

# Save line count for next run
echo "$CURRENT_LINES" > "$LAST_REPORT_FILE"

# ── Classify status ──────────────────────────────────────────────
STATUS="ALL CLEAR"
ISSUES=""

if [ "$REBOOTS" -gt 0 ]; then
    STATUS="ACTION NEEDED"
    ISSUES="${ISSUES}\n- $REBOOTS reboot(s) detected"
fi
if [ "$OOMS" -gt 0 ]; then
    STATUS="ACTION NEEDED"
    ISSUES="${ISSUES}\n- OOM kills detected"
fi
if [ "$SERVER_STATUS" = "DOWN" ]; then
    STATUS="ACTION NEEDED"
    ISSUES="${ISSUES}\n- server.py is DOWN right now"
fi
if [ "$STATUS" = "ALL CLEAR" ]; then
    _mem=${MEM_MAX:-0}; _mem=${_mem%%[!0-9]*}; _mem=${_mem:-0}
    _temp=${TEMP_MAX:-0}; _temp=${_temp%%.*}; _temp=${_temp:-0}
    [ "$_mem" -gt 85 ] 2>/dev/null && STATUS="HEADS UP" && ISSUES="${ISSUES}\n- Memory peaked at ${MEM_MAX}%"
    [ "$_temp" -gt 75 ] 2>/dev/null && STATUS="HEADS UP" && ISSUES="${ISSUES}\n- Temperature peaked at ${TEMP_MAX}C"
    [ "$ALERTS" -gt 5 ] 2>/dev/null && STATUS="HEADS UP" && ISSUES="${ISSUES}\n- $ALERTS alerts in soak log"
    [ "$SERVER_DOWNS" -gt 2 ] 2>/dev/null && STATUS="HEADS UP" && ISSUES="${ISSUES}\n- server.py was DOWN $SERVER_DOWNS times"
fi

# ── Build email ──────────────────────────────────────────────────
SUBJECT="[$HOSTNAME] Soak report: $STATUS"

BODY="PapaFrame Soak Test Report
$(date '+%F %T')
==============================

STATUS: $STATUS

System
------
  Uptime:     $UPTIME (since $UPTIME_SINCE)
  Boots:      $BOOT_COUNT (total in journal)
  Disk:       $DISK

Temperatures (last 30 readings)
------
  Range:      ${TEMP_MIN}C - ${TEMP_MAX}C
  Current:    ${TEMP_LAST}C

Memory (last 30 readings)
------
  Range:      ${MEM_MIN}% - ${MEM_MAX}%
  Current:    ${MEM_LAST}%

WiFi
------
  Signal:     ${WIFI_LAST}dBm

Load Average
------
$(cat /proc/loadavg | awk '{printf "  1min: %s  5min: %s  15min: %s\n", $1, $2, $3}')

Top CPU consumers
------
$(ps aux --sort=-%cpu | awk 'NR<=6{printf "  %-6s %-4s %-4s %s\n", $1, $3"%", $4"%", $11}' | head -6)

Top memory consumers
------
$(ps aux --sort=-%mem | awk 'NR<=6{printf "  %-6s %-4s %-6s %s\n", $1, $4"%", int($6/1024)"MB", $11}' | head -6)

All processes
------
$(ps -eo user,pid,%cpu,%mem,rss,etime,comm --sort=-%cpu | head -25)

Key services (right now)
------
  server.py:      $SERVER_STATUS
  start_frame.sh: $FRAME_STATUS
  fbi:            $FBI_STATUS
  monitor.sh:     $MONITOR_STATUS
  soak_logger:    $SOAK_STATUS

Events since last report
------
  Reboots:        $REBOOTS
  OOM kills:      $OOMS
  Alerts:         $ALERTS
  server.py DOWN: $SERVER_DOWNS
  fbi DOWN:       $FBI_DOWNS"

if [ -n "$ISSUES" ]; then
    BODY="${BODY}

Issues
------$(echo -e "$ISSUES")"
fi

if [ -n "$REBOOT_DETAIL" ]; then
    BODY="${BODY}

Reboot details
------
$REBOOT_DETAIL"
fi

# Last 5 soak log entries for context
BODY="${BODY}

Recent soak log
------
$(tail -5 "$SOAK_LOG" 2>/dev/null || echo "(empty)")"

# ── Send ─────────────────────────────────────────────────────────
printf "Subject: %s\n\n%s\n" "$SUBJECT" "$BODY" | msmtp "$EMAIL_TO" 2>/dev/null

echo "$(date '+%F %T') | Soak report sent: $STATUS" >> "$SOAK_LOG"
