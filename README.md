# video-stream
<img width="821" height="577" alt="Screenshot 2026-07-18 at 8 03 51 PM" src="https://github.com/user-attachments/assets/c8f4b0c0-56cc-47ba-a983-b1bad9ec91d1" />


Broadcast every local camera over Wi‑Fi. Each stream gets a shareable URL you can copy and paste into **OBS** on any machine on the same network — and, optionally, run computer vision on the feeds: a live **pose skeleton** overlay and a hands-free **auto-director** that switches OBS to whichever camera is active.

<div align="center">

**Dark control dashboard · live mini previews · one-click copy URLs · pose tracking · auto-switching**

</div>

## Features

**Streaming**

- Discovers available cameras on launch (macOS · Linux · Windows)
- Serves each camera as:
  - **Browser / OBS view** — full-bleed page for OBS *Browser Source*
  - **MJPEG stream** — raw multipart JPEG for Media / VLC sources
- Lists **LAN URLs** (not just localhost) for remote machines
- One-click **Copy** on every URL
- Live mini previews in a sleek black UI
- Start / stop per camera, rescan devices
- One-command install + `video-stream` launcher that opens the app

**Computer vision** (optional, toggle live from the dashboard)

- [**Pose skeleton**](#pose-overlay-motion-tracking) — draws a live 33-point body skeleton on any
  camera; the overlay streams straight into OBS with no OBS-side setup
- [**Auto-director**](#auto-director-hands-free-camera-switching) — scores motion per camera and
  auto-switches OBS to the active one over its WebSocket API, hands-free
- [**Avatar / VTuber**](#avatar-vtuber-beta) *(beta)* — drive a rigged 3D avatar with your face
  (head, blinks, mouth) in the browser; renders transparent for OBS

**Studio** (live-production controls on the dashboard)

- [**Studio Bus**](#studio-bus-safety--replay--smart-zoom) — one WebSocket (`/ws`) pushes every state
  change (cuts, cameras, safety, replays) to the dashboard and future overlays; polling is only a fallback
- [**Kill switch**](#kill-switch--automation-budget) — one red button (or `Ctrl+Shift+K`) freezes every
  automation; a rolling rate limiter caps automated OBS actions per minute
- [**Replay highlights**](#replay-highlights) — save the last seconds via the OBS replay buffer with one
  click (`Ctrl+Shift+H`), optional instant playback + lower-third + chapter marker; *Auto* mode captures
  by itself when motion spikes across the rig
- [**Smart Zoom**](#smart-zoom-punch-ins) — double-click any preview to punch in 2× on that spot, baked
  into the stream server-side so OBS sees it with zero setup; `--director-auto-punch` lets the director
  tighten on the tracked face after each cut
- [**Hybrid director rules**](#hybrid-director-rules-audio--motion) — "cut to whoever is *talking*":
  mix `audio:<OBS input>` (live dBFS meters) and `motion:<camera>` rules with priorities, hysteresis,
  and a human-readable `last_decision` explaining every cut
- [**Overlay pack**](#overlay-pack-captions--hud--alerts--stinger) — free live captions (speech
  recognition in your Chrome tab), a rig HUD, animated alerts, and a stinger that fires on every
  director cut — all transparent OBS browser sources with copyable URLs
- [**Unified chat**](#unified-live-chat-twitch--kick) — Twitch (zero credentials) + Kick chat merged
  server-side, with an on-stream chat overlay
- [**Setup wizard & settings**](#setup-wizard--runtime-settings) — scan → propose a scene map → verify
  the rig with a pass/fail checklist; edit everything from the dashboard (secrets masked, optional
  shared-token auth)
- [**Rig Link**](#rig-link-multi-machine-directing) — the director on your OBS box reacts to motion on
  cameras plugged into *other* machines: `--peers studio=192.168.1.42:8765` and rules like
  `motion:studio:1` cut on remote movement
- [**Phone as camera**](#phone-as-camera) — scan a QR and any phone becomes a wireless camera over
  WebRTC; the view page drops straight into OBS as a Browser Source
- [**Chaos engine**](#chaos-engine-fx--obs-choreography) — one-click fullscreen effects (confetti,
  glitch, matrix…) plus shareable JSON presets that choreograph OBS itself — scene slams, BRB
  sequences, intros — all behind the kill switch

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

### Linux notes

Works on Linux (tested against Arch-based setups such as **Omarchy**). Cameras are
discovered through **V4L2** by enumerating the real `/dev/video*` nodes, so sparse and
non-contiguous device numbering is handled correctly. Where the kernel exposes a device
name, it's used instead of a generic label — you'll see `Logitech BRIO` rather than
`Camera 2`, which matters once several cameras are connected.

Two Linux-specific things the installer checks for you, because both fail *silently* at
runtime (the dashboard just shows no cameras, with no error explaining why):

1. **`video` group membership** — required to open `/dev/video*`:

   ```bash
   sudo usermod -aG video "$USER"
   ```

   Log out and back in afterward; group changes only apply to a new session.

2. **At least one `/dev/video*` node exists** — i.e. a camera is actually attached.

**Open the port in your firewall.** Most desktop Linux distros — **Omarchy included** —
ship with `ufw` enabled, which blocks the stream from every other machine while it keeps
working perfectly in a browser on the Linux box itself:

```bash
sudo ufw allow 8765/tcp
```

The installer deliberately does not do this for you; changing firewall rules is your call,
not an installer's.

No system OpenCV packages are needed. The project uses `opencv-python-headless`, which
ships its own libraries and avoids the `libGL.so.1` error the regular `opencv-python`
wheel throws on minimal/headless installs.

If your distro doesn't bundle the venv module, install it first
(Debian/Ubuntu: `apt install python3-venv`; Arch/Omarchy already includes it).

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
| `--pose` | off | Overlay a live pose skeleton on every camera (see [Pose overlay](#pose-overlay-motion-tracking)) |
| `--pose-model` | `lite` | Pose model: `lite` (fast), `full`, or `heavy` (most accurate) |
| `--pose-stride` | `2` | Run pose inference every Nth frame — higher = lighter CPU |
| `--director` + `--obs-*` | off | Auto-director (see [Auto-director](#auto-director-hands-free-camera-switching)) |
| `--director-rules` | *(off)* | Hybrid audio+motion rules JSON (see [Hybrid director rules](#hybrid-director-rules-audio--motion)) |
| `--director-auto-punch` | off | Punch in on the subject after each director cut (see [Smart Zoom](#smart-zoom-punch-ins)) |
| `--peers` | *(off)* | Rig Link peers, e.g. `"studio=192.168.1.42:8765"` (see [Rig Link](#rig-link-multi-machine-directing)) |
| `--safety-*` | see below | Kill switch fallback scene + automation budget (see [Safety](#kill-switch--automation-budget)) |
| `--replay-*` | see below | Replay highlight garnish sources (see [Replay highlights](#replay-highlights)) |
| `--phone-https-port` | `8766` | HTTPS port for phone pages (see [Phone as camera](#phone-as-camera)) |
| `--ssl-certfile` / `--ssl-keyfile` | auto | TLS cert override; default is what `./install-phone.sh` generates |

Environment variables: `VIDEO_STREAM_HOST`, `VIDEO_STREAM_PORT`,
`VIDEO_STREAM_CACHE` (where pose models are cached).

## Pose overlay (motion tracking)

Optional. Draws a live skeleton (33 body keypoints — shoulders, arms, torso, face)
onto every camera feed using [MediaPipe](https://ai.google.dev/edge/mediapipe).
Because the skeleton is drawn straight onto the frame **before** it's streamed, it
flows through to OBS with no OBS-side setup — the same URLs, now with tracking.

It's a separate add-on so the core rig install stays light. Install it once:

```bash
./install-pose.sh
```

Then run with `--pose`:

```bash
video-stream --pose                 # lite model, skeleton on every camera
video-stream --pose --pose-model heavy   # slower, more accurate
video-stream --pose --pose-stride 3      # lighter CPU (infer every 3rd frame)
```

The model (~5 MB for `lite`) downloads once on first use and is cached under
`~/.cache/video-stream`.

You can also toggle the skeleton **per camera at runtime** with the **Skeleton**
button on each card in the dashboard — no restart, no flag. (`--pose` just turns it
on for every camera at startup.)

**Notes:**

- **CPU cost.** Inference runs per camera. On a multi-camera rig, prefer `lite` and
  a higher `--pose-stride`, or let each machine run pose only on its own cameras.
  A GPU helps a lot.
- **Why a separate installer?** MediaPipe depends on the *GUI* build of OpenCV, which
  conflicts with the `opencv-python-headless` the rig uses (and reintroduces the
  `libGL` error on headless Linux). `install-pose.sh` installs MediaPipe and then
  restores the headless build so it owns `cv2`. Don't use `pip install .[pose]`
  directly — it skips that fix-up.
- **Graceful fallback.** If you pass `--pose` without MediaPipe installed, the app
  prints how to install it and starts as a normal stream instead of crashing.

Pose estimation is the first CV feature; object detection and background removal
are natural next steps on the same per-frame hook in `video_stream/camera.py`.

## Auto-director (hands-free camera switching)

Turns a multi-camera rig into a self-operating one: it scores motion on each camera
and automatically switches OBS to whichever one is **active** — no keyboard, no
second operator. It talks to OBS over the built-in [OBS WebSocket](https://github.com/obsproject/obs-websocket),
so no extra dependency is needed.

**Toggle it from the dashboard.** The **Auto-director** button in the toolbar turns it
on/off live, and the selected camera gets an **ON AIR** ring so you can see what it's
choosing. (Wiring to real OBS still needs the scene map + password below, best passed
at startup.)

**Try it first in dry-run** (watch it decide without touching OBS):

```bash
video-stream --director --director-dry-run --obs-scene-map "0=Cam A,1=Cam B,2=Cam C"
```

Then open `http://127.0.0.1:8765/api/director` (or watch the console) to see live
motion scores, the current active camera, and recent switches. Tune from there.

**Wire it to OBS for real:**

1. In OBS: **Tools → WebSocket Server Settings → Enable**. Note the port (default
   `4455`) and password.
2. Make sure your camera scenes exist in OBS, then map each camera to its scene:

   ```bash
   video-stream --director \
     --obs-scene-map "0=Cam A,1=Cam B,2=Cam C" \
     --obs-password "YOUR_OBS_PASSWORD"
   ```

The camera index is the same one in the stream URLs (`/stream/0`, `/stream/1`, …).

**Tuning:**

| Flag | Default | Description |
|------|---------|-------------|
| `--director-hold` | `1.5` | Seconds a camera must stay most-active before cutting to it |
| `--director-cooldown` | `3.0` | Minimum seconds between cuts (prevents rapid flip-flopping) |
| `--director-min-score` | `0.02` | Motion score (0–1) a camera must clear to count as active |
| `--obs-host` / `--obs-port` | `127.0.0.1` / `4455` | Where OBS WebSocket is listening |

**Notes:**

- **Motion-based, not person-based (yet).** "Active" means *most movement*. It's cheap
  (pure OpenCV, no ML) and works well for talking-head / presenter switching. A fan or
  a screen in view can register as motion — aim cameras accordingly, or raise
  `--director-min-score`.
- **Safe by default.** If OBS isn't reachable it logs intended switches instead of
  crashing, so the stream keeps running. `--director-dry-run` forces log-only.
- **Runs on the OBS box.** Point one `video-stream --director` at your cameras (local
  or remote streams) and let it drive the OBS instance on the same machine.

## Studio Bus, safety & replay & Smart Zoom

The dashboard is a live production surface, not just a URL list. Everything below rides
the **Studio Bus** — a single WebSocket at `/ws` that pushes `{type, payload}` events
(camera changes, director cuts, safety state, saved replays) the instant they happen.
New connections receive the latest retained state immediately, and the old HTTP polling
stays on as a degraded fallback, so nothing breaks if the socket drops.

### Kill switch & automation budget

The red **KILL** button (or `Ctrl+Shift+K`) instantly freezes every automation — the
auto-director stops cutting, auto-replay stops capturing — until you release it. Manual
controls keep working; panic paths are never guarded. Independently, a rolling rate
limiter allows at most `--safety-max-actions` automated OBS actions per minute
(default 40), so a runaway loop can't machine-gun your scene collection. The topbar
pill shows the remaining budget live.

| Flag | Default | What it does |
|---|---|---|
| `--safety-fallback-scene` | *(off)* | OBS scene for the `POST /api/safety/fallback-scene` panic cut |
| `--safety-max-actions` | `40` | Max automated OBS actions per rolling minute |

### Replay highlights

Enable the replay buffer in OBS (**Settings → Output → Replay Buffer**), then hit
**⚡ Highlight** (or `Ctrl+Shift+H`) the moment something great happens: the last
seconds are saved to disk, and — if configured — instantly played back into an OBS
media source, announced with an auto-hiding lower-third, and chapter-marked in the
recording. Every optional step degrades to a log line; the file is saved regardless.

Turn on **Auto** and the rig captures by itself: several motion spikes inside a
10-second window trigger one capture, with a 30-second cooldown. Auto-captures pass
through the same kill switch and rate limiter as everything else.

| Flag | Default | What it does |
|---|---|---|
| `--replay-media-source` | *(off)* | OBS media source that instantly plays each saved replay |
| `--replay-lower-third` | *(off)* | OBS text source updated with the replay label |
| `--replay-lower-third-scene` | *(off)* | Scene holding that text source (shown, then auto-hidden) |

### Smart Zoom (punch-ins)

Double-click anywhere on a camera preview to smoothly punch in **2×** on that exact
spot — double-click again for the wide shot. The zoom is applied **server-side in the
capture loop** (eased crop + rescale, ported from ChromaCanvas), so every OBS pulling
that stream sees the punch-in with zero setup. Motion scoring still sees the full
frame, and the pose skeleton draws in the zoomed space you're looking at.

With `--director-auto-punch`, the auto-director punches in ~1.6× after each cut —
aimed at the tracked face when the skeleton overlay is on (center-frame otherwise) —
then eases back out before the next cut is allowed.

### Hybrid director rules (audio + motion)

Motion-only direction cuts to a waving hand while the speaker sits still. With a rules
file, the director also listens: `audio:<OBS input>` rules use **live loudness meters**
(a dedicated OBS WebSocket connection subscribed to `InputVolumeMeters` — the only way
to get real levels; the input fader is not loudness), and `motion:<camera>` rules use
this rig's motion scores. Copy `presets/director-rules.example.json`, adjust, then:

```bash
video-stream --director --director-rules my-rules.json
```

Rules never mix units: audio thresholds are dBFS (−90…0, e.g. `-55` for "speaking"),
motion thresholds are 0–1. Higher `priority` wins ties; a challenger must beat the
active audio rule by `hysteresis_db` (default 3) to steal the scene, must stay on top
for its `hold`, and cuts respect the global `cooldown`. The dashboard (and `GET
/api/director`) shows `last_decision` — `pending:mic_cam`, `hysteresis-hold:…`,
`switch:Camera` — so the director is never a black box. Audio input names match
case-insensitively.

### Rig Link (multi-machine directing)

Run video-stream on every camera machine, and one more on the OBS box with the
director. The OBS-box director already hears audio (its OBS inputs are local) — the
Rig Link adds *remote motion*: it polls each peer's `GET /api/signals` a few times a
second (switching the peer's motion scoring on automatically) and folds the scores
into its signal map as `motion:<peer>:<index>`.

```bash
# On the OBS box:
video-stream --director --director-rules rules.json \
  --peers "studio=192.168.1.42:8765,den=10.0.0.9:8765"
```

```json
{ "source": "motion:studio:1", "scene": "Studio Wide", "threshold": 0.05 }
```

Peers can also be set from the dashboard (Settings → `peers`). A dead peer's signals
simply go stale and drop out of candidacy within ~2 s; the Setup panel's *Verify rig*
checks every peer's reachability.

## Phone as camera

One-time setup: `./install-phone.sh` (generates a self-signed certificate — phones
only allow camera access on secure pages), then restart. After that:

1. Click **📱 Add phone** on the dashboard and scan the QR with the phone
   (same Wi-Fi; accept the certificate warning once, allow the camera).
2. Copy the **view URL** from the panel into OBS as a **Browser Source** —
   that page shows the phone's camera full-bleed, live over WebRTC.

Flip between front/back cameras from the phone; the view page reconnects
automatically, and it no longer matters which side opens first — the phone
re-offers whenever a receiver joins. Append `?audio=1` to the view URL in OBS
(with *Control audio via OBS*) to carry the phone's microphone too. Each *Add phone* click mints an independent session, so several
phones can join as separate sources. Signaling is a tiny in-process relay
(`/phone-signal`); media flows phone → receiver directly over the LAN (STUN only —
both devices must share the network).

## Chaos engine (FX + OBS choreography)

Drop `/overlay/fx` into OBS as a Browser Source and the **FX** buttons on the
dashboard fire fullscreen effects — `confetti · glitch · matrix · flash · blackout`
— pure overlay, harmless by construction.

Presets go further: JSON files in `presets/chaos/` choreograph OBS itself —

```json
{ "name": "BRB slam", "cooldown": 10, "confirm": true,
  "steps": [
    { "do": "fx", "effect": "glitch", "ms": 450 },
    { "do": "sleep", "ms": 300 },
    { "do": "scene", "scene": "BRB" },
    { "do": "parallel", "steps": [
      { "do": "item", "scene": "BRB", "source": "Logo", "enabled": true },
      { "do": "filter", "source": "Cam", "filter": "Blur", "enabled": true }
    ]}
  ] }
```

Step kinds: `scene` · `item` (show/hide) · `transform` · `filter` · `sleep` · `fx` ·
`request` (raw obs-websocket escape hatch) · `serial`/`parallel` containers. Presets
are validated at load with per-step labels (`brb-slam.json:steps[2]`) — bad files are
skipped loudly, never silently. One preset runs at a time (409 if busy), each has its
own cooldown (429), every run passes the safety guard (`chaos:<id>` — the kill switch
freezes chaos too), and `"confirm": true` makes the dashboard ask before running.

## Overlay pack (captions · HUD · alerts · stinger)

Five transparent **OBS Browser Source** pages, all pushed live over the Studio Bus
(no CDNs — they work on an offline rig). The dashboard's *Overlays* chips copy each
URL with your LAN address:

| Overlay | URL | What it shows |
|---|---|---|
| Captions | `/overlay/subtitles` | Live subtitle line (settings: font/size/colors via `/api/subtitles/settings`) |
| Alerts | `/overlay/alerts` | Animated alert cards with particle bursts (test from the dashboard) |
| Rig HUD | `/overlay/hud` | Per-camera LIVE/OFF, on-air + director reasoning, safety budget, replay/cut toasts |
| Stinger | `/overlay/stinger` | Plays a swipe on every director cut (`?style=glitch` or `?style=fade` for variants) |
| Chat | `/overlay/chat` | The merged Twitch+Kick feed, bottom-anchored |

**Live captions are free**: click *🎤 Start* in the dashboard's Captions bar — speech
recognition runs in that Chrome tab (needs `localhost` or HTTPS + mic permission) and
the server just relays text to the overlay. There's also a type-a-line box for manual
lower-thirds.

## Unified live chat (Twitch + Kick)

Enter a channel name in the Chat bar and hit Connect. **Twitch needs zero
credentials** (anonymous read-only IRC); Kick uses its public chat socket (if the
name lookup is blocked, paste the numeric chatroom id instead). Messages merge
server-side — every dashboard and the `/overlay/chat` browser source see the same
feed, and `GET /api/chat/history` backfills late joiners.

## Setup wizard & runtime settings

Click **Setup** in the topbar:

- **Verify rig** — pass/fail checklist: cameras streaming, OBS reachable, scene map
  covers live cameras, mapped scenes exist, replay buffer enabled, MediaPipe present.
  Run it *before* going live, not during.
- **Propose scene map** — scans your OBS scenes and matches them to cameras
  (`"Cam 0"`-style names match first, then device-name words); review and apply with
  one click.
- **Settings** — every important flag, editable live: OBS connection, director tuning,
  replay sources, safety caps. Saved to `~/.config/video-stream/settings.json`
  (`chmod 600`; secrets are write-only and never echoed back). Explicit CLI flags
  still win at boot. Set `auth_token` to require an `X-Auth-Token` header for future
  settings changes.

## Avatar (VTuber, beta)

Drive a rigged **3D avatar** with your face in real time — head turns, blinks, brows,
and mouth/visemes — rendered on a transparent canvas you drop straight into OBS as a
**Browser Source**. The studio lives **inside the dashboard** as the *Avatar* tab
(switching away pauses its rendering; switching back resumes exactly where you left
off), and the ↗ button pops it into its own browser tab for a second monitor. The
standalone `/avatar` URL is what OBS consumes — that never changes. Everything runs client-side in the browser (MediaPipe face tracking
→ [Kalidokit](https://github.com/yeemachine/kalidokit) retargeting →
[three-vrm](https://github.com/pixiv/three-vrm)); the server just serves the page. See
[`path_b.md`](path_b.md) for the full design.

It's opt-in — the browser libraries + face model (~40 MB) aren't bundled. Install once:

```bash
./install-avatar.sh
```

Then start the app and open **`http://<this-machine>:8765/avatar`** (or the **Avatar**
link in the dashboard). Press **Start tracking**, grant camera access, and your avatar
mirrors you. Add that same URL as an OBS **Browser Source** (transparent by default) to
composite the avatar over your scene — no green screen.

**Controls** (bar auto-hides; hover to show — so it never shows in OBS): start/stop
tracking, **source picker** (local webcam *or* any of this server's cameras), upload your
own `.vrm`, mirror toggle, preview backdrop (preview only), and **Copy OBS URL**.

### Using it in OBS (and across machines)

The avatar is a **page you point OBS at**, not a pulled video stream — it renders in the
browser. That means the tracking runs wherever the page is displayed. Two ways to use it:

- **Avatar Sync (the default now):** keep the avatar page open and tracking on any
  machine, and every copied OBS URL carries `?follow=1` — the OBS instance
  **mirrors your live tracking over the Studio Bus** instead of running its own.
  One tracker, any number of perfectly-synced OBS sources, no webcam needed on
  the OBS box. (Keep the tracking page open — it's the puppeteer.)
- **Independent tracking from a camera stream:** point the source at a feed and
  the OBS instance tracks it by itself:

  ```
  http://<camera-machine>:8765/avatar?obs=1&src=/stream/2
  ```

  OBS on any machine loads that, and the avatar is driven by camera 2 on the camera
  machine. Everything loads from that one host, so there's no cross-origin hassle.

**Easiest path:** open `/avatar`, set it up the way you want (pick the source, frame it
with scroll/drag, mirror), then click **Copy OBS URL** — it builds a ready-to-paste
Browser Source URL (with this machine's LAN IP and your framing baked in) and copies it.
Paste that into OBS on any machine.

**URL options** (for hands-off OBS sources): `?obs=1` (transparent, no UI) ·
`?src=/stream/N` (drive from a camera feed) · `?vrm=<url>` · `?mirror=0|1` ·
`?zoom=<n>` · `?pan=<y>` · `?autostart`.

> Note: the *local webcam* needs a secure context, so it only works on
> `localhost`/`127.0.0.1` (or HTTPS) — not a bare `http://<lan-ip>`. Over the LAN, use a
> **camera stream** source (`?src=`), which has no such restriction.

**Notes:**

- **Bring an avatar.** Use a **VRM** avatar — make one free at
  [VRoid Studio](https://vroid.com) or grab one from [VRoid Hub](https://hub.vroid.com).
  Drop it at `static/models/avatar.vrm` or upload it from the page.
- **Full body (beta).** The **Full body** button adds arms/legs/torso tracking (MediaPipe
  Pose). Stand back so your body is in frame; it auto-widens the shot. Legs get jittery
  when your lower body is out of frame — face-only stays cleaner for a seated close-up.
- **Your Tripo/other model needs prep.** A raw GLB/FBX isn't drivable as-is — it needs a
  humanoid rig ([Mixamo](https://mixamo.com)) and face blendshapes, then conversion to
  VRM. That's the real time sink; a VRoid avatar "just works." (`path_b.md` §6.)
- **Runs offline** once installed — libraries are vendored, no CDN at runtime.
- **Roadmap:** body + hands tracking, high-fidelity face (52 ARKit blendshapes /
  "PerfectSync"), and an AI-persona avatar (LLM + TTS + audio lip-sync). See `path_b.md`.

## Firewall notes

Allow inbound TCP on the chosen port (default **8765**) so other devices can reach the streams.

- **macOS**: System Settings → Network → Firewall
- **Windows**: Windows Defender Firewall → allow Python / the port
- **Linux**: `ufw allow 8765/tcp` (or your firewall of choice)

Both machines must be on the **same LAN / Wi‑Fi** (or routed private network). Guest Wi‑Fi isolation will block this.

## Troubleshooting

### `video-stream is not installed correctly.`

The `video-stream` command is **not a shell alias** — it's a small launcher script generated at
`~/.local/bin/video-stream` with the project path baked into it. If you **move or rename the project
folder**, that hardcoded path goes stale and the launcher exits with:

```
video-stream is not installed correctly.
Run:  cd /old/path/to/project && ./install.sh
```

Fix it by re-running the installer from the folder's **current** location:

```bash
cd /path/to/video-stream
./install.sh
```

If it still fails, the `.venv` also has the old path baked into its scripts — rebuild it from scratch:

```bash
cd /path/to/video-stream
rm -rf .venv
./install.sh
```

### `command not found: video-stream`

`~/.local/bin` isn't on your PATH. Open a new terminal (the installer adds it to your shell config),
or run `source ~/.zshrc`. To check:

```bash
echo $PATH | tr ':' '\n' | grep '.local/bin'
```

### Port already in use

Another copy is still running, or something else holds the port. Find and stop it:

```bash
lsof -i :8765          # see what's on the port
pkill -f video_stream  # stop a stray instance
```

Or just pick a different port: `video-stream --port 9000`.

### Works on the host, but other machines see nothing

The dashboard and streams load fine on the machine running `video-stream`, yet a browser or
OBS on another computer gets a blank page or a connection error. The stream itself is
healthy — something is blocking the port between the two machines.

Check in this order:

1. **Are you using the LAN URL?** `127.0.0.1` always means *the machine you typed it on*.
   From another computer you must use the `192.168.x.x` address shown in the dashboard.
2. **Firewall on the host.** The most common cause on Linux, where `ufw` is on by default
   (see [Linux notes](#linux-notes)):

   ```bash
   sudo ufw allow 8765/tcp                  # Linux
   ```

   On macOS: System Settings → Network → Firewall.
3. **Same network?** Both machines must be on the same LAN. Guest Wi‑Fi and "client
   isolation" / AP isolation modes block device-to-device traffic by design, even when
   both devices have working internet.

To confirm reachability from the other machine:

```bash
curl -I http://<host-lan-ip>:8765/
```

A `200` means the network path is fine and the problem is in OBS. A hang or "connection
refused" means it's still firewall or network.

### No cameras listed

Grant the OS camera permission when prompted (macOS: System Settings → Privacy & Security → Camera),
then click **Rescan** in the dashboard. Cameras held exclusively by another app (Zoom, Photo Booth,
OBS' own capture) may not appear until that app releases them.

**On Linux**, the usual cause is group permissions rather than the camera itself. Check that the
devices exist and that you can read them:

```bash
ls -l /dev/video*      # nodes present?
id -nG | grep video    # are you in the 'video' group?
```

If `video` is missing, run `sudo usermod -aG video "$USER"` and start a new login session.
See [Linux notes](#linux-notes) for the rest.

## Stack

- **Python 3.10+**
- **FastAPI** + **Uvicorn** — HTTP API & dashboard
- **OpenCV** — camera capture
- **MJPEG** — low-friction streaming for browsers & OBS

## Project layout

```
video-stream/
├── install.sh          # one-shot setup + PATH launcher
├── install-pose.sh     # optional pose-estimation add-on
├── install-avatar.sh   # optional VTuber-avatar assets (three-vrm, mediapipe, model)
├── install-phone.sh    # optional phone-as-camera HTTPS certificate
├── presets/            # director rules example + chaos choreography presets
├── path_b.md           # avatar feature design doc
├── video_stream/
│   ├── app.py          # FastAPI routes + CLI
│   ├── camera.py       # discovery & MJPEG capture (zoom + per-frame CV hooks)
│   ├── pose.py         # optional MediaPipe pose overlay
│   ├── motion.py       # cheap per-camera motion scoring
│   ├── director.py     # auto-switch OBS to the active camera
│   ├── hub.py          # Studio Bus: the /ws WebSocket event hub
│   ├── safety.py       # kill switch + automation rate limiter
│   ├── replay.py       # replay highlights + motion-spike auto-capture
│   ├── overlays.py     # captions/alerts/HUD/stinger overlay pack + APIs
│   ├── chat.py         # unified Twitch + Kick chat aggregator
│   ├── settings.py     # runtime settings registry + token auth
│   ├── setup_wizard.py # scan · propose scene map · verify checklist
│   ├── peers.py        # Rig Link: remote motion signals for the director
│   ├── phone.py        # phone-as-camera signaling + QR + pages
│   ├── chaos.py        # JSON OBS choreography + fx effects engine
│   ├── obs.py          # minimal OBS WebSocket v5 client + audio meters
│   ├── network.py      # LAN IP detection
│   ├── static/         # CSS / JS (+ vendored avatar libs, gitignored)
│   └── templates/      # dashboard · OBS viewer · avatar
├── tests/              # pytest suite (safety · director · zoom)
├── requirements.txt
├── pyproject.toml
└── README.md
```

## License

MIT
