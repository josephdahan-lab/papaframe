# PapaFrame

A Linux-based digital picture frame that turns a Raspberry Pi (or any Linux box
with a display) into a network-controllable slideshow. A small bash runner
drives the slideshow directly on the framebuffer — no desktop environment
required — while a Flask web server gives you a phone-friendly dashboard for
start/stop, duration, filtering by year or country, EXIF/GPS inspection, and
scheduled screen on/off.

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
- Background reshuffle on a configurable interval (default 15 min).
- Background pass to drop dead paths from the live list — important for
  network shares where files come and go.
- Resume position when duration changes mid-show, so the viewer doesn't snap
  back to photo #1 every time you tweak the speed.
- Year filter (`/2017/`-style path matching) and country filter (reverse-
  geocoded from photo GPS).

**Web UI (`server.py`, Flask)**
- Mobile-friendly dashboard at `/` and admin page at `/admin`.
- Start / stop / restart the slideshow.
- Change per-photo duration on the fly.
- Inspect the current photo: EXIF, GPS, resolved country.
- Thumbnail endpoint backed by Pillow.
- Year and country pickers backed by indexes built from your library.
- Daily on/off schedule (DPMS) — great for "lights out" at night.
- Edit `config.sh` from the admin page (comments preserved).

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

```bash
git clone https://github.com/josephdahan-lab/papaframe.git
cd papaframe

# Python deps in a venv (recommended)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Make the slideshow runner executable
chmod +x scripts/start_frame.sh
```

If you use a network share for photos (NFS / SMB), mount it before starting
the slideshow — `start_frame.sh` does a parallel `stat` over the photo list
and will quietly tolerate missing files, but a totally-down mount means an
empty list.

---

## Configuration

**Everything tunable lives in [config.sh](config.sh).** Both `server.py` and
`scripts/start_frame.sh` source it, so it's the only file you need to edit
when moving PapaFrame to a different frame.

| Key                  | What it does                                         | Default                            |
| -------------------- | ---------------------------------------------------- | ---------------------------------- |
| `PHOTO_DIRS`         | Photo roots, colon-separated like `$PATH`            | `$HOME/Pictures`                   |
| `DEFAULT_DURATION`   | Seconds per photo when the UI doesn't override       | `30`                               |
| `RESHUFFLE_INTERVAL` | Background reshuffle interval (seconds)              | `900`                              |
| `FORCE_VIEWER`       | `auto`, `fbi`, `feh`, `eog`, or `display`            | `auto`                             |
| `FBI_VT`             | Virtual terminal for `fbi` (`auto` or a number 1–7)  | `auto`                             |
| `SERVER_HOST`        | Bind address (`0.0.0.0` = all interfaces)            | `0.0.0.0`                          |
| `SERVER_PORT`        | TCP port for the web UI                              | `8000`                             |
| `SOURCE_FILE`        | Master photo list (regenerated when missing)         | `photo_list.txt`                   |
| `FRAME_SCRIPT`       | Path to `start_frame.sh`                             | `scripts/start_frame.sh`           |
| `LOG_FILE`           | Server log path (relative paths anchor at repo root) | `frame_display.log`                |

> Relative paths in `SOURCE_FILE`, `FRAME_SCRIPT`, and `LOG_FILE` resolve
> from the repo root (the directory holding `server.py`). Absolute paths
> are used as-is. `~` and `$VARS` are expanded.

### Two ways to edit it

**1. From the web admin page (routine tweaks)** — open
`http://<frame-ip>:8000/admin`, change values, **Save config**. The values are
written back to `config.sh` with all comments preserved.

**2. By hand (initial setup or emergencies)**

```bash
$EDITOR config.sh
```

### After editing

- Restart the server for any change to take effect.
- If you changed `PHOTO_DIRS`, click **🔁 Rebuild photo list** on the admin
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
| `/`          | Live status, current photo + EXIF/GPS, start/stop, duration slider, year and country pickers, schedule controls. |
| `/admin`     | Full `config.sh` editor with inline help, plus a **Rebuild photo list** button. |

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

**Stats**
- `GET  /api/stats`
- `GET  /api/sessionpoints`
- `POST /api/clearsession`

---

## Repository layout

```
papaframe/
├── server.py              ← Flask web server + admin API
├── config.sh              ← single source of truth for all settings
├── requirements.txt
├── static/
│   ├── index.html         ← main dashboard
│   ├── admin.html         ← /admin page
│   ├── style.css
│   └── favicon.svg
└── scripts/
    └── start_frame.sh     ← slideshow launcher (sources config.sh)
```

State files written at runtime live under `/tmp/` (slideshow flags, the
filtered live list, the slideshow state JSON) and next to `server.py`
(`frame_display.log`, the year/location index caches).

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
- **New photos don't appear** — click **🔁 Rebuild photo list** on the admin
  page, then restart the slideshow.
- **Empty slideshow over SMB/NFS** — the live-list filter drops missing
  paths; if the mount is fully down it'll keep the stale list rather than
  blank the screen, but verify the share is mounted before debugging further.
- **"Loading FAILED" briefly visible** — `fbi` prints that itself when a
  file isn't readable. Run a manual rebuild after large library changes.

---

## License

MIT. See [LICENSE](LICENSE).
