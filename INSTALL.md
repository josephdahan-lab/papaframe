# Installing PapaFrame on a Raspberry Pi

A walkthrough for taking a fresh Pi from "just unboxed" to a working
network-controllable picture frame. Aimed at Pi Zero / Zero 2 W / 3 / 4 / 5
running Raspberry Pi OS Lite (Bookworm or Bullseye).

> **TL;DR** — flash Pi OS Lite, SSH in, clone the repo, run
> `sudo bash scripts/install.sh`, edit `config.sh` to point at your photos,
> reboot. Skip to [§5](#5-run-the-installer) if you've already got the OS
> booted and SSH'd in.

---

## What you need

**Hardware**
- A Raspberry Pi (any model with HDMI). A Pi Zero 2 W is the sweet spot for
  cost / size; original Pi Zero (armv6, 512 MB) works but is **slow** — see
  the [Pi Zero notes](#appendix-original-pi-zero-armv6-notes) at the bottom.
- microSD card (8 GB+).
- A display connected by HDMI (use the mini-HDMI adapter on Zero/Zero 2 W).
- Network — Wi-Fi is fine, Ethernet works on models that have it.

**Photos**
- Any of: a folder on the Pi's SD card, a USB drive plugged into the Pi, or
  a network share (NAS / SMB / NFS). Section [§7](#7-photos-on-a-nas) covers
  the network-share path.

---

## 1. Flash Raspberry Pi OS Lite

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Pick
**Raspberry Pi OS Lite (64-bit)** for Pi 3 and newer; **Lite (32-bit)** for
the original Pi Zero / Pi 1.

Click the gear icon ⚙️ before writing and set:
- **Hostname** — e.g. `papaframe2` (whatever you want it to be on the LAN)
- **Username + password** — remember these
- **Wi-Fi** — SSID and password
- **SSH** — enable, password auth is fine for a LAN-only frame
- **Locale / keyboard** — your timezone and layout

Write the card, boot the Pi, wait ~60 s for the first-boot expansion to
finish.

---

## 2. SSH in

```bash
ssh <username>@<hostname>.local
```

If `.local` resolution doesn't work on your network, find the IP from your
router and SSH to that. Update the OS while you're there:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## 3. Clone the repo

```bash
sudo apt install -y git
git clone https://github.com/josephdahan-lab/papaframe.git
cd papaframe
```

The repo lives in your home directory (`~/papaframe`). The installer will
respect that path — it does not move or symlink anything.

---

## 4. (Optional) Set up your photo source

The default `config.sh` points at `$HOME/Pictures`. You have three options:

**a. Photos on the SD card.** Easiest. Drop them in `~/Pictures` (use `scp`
   or `rsync` from your laptop). Skip to [§5](#5-run-the-installer).

**b. Photos on a USB drive.** Plug it in, find it with `lsblk`, and add an
   entry to `/etc/fstab` so it auto-mounts. Then point `PHOTO_DIRS` at the
   mount point (e.g. `/mnt/usb/Pictures`).

**c. Photos on a NAS over SMB or NFS.** See [§7](#7-photos-on-a-nas) — do it
   *before* the first slideshow run so the photo list builds successfully.

---

## 5. Run the installer

```bash
sudo bash scripts/install.sh
```

The script is idempotent — re-run it any time to recover from a botched
install or to migrate after moving the repo. It is also **hardware-aware**:
it detects the board's architecture and RAM and tailors the install (see the
[Pi Zero appendix](#appendix-original-pi-zero-armv6-notes)). It prints what it
detected and which path it's taking before step 1. It does:

| Step | What                                                                      |
| ---- | ------------------------------------------------------------------------- |
| 1    | `apt install` — `fbi`, Python venv tools (+ Pillow build deps on armv6)   |
| 2    | Adds you to the `video` group                                     |
| 3    | Creates `.venv/` next to `server.py` and installs requirements    |
| 4    | Installs `/usr/local/bin/papaframe-screen` + passwordless sudoers |
| 5    | `systemctl set-default multi-user.target` (no graphical login)    |
| 6    | Autologin drop-in at `getty@tty1.service.d/autologin.conf`        |
| 7    | Adds an `exec` line to `~/.bash_profile` for the slideshow runner |
| 8    | Installs + enables `papaframe-server.service` for the Flask UI    |

It does **not** touch `config.sh`, your photos, or anything network-related.

When it finishes:

```bash
nano ~/papaframe/config.sh         # set PHOTO_DIRS, port, schedule, etc.
sudo reboot
```

After the reboot:

- The slideshow appears on the HDMI display.
- The web UI is at `http://<hostname>.local:8000/` (or the Pi's IP:8000).

---

## 6. Why two systemd things?

Because the slideshow viewer (`fbi`) needs a *controlling* TTY to take
ownership of the framebuffer, not just a writable one. A regular systemd
service has no controlling TTY, so `fbi` would crash with
`ioctl VT_SETMODE: Operation not permitted`.

The workaround is the standard kiosk pattern:

1. `getty@tty1` autologins your user (drop-in step 6).
2. Bash loads `~/.bash_profile`, which `exec`s `start_frame.sh` only when
   it sees `tty == /dev/tty1` (step 7). `fbi` then inherits tty1 as its
   ctty.
3. The Flask web server runs as a normal systemd unit (step 8) — it
   doesn't need a TTY.

If you ever see the slideshow show a *bash prompt* instead of photos, the
`.bash_profile` exec is the thing to inspect.

---

## 7. Photos on a NAS

PapaFrame is happy reading photos from a network mount. The trick is to use
a **systemd automount** so the slideshow can boot even when the NAS is
temporarily down, and self-heals after the NAS reboots.

Example for an SMB / CIFS share at `//nas.local/photos`:

```bash
sudo apt install -y cifs-utils
sudo mkdir -p /mnt/photos

# Credentials file, root-only readable
sudo tee /etc/samba/papaframe.creds >/dev/null <<EOF
username=YOUR_NAS_USER
password=YOUR_NAS_PASSWORD
EOF
sudo chmod 600 /etc/samba/papaframe.creds

# /etc/fstab line — note the x-systemd flags
sudo tee -a /etc/fstab >/dev/null <<EOF
//nas.local/photos /mnt/photos cifs credentials=/etc/samba/papaframe.creds,uid=1000,gid=1000,iocharset=utf8,vers=3.0,x-systemd.automount,_netdev,x-systemd.mount-timeout=30,x-systemd.idle-timeout=600 0 0
EOF

sudo systemctl daemon-reload
sudo systemctl start mnt-photos.automount
ls /mnt/photos                     # should trigger the lazy mount
```

Then in `~/papaframe/config.sh`:

```bash
PHOTO_DIRS="/mnt/photos/Pictures"
```

For NFS, swap `cifs-utils` → `nfs-common` and the `/etc/fstab` line for the
appropriate NFS mount string (the same `x-systemd.automount,_netdev,…`
flags apply).

If the slideshow shows "Loading FAILED" for every photo, your share isn't
mounted — `systemctl status mnt-photos.automount mnt-photos.mount` will
tell you why.

---

## 8. Verifying things work

```bash
# Web server status
systemctl status papaframe-server

# Live server log
journalctl -u papaframe-server -f

# Slideshow runner log (only the most recent run)
tail -f ~/papaframe/frame_display.log

# What's the current photo?
curl -s http://localhost:8000/api/currentphoto | python3 -m json.tool
```

The admin page at `http://<frame>:8000/admin` lets you edit `config.sh`
from the browser, rebuild the photo list, and tweak the on/off schedule.

---

## 9. Updating PapaFrame

```bash
cd ~/papaframe
git pull
~/papaframe/.venv/bin/pip install -r requirements.txt   # if requirements changed
sudo systemctl restart papaframe-server
# slideshow restarts itself when /api/restart is hit, or:
killall start_frame.sh   # autologin loop on tty1 will respawn it
```

---

## 10. Uninstall

```bash
sudo systemctl disable --now papaframe-server.service
sudo rm /etc/systemd/system/papaframe-server.service
sudo rm /etc/sudoers.d/papaframe-screen
sudo rm /usr/local/bin/papaframe-screen
sudo rm /etc/systemd/system/getty@tty1.service.d/autologin.conf
sudo rmdir /etc/systemd/system/getty@tty1.service.d 2>/dev/null
sudo systemctl set-default graphical.target   # or leave at multi-user
sudo systemctl daemon-reload
# Remove the slideshow exec from ~/.bash_profile by hand.
rm -rf ~/papaframe
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| Black screen, login prompt visible | autologin drop-in missing | Re-run installer |
| Black screen, *bash prompt* visible | `.bash_profile` exec missing | Re-run installer or check the file |
| `drm: no dumb buffer support` in dmesg | `FBI_DEVICE` picked the wrong card | Set `FBI_DEVICE=/dev/dri/card1` (Pi 4/5) or `card0` (Pi Zero/3) in `config.sh` |
| Web UI loads but every photo shows "Loading FAILED" | photo paths point at an unmounted share | `mount /mnt/photos`, then click "Rebuild photo list" in `/admin` |
| `feh` flashes and exits | `FORCE_VIEWER=auto` chose feh on console | Set `FORCE_VIEWER=fbi` in `config.sh` |
| Web UI 502 / not reachable | server crashed | `journalctl -u papaframe-server -n 200` |
| Screen on/off does nothing | sudoers rule missing or connector path wrong | `sudo /usr/local/bin/papaframe-screen off` to test directly |
| Pillow install hangs forever | building from source on slow Pi | Be patient, or `sudo apt install python3-pillow` and recreate the venv with `--system-site-packages` |
| pip `IncompleteRead` / `Connection broken` | network dropped mid-download | Re-run the installer — pip retries 10×; persistent failures mean the network is cutting the connection |

---

## Appendix: Original Pi Zero (armv6) notes

The first-gen Pi Zero / Zero W is **armv6**, single-core, 512 MB RAM — the
slowest board PapaFrame supports. pip publishes no wheels for armv6, so
Pillow has to be compiled from source.

**The installer detects this automatically.** When `scripts/install.sh`
sees an armv6 board it will, with no extra steps from you:

- install Debian's prebuilt `python3-pil` / `python3-flask` /
  `python3-psutil` / `python3-pycountry` and build the venv with
  `--system-site-packages`, so those don't all compile from source;
- grow swap to 512 MB via `dphys-swapfile` so the Pillow C build doesn't
  get OOM-killed;
- skip `reverse_geocoder` — it depends on **scipy**, which has no armv6
  wheels and won't realistically build on a Pi Zero (it OOMs / runs for
  hours and still fails). The offline location lookup feature simply stays
  disabled; the server already handles `reverse_geocoder` being absent (see
  [server.py:23-29](server.py#L23-L29)).

So on an armv6 board the install is still just
`sudo bash scripts/install.sh` — just expect the Pillow build to take a
while. The installer prints `plan: armv6 — …` in its header so you can
confirm it took this path.

**Pi Zero 2 W** (armv7l/aarch64, quad-core, same 512 MB) has none of these
problems — wheels exist, nothing compiles, and `reverse_geocoder` installs
cleanly. Strongly recommended over the original Zero if you have the
choice.
