# video-stream

Broadcast every local camera over Wi‑Fi. Each stream gets a shareable URL you can copy and paste into **OBS** on any machine on the same network.

<div align="center">

**Dark control dashboard · live mini previews · one-click copy URLs**

</div>

## Features

- Discovers available cameras on launch
- Serves each camera as:
  - **Browser / OBS view** — full-bleed page for OBS *Browser Source*
  - **MJPEG stream** — raw multipart JPEG for Media / VLC sources
- Lists **LAN URLs** (not just localhost) for remote machines
- One-click **Copy** on every URL
- Live mini previews in a sleek black UI
- Start / stop per camera, rescan devices
- One-command install + `video-stream` launcher that opens the app

## Install

```bash
git clone https://github.com/lalomorales22/video-stream.git
cd video-stream
./install.sh
```

That’s it. The installer will:

1. Create a project virtualenv (`.venv`)
2. Install Python dependencies (editable install)
3. Put a **`video-stream`** command on your PATH at `~/.local/bin/video-stream`
4. Add `~/.local/bin` to your PATH in `~/.zshrc` / `~/.bashrc` if it isn’t already there

If it updated your shell config, open a **new terminal** (or run `source ~/.zshrc`).

Re-run `./install.sh` anytime after pulling updates to refresh the install.

### Requirements

- **Python 3.10+** (`python3` on your PATH)
- macOS / Linux (Windows: use the [manual install](#manual-install-optional) path)
- Camera permission when the OS prompts you

## Run

From any terminal:

```bash
video-stream
```

This will:

1. Start the local Wi‑Fi broadcaster (default port **8765**)
2. Open the dashboard in your browser at [http://127.0.0.1:8765](http://127.0.0.1:8765)

Useful variants:

```bash
video-stream --help
video-stream --port 9000
video-stream --no-open          # start server only, don’t open browser
video-stream --width 1920 --height 1080 --fps 30 --quality 85
```

### URLs after launch

| Where | URL |
|--------|-----|
| This machine | [http://127.0.0.1:8765](http://127.0.0.1:8765) |
| Other devices on your LAN | `http://<your-lan-ip>:8765` |

macOS may prompt for **Camera** permission the first time — allow it, then hit **Rescan** in the dashboard.

### Manual install (optional)

If you prefer not to use `install.sh`:

```bash
git clone https://github.com/lalomorales22/video-stream.git
cd video-stream
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
python -m video_stream
```

## Use with OBS

1. Run **`video-stream`** on the machine that has the cameras.
2. In the dashboard, copy the **OBS · Browser Source** URL (use a **LAN** IP, not `127.0.0.1`, on the remote PC).
3. On the OBS machine (same Wi‑Fi / network):
   - **Sources → + → Browser**
   - URL: paste the view link, e.g. `http://192.168.1.42:8765/view/0`
   - Width / height: match your capture (default `1280×720`)
   - OK

### URL shapes

| Purpose | Path | Example |
|--------|------|---------|
| Dashboard | `/` | `http://192.168.1.42:8765/` |
| OBS Browser Source | `/view/{id}` | `http://192.168.1.42:8765/view/0` |
| Raw MJPEG | `/stream/{id}` | `http://192.168.1.42:8765/stream/0` |
| Single JPEG snapshot | `/snapshot/{id}` | `http://192.168.1.42:8765/snapshot/0` |
| JSON status | `/api/status` | `http://192.168.1.42:8765/api/status` |

Camera ids are integers starting at `0`.

## CLI options

```bash
video-stream --help
video-stream --port 8765 --width 1280 --height 720 --fps 30 --quality 80
video-stream --no-open
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address (`0.0.0.0` = all interfaces) |
| `--port` | `8765` | HTTP port |
| `--width` | `1280` | Capture width |
| `--height` | `720` | Capture height |
| `--fps` | `30` | Target FPS |
| `--quality` | `80` | JPEG quality (40–95) |
| `--open` / `--no-open` | open | Open the dashboard in your browser |

Environment variables: `VIDEO_STREAM_HOST`, `VIDEO_STREAM_PORT`.

## Firewall notes

Allow inbound TCP on the chosen port (default **8765**) so other devices can reach the streams.

- **macOS**: System Settings → Network → Firewall
- **Windows**: Windows Defender Firewall → allow Python / the port
- **Linux**: `ufw allow 8765/tcp` (or your firewall of choice)

Both machines must be on the **same LAN / Wi‑Fi** (or routed private network). Guest Wi‑Fi isolation will block this.

## Stack

- **Python 3.10+**
- **FastAPI** + **Uvicorn** — HTTP API & dashboard
- **OpenCV** — camera capture
- **MJPEG** — low-friction streaming for browsers & OBS

## Project layout

```
video-stream/
├── install.sh          # one-shot setup + PATH launcher
├── video_stream/
│   ├── app.py          # FastAPI routes + CLI
│   ├── camera.py       # discovery & MJPEG capture
│   ├── network.py      # LAN IP detection
│   ├── static/         # CSS / JS
│   └── templates/      # dashboard + OBS viewer
├── requirements.txt
├── pyproject.toml
└── README.md
```

## License

MIT
