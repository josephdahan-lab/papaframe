#!/bin/bash
# PapaFrame Wi-Fi watchdog. Installed by scripts/harden_pi.sh and run every
# 5 minutes, as root, by frame-wifi-watchdog.timer.
#
# Each run checks that the default gateway (or, failing that, 1.1.1.1)
# answers ping. Consecutive failures escalate:
#   1-2  failures: ask NetworkManager to bring the Wi-Fi connection up again
#                  (this also clears NM's "gave up after N retries" block)
#   3-11 failures: turn the Wi-Fi radio off and on
#   12   failures (~1 hour offline): reboot
# The failure count lives in /run, so it starts from zero after every boot.

STATE=/run/frame-wifi-watchdog.fails
LOG=/var/log/frame-wifi-watchdog.log
REBOOT_AFTER=12

log() { echo "$(date '+%F %T') | $1" >> "$LOG"; }

online() {
    local gw
    gw=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
    [ -n "$gw" ] && ping -c 2 -W 5 "$gw" >/dev/null 2>&1 && return 0
    ping -c 2 -W 5 1.1.1.1 >/dev/null 2>&1
}

fails=$(cat "$STATE" 2>/dev/null || echo 0)

if online; then
    [ "$fails" -gt 0 ] && log "back online after $fails failed check(s)"
    echo 0 > "$STATE"
    exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE"

conn=$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless" {print $1; exit}')

if [ "$fails" -ge "$REBOOT_AFTER" ]; then
    log "offline for $fails checks, rebooting"
    sync
    systemctl reboot
elif [ "$fails" -ge 3 ]; then
    log "offline ($fails), cycling Wi-Fi radio"
    nmcli radio wifi off; sleep 5; nmcli radio wifi on
    sleep 10
    [ -n "$conn" ] && nmcli connection up "$conn" >/dev/null 2>&1
else
    log "offline ($fails), reconnecting '${conn:-?}'"
    [ -n "$conn" ] && nmcli connection up "$conn" >/dev/null 2>&1
fi
