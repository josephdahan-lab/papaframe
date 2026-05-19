#!/bin/bash
# ── PapaFrame Pi-hardening ────────────────────────────────────────────────────
# Run once, as root:   sudo bash scripts/harden_pi.sh
#
# Addresses the freeze where the Pi locks up hard and needs a manual power-cycle.
# Root cause: the CIFS photo share (//hpenvy/plex, over WiFi) wedges its kernel
# "deferredclose" workers in uninterruptible D-state under sustained load; once
# enough pile up the CIFS workqueue stalls and the Pi can lock up.
#
# This script makes three system-level changes (the indexer throttle is a
# separate code change in server.py):
#   1. Hardware watchdog  — a frozen Pi self-reboots instead of needing a hand.
#   2. Persistent journald + a 1-min health sampler — so the next freeze leaves
#      evidence (right now the journal is RAM-only and wiped on every reboot).
#   3. CIFS closetimeo=0  — disables deferred-close handle caching, the exact
#      mechanism whose workers were seen wedged.
#
# Idempotent: safe to re-run. Backs up every file it edits.
set -u

if [ "$(id -u)" -ne 0 ]; then
    echo "Must run as root:  sudo bash scripts/harden_pi.sh" >&2
    exit 1
fi

ts=$(date +%Y%m%d-%H%M%S)
backup() { [ -f "$1" ] && cp -a "$1" "$1.papaframe-bak.$ts" && echo "  backed up $1 -> $1.papaframe-bak.$ts"; }

echo "=== 1/3  Hardware watchdog ==="
# /dev/watchdog already exists on this Pi, so the bcm2835 timer is live — we
# just tell systemd (PID 1) to pet it. If systemd ever stops being scheduled
# (a hard freeze), the hardware resets the board after RuntimeWatchdogSec.
SC=/etc/systemd/system.conf
backup "$SC"
sed -i 's/^#\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15s/' "$SC"
sed -i 's/^#\?RebootWatchdogSec=.*/RebootWatchdogSec=2min/'  "$SC"
grep -qE '^RuntimeWatchdogSec='  "$SC" || echo 'RuntimeWatchdogSec=15s' >> "$SC"
grep -qE '^RebootWatchdogSec='   "$SC" || echo 'RebootWatchdogSec=2min' >> "$SC"
echo "  $(grep -E '^RuntimeWatchdogSec|^RebootWatchdogSec' "$SC" | tr '\n' ' ')"
systemctl daemon-reexec
sleep 1
echo "  watchdog now: timeout=$(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null)s state=$(cat /sys/class/watchdog/watchdog0/state 2>/dev/null)"

echo "=== 2/3  Persistent journald + health sampler ==="
JC=/etc/systemd/journald.conf
backup "$JC"
sed -i 's/^#\?Storage=.*/Storage=persistent/' "$JC"
grep -qE '^Storage='  "$JC" || echo 'Storage=persistent' >> "$JC"
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
systemctl restart systemd-journald
journalctl --flush
echo "  journald: $(grep -E '^Storage' "$JC")  (logs now survive reboot)"

# 1-minute vitals sample. After a freeze, `journalctl -t papaframe-health`
# shows the trail; the last line before the gap is the state at freeze time.
cat > /usr/local/bin/papaframe-health.sh <<'EOF'
#!/bin/bash
read -r _ load1 _ < /proc/loadavg
mem=$(free -m | awk '/^Mem:/{printf "%d/%dMB", $3, $2}')
temp=$(awk '{printf "%.1fC", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
thr=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
# D-state procs and wedged CIFS workers — the metric that precedes the freeze.
dproc=$(ps -eo stat= | grep -c '^D')
cifsd=$(ps -eo stat=,comm= | awk '$1 ~ /^D/ && $2 ~ /kworker/' | wc -l)
echo "load=$load1 mem=$mem temp=$temp throttled=$thr dproc=$dproc cifs_dworkers=$cifsd"
EOF
chmod +x /usr/local/bin/papaframe-health.sh

cat > /etc/systemd/system/papaframe-health.service <<'EOF'
[Unit]
Description=PapaFrame health sample
[Service]
Type=oneshot
ExecStart=/usr/local/bin/papaframe-health.sh
SyslogIdentifier=papaframe-health
EOF

cat > /etc/systemd/system/papaframe-health.timer <<'EOF'
[Unit]
Description=Sample PapaFrame vitals every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now papaframe-health.timer
echo "  health sampler armed — view with:  journalctl -t papaframe-health"

echo "=== 3/3  CIFS closetimeo=0 ==="
# Deferred-close keeps file handles open briefly after close() to optimise
# re-opens. Under our access pattern (hundreds of thousands of one-shot opens)
# it just queues unbounded close work; closetimeo=0 makes closes synchronous so
# the deferredclose workers stop piling up in D-state.
if grep -qE '//hpenvy/plex.*closetimeo=0' /etc/fstab; then
    echo "  closetimeo=0 already present in /etc/fstab — skipping"
else
    backup /etc/fstab
    sed -i 's#\(//hpenvy/plex[[:space:]]\+/mnt/plex[[:space:]]\+cifs[[:space:]]\+\)#\1closetimeo=0,#' /etc/fstab
    echo "  $(grep //hpenvy/plex /etc/fstab)"
    systemctl daemon-reload
fi

echo
echo "=== DONE ==="
echo "Watchdog + persistent logging are active now."
echo "The new mount option applies on next access after an unmount:"
echo "    sudo umount /mnt/plex 2>/dev/null; ls /mnt/plex >/dev/null"
echo "(or it simply takes effect after the next reboot)."
