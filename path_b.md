# Path B — Browser VTuber Avatar (scope)

Drive a rigged 3D avatar with your face (and later body) in real time, rendered as a
web page you drop straight into OBS as a **Browser Source** — the exact same pattern
video-stream already uses for camera views. No third-party VTuber app in the loop.

Status: **planning**. Nothing here is built yet. This is the build spec.

---

## 1. The idea in one picture

```
webcam ─▶ MediaPipe FaceLandmarker ─▶ Kalidokit ─▶ three-vrm avatar ─▶ transparent
(browser)  (52 ARKit blendshapes +     (retarget:    (renders the VRM    canvas
           468 face landmarks,          landmarks →   with your head/     │
           runs in-browser via WASM)    bone rots +   face motion)        ▼
                                        expressions)                  OBS Browser
                                                                       Source
```

Everything runs **client-side in the browser**. The FastAPI app just serves the page,
the avatar file, and the tracking model — no server-side CV cost (unlike the pose
overlay and director, which run on the server). That keeps the OBS box doing the work
where the avatar is actually shown.

## 2. What it looks like to use

1. Run `video-stream` as usual.
2. Open **`http://<obs-box>:8765/avatar`** (a new route) — or add it as an OBS Browser
   Source directly.
3. Grant camera access. Your avatar mirrors your head turns, blinks, eyebrows, and
   mouth/jaw in real time, on a transparent background.
4. In OBS the avatar floats over your scene with no green screen (canvas alpha).
5. A small control strip lets you pick the webcam, upload your own `.vrm`, toggle
   mirror, and nudge position/scale.

MVP is **face + head only** — the "close-up talking avatar that's really you" case.
Body and hands are a later phase (see §7).

## 3. Architecture & where tracking runs

**MVP: client-side tracking (recommended).** The `/avatar` page uses `getUserMedia`
to read the local webcam, runs MediaPipe's FaceLandmarker in-browser (WASM), and
renders the avatar. Simplest, lowest latency, works offline once libraries are
vendored, and matches how every Kalidokit demo works.

**Why not reuse the server's MediaPipe?** The server already runs pose for the overlay,
but the avatar needs the *browser* to have the tracking data to render three.js. Sending
landmarks server→browser adds a WebSocket protocol and latency for no MVP benefit when
you're sitting at the OBS box anyway.

**Natural next step that fits the multi-machine rig (Phase 4).** Because the browser can
draw a same-origin MJPEG `<img>` onto a canvas and read its pixels, the avatar page could
take its tracking input from a **video-stream MJPEG feed** (`/stream/{i}`) instead of the
local webcam. That means the *camera can be on another machine*, streamed in by
video-stream, while the avatar renders on the OBS box — very on-brand for this project.
Same in-browser MediaPipe, different frame source. Worth designing toward, not building
first.

## 4. The stack (all browser JS, no new Python deps)

| Library | Role | Notes |
|---|---|---|
| **three.js** (r16x) | 3D scene + renderer | transparent canvas (`alpha: true`) for OBS |
| **@pixiv/three-vrm** (v3) | load & pose VRM avatars | standard humanoid rig + expressions |
| **kalidokit** (1.1) | retarget face landmarks → head rotation + basic face | works on the 468-point landmark array from either MediaPipe API |
| **@mediapipe/tasks-vision** (0.10.x) | in-browser FaceLandmarker | outputs 468 landmarks **and** 52 ARKit blendshapes when `outputFaceBlendshapes: true` |

No `pip` changes — these are all served as static assets.

### Serving the libraries: vendor them, don't CDN

OBS Browser Source is Chromium and *can* hit a CDN, but a LAN/offline tool should not
depend on one. Vendor the JS + the MediaPipe WASM into `static/vendor/` so the whole
thing works with no internet, like the rest of video-stream. Slightly heavier repo,
much more reliable.

### The tracking model

MediaPipe's `face_landmarker.task` (~3.7 MB, includes the blendshape head) downloads
once and is cached — reuse the exact pattern already in `video_stream/pose.py`
(`~/.cache/video-stream`), or vendor it under `static/models/`.

## 5. How the face actually drives the avatar

Two fidelity tiers, and we should support both:

- **Tier 1 — Kalidokit (works on ANY VRM).** Feed the 468 face landmarks into
  Kalidokit's face solver → head rotation, eye open/close, basic mouth shape, pupil
  direction → map onto the VRM's standard expressions (`blink`, `aa/ih/ou/ee/oh`
  visemes, `happy`, etc.). Robust, forgiving, looks good, and works on a generic
  avatar (including a converted Tripo model). **This is the MVP.**

- **Tier 2 — PerfectSync (high fidelity, needs a matching avatar).** MediaPipe's 52
  ARKit blendshapes map 1:1 onto avatars authored with "PerfectSync" blendshapes (many
  VRoid/commercial VRMs have these). Full brow/cheek/mouth expressiveness. Auto-detect
  whether the loaded VRM has the ARKit shapes; if so, drive them directly; otherwise
  fall back to Tier 1.

The mouth being driven by your *actual* mouth (video-driven, not audio lip-sync) is what
makes it read as genuinely you.

## 6. Getting an avatar that works (the real friction point)

The tracking/rendering is the easy part — libraries do it. The friction is the avatar:

- **Fastest:** make one in **VRoid Studio** — exports VRM with expressions (and optional
  PerfectSync) already in place. MVP ships a license-clean sample VRM so it runs day one.
- **Your Tripo model:** geometry is fine, but to be *drivable* it needs (a) a humanoid
  rig — [Mixamo](https://mixamo.com) auto-rigs the body in ~30s — and (b) **face
  blendshapes**, which Tripo does not generate. Adding ARKit/PerfectSync blendshapes is a
  Blender task and is the single biggest time sink. For face-only MVP you at least need
  jaw/eye/mouth morphs. **Plan a "rig your character" session separate from the app work.**
- Convert to VRM via the Blender **VRM add-on**.

This section is the honest "it depends" of the whole project.

## 7. Scope: MVP vs later

**MVP (Phase 1–2) — face-only puppet**
- `/avatar` route, transparent canvas, default sample VRM
- Local webcam → FaceLandmarker → Kalidokit → head + basic face
- Controls: camera picker, VRM upload, mirror, position/scale
- OBS transparency verified

**Later**
- **Phase 3 — body + hands:** MediaPipe Pose + Hands (Holistic-style) → Kalidokit body/hand
  solvers → full/half-body VRM. Bigger; more retargeting jank to tune.
- **Phase 3.5 — PerfectSync tier** for high-fidelity face on supported avatars.
- **Phase 4 — remote camera source:** track from a video-stream MJPEG feed instead of the
  local webcam (the multi-machine payoff, §3).
- **Phase 5 — dashboard integration:** an "Avatar" card/link, per-avatar OBS URL, maybe
  multiple avatars.

## 8. How it slots into the codebase

New files (mirrors the existing template/static layout):
```
video_stream/
├── templates/avatar.html          # the puppet page
├── static/js/avatar.js            # tracking + render loop
├── static/css/avatar.css          # (or inline)
├── static/vendor/                 # three, three-vrm, kalidokit, mediapipe wasm
└── static/models/
    ├── avatar.vrm                 # default sample avatar (license-clean)
    └── face_landmarker.task       # or cached like the pose model
```
`app.py`:
```
@app.get("/avatar")                # serve avatar.html (cache-busted like the dashboard)
# optional: --avatar-vrm <path> flag / config for a default custom avatar
# dashboard: a small "Avatar (beta)" link
```
No changes to the capture loop, streaming, pose, or director paths — this is additive.

## 9. Milestones (each independently demoable)

1. **Static scene** — `/avatar` renders the sample VRM in three.js on a transparent
   canvas, idle. Confirms VRM load + OBS transparency.
2. **Head tracking** — webcam → FaceLandmarker → head bone follows your head turns.
3. **Face** — blinks, brows, mouth/jaw via Kalidokit → looks alive.
4. **Controls + polish** — camera picker, VRM upload, mirror, position/scale, smoothing.
5. **OBS pass** — drop into a real scene, verify transparency + performance.

Ship/checkpoint after each; face-only is a complete, useful result at milestone 4.

## 10. Risks & honest caveats

- **Avatar prep, not code, is the long pole** (§6). Budget it separately.
- **Perf:** in-browser MediaPipe + three.js is fine on a modern machine but not free;
  the OBS box will do real work. Cap render FPS, run tracking at a lower rate than render.
- **Generic-VRM expressiveness:** Tier 1 gives good-not-perfect face; the full 52-shape
  realism needs a PerfectSync avatar (Tier 2).
- **Library churn:** three-vrm/three.js versions must match (three-vrm pins a three range);
  pin exact vendored versions.
- **Uncanny valley / jank:** retargeting always needs smoothing + a bit of hand-tuning.

## 11. Decisions I need from you before building

1. **Default avatar:** ship a generic sample VRM for the MVP, or do you want to prep your
   Tripo character first and build straight against that? (Sample-first is faster to a
   working demo.)
2. **Face-only MVP confirmed?** (Recommended — the close-up case you described.)
3. **Vendored libraries** (offline, heavier repo) vs **CDN** (lighter, needs internet) —
   I recommend vendored to match the project.
4. **Where to render:** always the OBS box for now? (Affects whether we prioritize Phase 4.)
