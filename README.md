# PapaFrame

A Linux-based digital picture frame that turns a Raspberry Pi (or any Linux box
with a display) into a network-controllable slideshow. A small bash runner
drives the slideshow directly on the framebuffer — no desktop environment
required — while a Flask web server gives you a phone-friendly dashboard for
start/stop, duration, filtering by year or country, EXIF/GPS inspection,
photo sync from NAS, and scheduled screen on/off.

```
   ┌──────────────────┐         ┌──────────────────────┐
   │ start_frame.sh   │ <─────  │ /tmp/*.flag, *.json  │
   │  shuf | fbi/feh  │         │ control files        │
   └────────┬─────────┘         └──────────┬───────────┘
            │ draws to                     │ written by
            ▼                              │
       /dev/fb0 / X11                ┌─────┴───────┐
                                     │  server.py  │ ◄── HTTP from your phone
                                     │   (Flask)   │
                                     └─────────────┘
                                           ▲
                                           │ triggers
                                     ┌─────┴───────────┐
                                     │ photo_sync.py    │ ◄── cron (3 AM daily)
                                     │ NAS → USB resize │
                                     └─────────────────┘
```

Both processes share a single config file — `config.sh` — which the web admin
page can edit in place.

---

## Features

**Display**
- Console mode via `fbi` on the Linux framebuffer (no X required, perfect for
  a kiosked Pi).
- X11 mode via `feh`, `eog`, or `display` if a desktop is detected.
- Auto-detects desktop environment, framebuffer device, and writable VT;
  override with `FORCE_VIEWER` / `FBI_VT`.
- Handles Pi 4/5 quirk where HDMI sits on `/dev/dri/card1` rather than `card0`.

**Slideshow**
- Recursive scan of one or more photo roots (`PHOTO_DIRS`, colon-separated).
- Optional local **photo cache** — keeps upcoming photos on local disk
  (resized to 1920×1080 by default), filled overnight. The slideshow shows
  cached copies transparently and keeps running even if the photo store
  (e.g. a NAS) goes offline. Size, compression, and quality are configurable.
- Background reshuffle on a configurable interval (default 15 min).
- Background pass to drop dead paths from the live list — important for
  network shares where files come and go.
- Resume position when duration changes mid-show, so the viewer doesn't snap
  back to photo #1 every time you tweak the speed.
- Year filter (`/2017/`-style path matching) and country filter (reverse-
  geocoded from photo GPS).

**Photo Sync (v2.0)**
- Automatic daily sync from a NAS (or any mounted source) to local USB storage.
- Configurable folder pairs — map any source directory to any destination.
- Images are resized to fit 1920×1080 (preserving aspect ratio, never
  upscaling) and saved as optimized JPEG (quality 85).
- Non-image files and videos are skipped automatically.
- Incremental mode: only files newer than the last run are processed.
  Use `--full` flag for a complete rescan.
- Fallback copy for files Pillow cannot decode (malformed JPEG headers, etc.).
- Sync log with 1-year retention, flushed every 25 files for live progress.
- Runs via cron at 3 AM daily, or on demand from the dashboard.

**Web UI (`server.py`, Flask)**
- Mobile-friendly dashboard at `/` and admin page at `/admin`.
- Start / stop / restart the slideshow.
- Change per-photo duration on the fly.
- Inspect the current photo: EXIF, GPS, resolved country.
- Thumbnail endpoint backed by Pillow.
- Year and country pickers backed by indexes built from your library.
- **Latest Changes** card showing recent sync activity with live progress.
- Daily on/off schedule (DPMS) — great for "lights out" at night.
- Edit `config.sh` from the admin page (comments preserved).
- **Sync folder configuration** on the admin page — add, remove, and
  reorder source/destination pairs without touching config files.

**Stats**
- Session view tracks photos shown since the last start.
- `/api/stats` and `/api/sessionpoints` for graphs/dashboards.

---

## Requirements

**Hardware**
- A Linux machine with a display attached (Raspberry Pi 3/4/5 is the common
  target, but anything with a framebuffer or X11 works).
- The user running the slideshow must be in the `video` group to access
  `/dev/fb0` and `/dev/dri/cardN`.

**System packages**

```bash
sudo apt install fbi          # framebuffer viewer (console mode)
# optional, used when X11 is detected:
sudo apt install feh eog imagemagick
```

**Python**
- Python 3.10+
- `flask`, `pillow`, `psutil`, `pycountry`, `reverse_geocoder`

```bash
pip install -r requirements.txt
```

(See [requirements.txt](requirements.txt) for the pinned set used in
development.)

---

## Installation

For a full Raspberry Pi setup (autologin kiosk + web server as systemd
services + sudoers for screen on/off), see **[INSTALL.md](INSTALL.md)**.
The short version:

```bash
git clone https://github.com/josephdahan-lab/papaframe.git
cd papaframe
sudo bash scripts/install.sh         # apt deps, venv, autologin, systemd
nano config.sh                       # set PHOTO_DIRS
sudo reboot
```

For a manual / non-Pi install:

```bash
git clone https://github.com/josephdahan-lab/papaframe.git
cd papaframe

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

chmod +x scripts/start_frame.sh
```

If you use a network share for photos (NFS / SMB), mount it before starting
the slideshow — `start_frame.sh` does a parallel `stat` over the photo list
and will quietly tolerate missing files, but a totally-down mount means an
empty list.

### Setting up Photo Sync

1. Mount your NAS and USB storage (e.g. via `/etc/fstab`).
2. Configure folder pairs on the admin page (`/admin` → **Photo Sync Folders**)
   or edit `sync_config.json` directly:
   ```json
   {
     "folders": [
       {"source": "/mnt/nas/Pictures/Joseph", "destination": "/mnt/usb/Pictures/Joseph"},
       {"source": "/mnt/nas/Pictures/Tricia", "destination": "/mnt/usb/Pictures/Tricia"}
     ]
   }
   ```
3. Add a cron job for daily sync:
   ```bash
   crontab -e
   # Add:
   0 3 * * * PYTHONUNBUFFERED=1 /path/to/papaframe/.venv/bin/python3 /path/to/papaframe/scripts/photo_sync.py >> /path/to/papaframe/sync.log 2>&1
   ```
4. The **Latest Changes** card on the dashboard shows sync progress and history.

---

## Configuration

**Everything tunable lives in [config.sh](config.sh).** Both `server.py` and
`scripts/start_frame.sh` source it, so it's the only file you need to edit
when moving PapaFrame to a different frame.

| Key                  | What it does                                         | Default                            |
| -------------------- | ---------------------------------------------------- | ---------------------------------- |
| `PHOTO_DIRS`         | Photo roots, colon-separated like `$PATH`            | `$HOME/Pictures`                   |
| `CACHE_SIZE_MB`      | Local photo-cache budget in MB (`0` disables it)     | `2048`                             |
| `CACHE_COMPRESS`     | `yes` = resize to 1080p + JPEG; `no` = copy verbatim | `yes`                              |
| `CACHE_QUALITY`      | JPEG quality 1–100 when `CACHE_COMPRESS=yes`         | `82`                               |
| `CACHE_DIR`          | Cache folder (relative paths anchor at repo root)    | `cache`                            |
| `DEFAULT_DURATION`   | Seconds per photo when the UI doesn't override       | `30`                               |
| `RESHUFFLE_INTERVAL` | Background reshuffle interval (seconds)              | `900`                              |
| `FORCE_VIEWER`       | `auto`, `fbi`, `feh`, `eog`, or `display`            | `auto`                             |
| `FBI_VT`             | Virtual terminal for `fbi` (`auto` or a number 1–7)  | `auto`                             |
| `FBI_DEVICE`         | DRM device for `fbi` (`auto`, `/dev/dri/cardN`, `""`) | `auto`                            |
| `SERVER_HOST`        | Bind address (`0.0.0.0` = all interfaces)            | `0.0.0.0`                          |
| `SERVER_PORT`        | TCP port for the web UI                              | `8000`                             |
| `SOURCE_FILE`        | Master photo list (regenerated when missing)         | `photo_list.txt`                   |
| `FRAME_SCRIPT`       | Path to `start_frame.sh`                             | `scripts/start_frame.sh`           |
| `LOG_FILE`           | Server log path (relative paths anchor at repo root) | `frame_display.log`                |

> Relative paths in `SOURCE_FILE`, `FRAME_SCRIPT`, `LOG_FILE`, and `CACHE_DIR`
> resolve from the repo root (the directory holding `server.py`). Absolute
> paths are used as-is. `~` and `$VARS` are expanded.

### Photo cache

When `CACHE_SIZE_MB` is non-zero, a background worker copies upcoming photos
into `CACHE_DIR` during the nightly screen-off window (the same schedule shown
on the dashboard). With `CACHE_COMPRESS=yes` each photo is resized to fit
1920×1080 and re-encoded as JPEG — roughly 200–400 KB each, so 2 GB holds
~5,000–10,000 photos; with `CACHE_COMPRESS=no` originals are copied verbatim.

The slideshow shows a cached copy whenever one exists and falls back to the
original otherwise — the swap is just a different path in the viewer's file
list, never a restart, so it is seamless. A useful side effect: if the photo
store is a NAS that goes offline, the slideshow keeps running from the cache.
The **Photo Cache** card on the dashboard shows usage and has a *Fill cache
now* button; cache settings are also editable on the `/admin` page.

### Sync configuration

Sync folder pairs are stored in `sync_config.json` and can be managed from
the admin page (**Photo Sync Folders** section). Each pair maps a source
directory (typically on a NAS mount) to a destination directory (typically on
local/USB storage). Subdirectories are included recursively.

### Two ways to edit config.sh

**1. From the web admin page (routine tweaks)** — open
`http://<frame-ip>:8000/admin`, change values, **Save config**. The values are
written back to `config.sh` with all comments preserved.

**2. By hand (initial setup or emergencies)**

```bash
$EDITOR config.sh
```

### After editing

- Restart the server for any change to take effect.
- If you changed `PHOTO_DIRS`, click **Rebuild photo list** on the admin
  page (or delete `SOURCE_FILE`) so the master list is regenerated.
- If you changed `SERVER_PORT`, point your browser at the new port.

### Pointing to a non-default config

Both scripts honor `PAPAFRAME_CONFIG` if you want the config file to live
elsewhere (`/etc/papaframe.conf`, dotfiles repo, etc.):

```bash
PAPAFRAME_CONFIG=/etc/papaframe.conf python3 server.py
```

---

## Running

**Manually** (two terminals or `&`):

```bash
# 1. Slideshow runner
bash scripts/start_frame.sh

# 2. Web server
python3 server.py
```

Then open `http://<frame-ip>:8000/` from any device on the same network.

### As a systemd service (recommended on a Pi)

Two units — one for the slideshow, one for the web server. Adjust paths and
`User=` to match your install.

`/etc/systemd/system/papaframe-slideshow.service`

```ini
[Unit]
Description=PapaFrame slideshow runner
After=multi-user.target

[Service]
Type=simple
User=pi
Group=video
ExecStart=/usr/bin/bash /home/pi/papaframe/scripts/start_frame.sh
Restart=on-failure
StandardOutput=append:/var/log/papaframe-slideshow.log
StandardError=append:/var/log/papaframe-slideshow.log

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/papaframe-server.service`

```ini
[Unit]
Description=PapaFrame web server
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/papaframe
ExecStart=/home/pi/papaframe/.venv/bin/python3 server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now papaframe-slideshow papaframe-server
```

> **Disable any display manager** (e.g. `sudo systemctl disable --now lightdm`)
> if you want `fbi` to own the framebuffer cleanly. Otherwise the desktop
> session will fight it for the screen.

---

## Web UI tour

| Page         | What's there                                                              |
| ------------ | ------------------------------------------------------------------------- |
| `/`          | Live status, current photo + EXIF/GPS, start/stop, duration slider, year and country pickers, schedule controls, **Latest Changes** sync log. |
| `/admin`     | Full `config.sh` editor with inline help, **Photo Sync Folders** config, and a **Rebuild photo list** button. |

### HTTP API

The dashboard is just a client of these endpoints — they're stable enough to
script against.

**Status / control**
- `GET  /api/status` — running flag, current duration, viewer, environment.
- `GET  /api/environment` — detected desktop / framebuffer / VT.
- `GET  /api/hostname`
- `POST /api/start` — launch the slideshow runner.
- `POST /api/stop` — request stop via flag file.
- `POST /api/restart`
- `POST /api/setduration` — `{ "duration": 10 }`

**Photos**
- `GET  /api/currentphoto` — path + EXIF + GPS for what's on screen now.
- `GET  /api/photoinfo?path=…` — same, for an arbitrary path.
- `GET  /api/photo/thumb?path=…` — JPEG thumbnail.

**Filters**
- `GET  /api/years` — `[ "2017", "2018", … ]`
- `POST /api/setfilter` — `{ "year": "2018" }` or `{ "year": "" }` to clear.
- `POST /api/rebuildyears`
- `GET  /api/locations` — countries present in the library.
- `POST /api/setlocationfilter` — `{ "country": "FR" }`
- `POST /api/rebuildlocations`

**Schedule**
- `GET  /api/schedule/status`
- `POST /api/schedule/configure` — `{ "off": [23,55], "on": [6,5] }`
- `POST /api/schedule/enable` / `POST /api/schedule/disable`

**Screen**
- `POST /api/screen` — `{ "state": "on" | "off" }` (DPMS).

**Config**
- `GET  /api/config` — current values + descriptions.
- `POST /api/config` — write back to `config.sh` (comments preserved).
- `POST /api/config/rebuild` — regenerate the master photo list.

**Cache**
- `GET  /api/cache/status` — enabled flag, size budget, usage, photo count.
- `POST /api/cache/fill` — trigger a cache fill now (runs in the background).

**Photo Sync (v2.0)**
- `GET  /api/sync/config` — current sync folder pairs.
- `POST /api/sync/config` — save sync folder pairs (`{ "folders": [...] }`).
- `GET  /api/sync/log?limit=N` — recent sync log entries (newest first).
- `POST /api/sync/run` — trigger a sync now (runs in background).
- `GET  /api/sync/status` — whether a sync is currently running.

**Stats**
- `GET  /api/stats`
- `GET  /api/sessionpoints`
- `POST /api/clearsession`

---

## Repository layout

```
papaframe/
├── server.py              ← Flask web server + admin API + sync API
├── config.sh              ← single source of truth for all settings
├── sync_config.json       ← sync folder pair mappings (editable from admin)
├── version.py             ← version string
├── requirements.txt
├── static/
│   ├── index.html         ← main dashboard (includes Latest Changes card)
│   ├── admin.html         ← /admin page (includes Photo Sync Folders config)
│   ├── style.css
│   └── favicon.svg
└── scripts/
    ├── start_frame.sh     ← slideshow launcher (sources config.sh)
    ├── photo_sync.py      ← NAS-to-USB photo sync with resize
    ├── papaframe-screen   ← root helper for HDMI on/off (installed to /usr/local/bin)
    └── install.sh         ← Pi installer (apt deps, venv, autologin, systemd unit)
```

State files written at runtime live under `/tmp/` (slideshow flags, the
filtered live list, the slideshow state JSON) and next to `server.py`
(`frame_display.log`, the year/location index caches, `sync_log.json`,
and the `cache/` folder holding cached photos + `manifest.tsv`).

---

## Changelog

### v2.0.0

- **Photo sync**: automatic daily sync from NAS to USB with image resize
  (1920×1080, JPEG quality 85). Configurable folder pairs, incremental
  mode, fallback copy for unreadable files.
- **Admin sync config**: add/remove unlimited source→destination folder
  pairs from the admin page — no file editing required.
- **Latest Changes dashboard card**: live sync progress, recent sync
  history, and a "Sync Now" button on the main dashboard.
- **Location filter fix**: corrected reverse-geocoder country code mapping
  for Israel (was incorrectly showing as "State of Palestine" for some
  GPS coordinates).
- **Year filter bug fix**: fixed a bug where applying a year filter would
  permanently truncate the master photo list, making it impossible to
  return to "All Years" without a manual rebuild.

### v1.0.5

- Initial public release with slideshow, web UI, photo cache, year/country
  filters, EXIF/GPS inspection, and DPMS schedule.

---

## Troubleshooting

- **Black screen, no `fbi` output** — the user running `start_frame.sh` is
  probably not in the `video` group. `groups` should list it.
- **`fbi` on a Pi 4/5 fails on `/dev/dri/card0`** — known: HDMI is on
  `card1`. The runner already passes `-device /dev/dri/card1`.
- **Lightdm or another DM is grabbing the framebuffer** — disable it:
  `sudo systemctl disable --now lightdm`.
- **Admin page won't save** — check `frame_display.log`; the server user
  needs write access to `config.sh`.
- **New photos don't appear** — click **Rebuild photo list** on the admin
  page, then restart the slideshow.
- **Empty slideshow over SMB/NFS** — the live-list filter drops missing
  paths; if the mount is fully down it'll keep the stale list rather than
  blank the screen, but verify the share is mounted before debugging further.
- **"Loading FAILED" briefly visible** — `fbi` prints that itself when a
  file isn't readable. Run a manual rebuild after large library changes.
- **Sync errors in the log** — check that both NAS and USB mounts are
  accessible and that the user running the cron job has write permissions
  to the destination directories. Verify mount ownership in `/etc/fstab`
  matches the user's UID/GID.
- **Year filter stuck on one year** — this was a bug fixed in v2.0.0. If
  the master photo list (`photo_list.txt`) looks truncated, delete it and
  click **Rebuild photo list** on the admin page.

---

## License

MIT. See [LICENSE](LICENSE).
