// Path B — browser VTuber avatar.
//
// (camera OR a video-stream feed) → MediaPipe FaceLandmarker → Kalidokit →
// three-vrm avatar → transparent canvas → OBS Browser Source. Everything runs
// client-side; the server only serves the files. See path_b.md.
//
// URL config (so it works as a hands-off OBS Browser Source):
//   ?obs=1            transparent + hide controls (set by the HTML boot script)
//   ?src=/stream/2    drive tracking from a camera stream instead of the local webcam
//                     (relative = this server; absolute URL = another machine's feed)
//   ?vrm=<url>        load a specific avatar
//   ?mirror=0|1       mirror like a selfie (default 1)
//   ?zoom=<n>         camera distance   ?pan=<y> camera target height
//   ?autostart        begin tracking on load (implied when ?src is present)

import * as THREE from "three";
import { GLTFLoader } from "/static/vendor/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "/static/vendor/three-vrm.module.js";
import * as Kalidokit from "/static/vendor/kalidokit.es.js";
import { FaceLandmarker, PoseLandmarker, FilesetResolver } from "/static/vendor/mediapipe/vision_bundle.mjs";
import { buildVrma, HUMANOID_ORDER, nearestParent } from "/static/js/vrma.js";

// Cache-busted by the static-tree mtime token: swapping avatar.vrm on disk
// changes the URL, so browsers and OBS fetch the new model instead of the
// cached old one; an unchanged model stays cached.
const DEFAULT_VRM = "/static/models/avatar.vrm?v=" + (window.ASSET_V || Date.now());
const FACE_MODEL = "/static/models/face_landmarker.task";
const POSE_MODEL = "/static/models/pose_landmarker_full.task";
const WASM_PATH = "/static/vendor/mediapipe/wasm";

const els = {
  canvas: document.getElementById("stage"),
  video: document.getElementById("webcam"),
  srcImg: document.getElementById("srcimg"),
  status: document.getElementById("status"),
  fps: document.getElementById("fps"),
  bar: document.getElementById("bar"),
  btnTrack: document.getElementById("btn-track"),
  srcSelect: document.getElementById("src-select"),
  vrmFile: document.getElementById("vrm-file"),
  btnBody: document.getElementById("btn-body"),
  btnMirror: document.getElementById("btn-mirror"),
  btnBg: document.getElementById("btn-bg"),
  btnSave: document.getElementById("btn-save"),
  btnGallery: document.getElementById("btn-gallery"),
  btnCopy: document.getElementById("btn-copy"),
  gallery: document.getElementById("gallery"),
  galleryList: document.getElementById("gallery-list"),
  galleryClose: document.getElementById("gallery-close"),
  btnRig: document.getElementById("btn-rig"),
  rig: document.getElementById("rig"),
  rigClose: document.getElementById("rig-close"),
  btnRec: document.getElementById("btn-rec"),
  btnMoves: document.getElementById("btn-moves"),
  moves: document.getElementById("moves"),
  movesList: document.getElementById("moves-list"),
  movesClose: document.getElementById("moves-close"),
};

// ── URL config ────────────────────────────────────────────────────────
const params = new URLSearchParams(location.search);
const initialSrc = params.get("src"); // e.g. "/stream/2" or "http://host:8765/stream/0"
const initialVrm = params.get("vrm");
const urlZoom = parseFloat(params.get("zoom"));
const urlPan = parseFloat(params.get("pan"));
const urlOx = parseFloat(params.get("ox"));
const urlOy = parseFloat(params.get("oy"));
// Avatar Sync: ?follow=1 mirrors the pose broadcast by whichever page is
// actually tracking (over the Studio Bus) instead of tracking locally — the
// mode OBS browser sources want, since they have no webcam of their own.
const follow = params.get("follow") === "1";
const autostart = !follow && (params.has("autostart") || !!initialSrc);

const settings = {
  mirror: params.get("mirror") !== "0",
  body: params.get("body") === "1", // full-body (arms/legs/torso) tracking
  tracking: false,
};
// Current tracking source: a local webcam, or a camera stream URL.
let source = initialSrc
  ? { kind: "stream", url: initialSrc }
  : { kind: "webcam", deviceId: null };

let currentVrm = null;
let currentVrmUrl = initialVrm || DEFAULT_VRM;
let uploadedFile = null; // the local .vrm File (if any), so Save can persist it
let faceLandmarker = null;
let poseLandmarker = null;
let stream = null;

function setStatus(msg, isError = false, autohide = false) {
  els.status.textContent = msg;
  els.status.classList.toggle("err", isError);
  els.status.classList.remove("hide");
  if (autohide) setTimeout(() => els.status.classList.add("hide"), 1800);
}

// Surface runtime errors on-screen instead of failing silently to a blank page.
window.addEventListener("error", (e) =>
  setStatus("error: " + (e.message || e.error || e), true)
);
window.addEventListener("unhandledrejection", (e) =>
  setStatus("error: " + (e.reason?.message || e.reason || e), true)
);

// ── three.js scene ────────────────────────────────────────────────────
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas: els.canvas, alpha: true, antialias: true });
} catch (err) {
  setStatus("WebGL failed to start — this browser/GPU can't render the avatar: " + err, true);
  throw err;
}
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x000000, 0); // transparent for OBS

const scene = new THREE.Scene();

const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 20);
camera.position.set(0, 1.35, 1.15);
const lookTarget = new THREE.Vector3(0, 1.32, 0);
camera.lookAt(lookTarget);

const key = new THREE.DirectionalLight(0xffffff, Math.PI);
key.position.set(1, 1.5, 1.2);
scene.add(key);
// Cool rim light from behind/side for a little dimension.
const rim = new THREE.DirectionalLight(0x88aaff, Math.PI * 0.5);
rim.position.set(-1.2, 1.4, -1.0);
scene.add(rim);
scene.add(new THREE.AmbientLight(0xffffff, Math.PI * 0.4));

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
resize();

// The arm z-axis (swing up/down) points the opposite way in VRM 0.x rigs
// (e.g. Blender-exported models) vs VRM 1.0 rigs. +1 for VRM0, -1 for VRM1 —
// multiply any "arms down" z rotation by this so both spec versions rest and
// track correctly.
function armZSign(vrm) {
  return vrm?.meta?.metaVersion === "0" ? 1 : -1;
}

// Bring the arms down from the default T-pose into a natural rest position.
function applyRestPose(vrm) {
  const h = vrm.humanoid;
  const s = armZSign(vrm);
  const set = (name, x, y, z) => {
    const b = h?.getNormalizedBoneNode(name);
    if (b) b.rotation.set(x, y, z);
  };
  set("leftUpperArm", 0, 0, 1.3 * s);
  set("rightUpperArm", 0, 0, -1.3 * s);
  set("leftLowerArm", 0, 0, 0.1 * s);
  set("rightLowerArm", 0, 0, -0.1 * s);
}

// Camera framing state — adjustable live (scroll zoom, drag pan) or via URL.
// ox/oy are screen-pan offsets so you can position the avatar anywhere.
const view = { cx: 0, cz: 0, targetY: 1.1, distance: 1.5, ox: 0, oy: 0 };

function updateCamera() {
  const x = view.cx + view.ox;
  const y = view.targetY + view.oy;
  lookTarget.set(x, y, view.cz);
  camera.position.set(x, y + 0.12, view.cz + view.distance);
  camera.lookAt(lookTarget);
  camera.updateProjectionMatrix();
}

function frameOnHead(vrm) {
  vrm.scene.updateMatrixWorld(true);
  const head = vrm.humanoid?.getNormalizedBoneNode("head");
  const p = new THREE.Vector3(0, 1.4, 0);
  if (head) head.getWorldPosition(p);
  view.cx = p.x;
  view.cz = p.z;
  view.ox = isNaN(urlOx) ? 0 : urlOx;
  view.oy = isNaN(urlOy) ? 0 : urlOy;
  view.targetY = isNaN(urlPan) ? p.y - 0.35 : urlPan;
  view.distance = isNaN(urlZoom) ? 1.5 : urlZoom;
  updateCamera();
}

// Wider framing for full-body mode (head to feet-ish).
function frameFullBody(vrm) {
  vrm.scene.updateMatrixWorld(true);
  const hips = vrm.humanoid?.getNormalizedBoneNode("hips");
  const head = vrm.humanoid?.getNormalizedBoneNode("head");
  const hp = new THREE.Vector3(0, 0.9, 0);
  const hd = new THREE.Vector3(0, 1.4, 0);
  if (hips) hips.getWorldPosition(hp);
  if (head) head.getWorldPosition(hd);
  view.cx = hp.x;
  view.cz = hp.z;
  view.ox = isNaN(urlOx) ? 0 : urlOx;
  view.oy = isNaN(urlOy) ? 0 : urlOy;
  view.targetY = isNaN(urlPan) ? (hp.y + hd.y) / 2 : urlPan;
  view.distance = isNaN(urlZoom) ? 3.0 : urlZoom;
  updateCamera();
}

// ── VRM loading ───────────────────────────────────────────────────────
async function loadVRM(url) {
  setStatus("loading avatar…");
  const loader = new GLTFLoader();
  loader.register((parser) => new VRMLoaderPlugin(parser));
  try {
    const gltf = await loader.loadAsync(url);
    const vrm = gltf.userData.vrm;
    if (currentVrm) {
      scene.remove(currentVrm.scene);
      VRMUtils.deepDispose(currentVrm.scene);
    }
    currentVrm = vrm;
    VRMUtils.rotateVRM0(vrm);
    VRMUtils.removeUnnecessaryVertices(vrm.scene);
    VRMUtils.combineSkeletons(vrm.scene);
    vrm.scene.traverse((o) => (o.frustumCulled = false));
    scene.add(vrm.scene);
    applyRestPose(vrm);
    if (settings.body) frameFullBody(vrm);
    else frameOnHead(vrm);
    setStatus(settings.tracking ? "tracking" : "avatar loaded — press Start tracking", false, settings.tracking);
  } catch (err) {
    console.error(err);
    setStatus("could not load avatar — is it a valid .vrm? " + (err?.message || ""), true);
  }
}

// ── MediaPipe trackers ────────────────────────────────────────────────
let fileset = null;
async function getFileset() {
  if (!fileset) fileset = await FilesetResolver.forVisionTasks(WASM_PATH);
  return fileset;
}

async function initFaceLandmarker() {
  faceLandmarker = await FaceLandmarker.createFromOptions(await getFileset(), {
    baseOptions: { modelAssetPath: FACE_MODEL, delegate: "GPU" },
    runningMode: "VIDEO",
    numFaces: 1,
    outputFaceBlendshapes: true,
    outputFacialTransformationMatrixes: true,
  });
}

async function initPose() {
  poseLandmarker = await PoseLandmarker.createFromOptions(await getFileset(), {
    baseOptions: { modelAssetPath: POSE_MODEL, delegate: "GPU" },
    runningMode: "VIDEO",
    numPoses: 1,
  });
}

// ── tracking source (webcam or camera stream) ─────────────────────────
const work = document.createElement("canvas"); // scratch for stream frames
const workCtx = work.getContext("2d", { willReadFrequently: true });

async function populateSources() {
  const sel = els.srcSelect;
  const prev = sel.value;
  sel.innerHTML = "";
  const add = (value, label) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
  };
  // Local webcams (labels only appear after camera permission; needs a secure
  // context — works on localhost, not a bare http LAN IP).
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    devices
      .filter((d) => d.kind === "videoinput")
      .forEach((c, i) => add("webcam:" + c.deviceId, (c.label || `Webcam ${i + 1}`) + " · local"));
  } catch {
    /* no mediaDevices (insecure origin) — stream sources still work */
  }
  // This server's cameras (no permission needed; work over plain http).
  try {
    const d = await (await fetch("/api/status")).json();
    (d.cameras || []).forEach((c) =>
      add("stream:/stream/" + c.index, (c.name || `Camera ${c.index}`) + " · stream")
    );
  } catch {
    /* ignore */
  }
  // Make sure a URL-provided ?src is selectable even if it's a remote feed.
  if (initialSrc && ![...sel.options].some((o) => o.value === "stream:" + initialSrc)) {
    add("stream:" + initialSrc, initialSrc + " · stream");
  }
  // Restore selection.
  const want = source.kind === "stream" ? "stream:" + source.url : prev;
  if (want && [...sel.options].some((o) => o.value === want)) sel.value = want;
}

function sourceFromValue(v) {
  if (v.startsWith("webcam:")) return { kind: "webcam", deviceId: v.slice(7) || null };
  if (v.startsWith("stream:")) return { kind: "stream", url: v.slice(7) };
  return { kind: "webcam", deviceId: null };
}

async function startSource(src) {
  source = src;
  if (src.kind === "webcam") {
    els.srcImg.removeAttribute("src");
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = await navigator.mediaDevices.getUserMedia({
      video: src.deviceId ? { deviceId: { exact: src.deviceId } } : { width: 640, height: 480, facingMode: "user" },
      audio: false,
    });
    els.video.srcObject = stream;
    await els.video.play();
    await populateSources(); // labels resolve once permission is granted
  } else {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    els.video.srcObject = null;
    // Cache-bust so re-selecting the same feed reconnects the MJPEG stream.
    els.srcImg.src = src.url + (src.url.includes("?") ? "&" : "?") + "_t=" + Date.now();
  }
}

// Returns the current frame as a MediaPipe ImageSource plus its dimensions.
// Always routed through the work canvas so we can mirror the *frame* (rather than
// negating each bone) — that keeps face and body mirroring consistent.
function currentFrame() {
  let img, w, h;
  if (source.kind === "webcam") {
    if (els.video.readyState < 2 || !els.video.videoWidth) return null;
    img = els.video;
    w = els.video.videoWidth;
    h = els.video.videoHeight;
  } else {
    img = els.srcImg;
    if (!img.naturalWidth) return null;
    w = img.naturalWidth;
    h = img.naturalHeight;
  }
  if (work.width !== w || work.height !== h) {
    work.width = w;
    work.height = h;
  }
  workCtx.save();
  if (settings.mirror) {
    workCtx.translate(w, 0);
    workCtx.scale(-1, 1);
  }
  workCtx.drawImage(img, 0, 0, w, h);
  workCtx.restore();
  return { image: work, w, h };
}

// ── retargeting: Kalidokit rig → VRM ──────────────────────────────────
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function rigRotation(name, rot, dampen = 1, lerp = 0.3) {
  if (!rot) return;
  const bone = currentVrm?.humanoid?.getNormalizedBoneNode(name);
  if (!bone) return;
  // Mirroring is handled by flipping the input frame (see currentFrame), so no
  // per-bone negation is needed here.
  const euler = new THREE.Euler(rot.x * dampen, rot.y * dampen, rot.z * dampen, "XYZ");
  const q = new THREE.Quaternion().setFromEuler(euler);
  bone.quaternion.slerp(q, lerp);
}

function expr(name, value, lerp = 0.4) {
  const em = currentVrm?.expressionManager;
  if (!em || em.getExpression(name) == null) return;
  const cur = em.getValue(name) ?? 0;
  em.setValue(name, cur + (clamp(value, 0, 1) - cur) * lerp);
}

function rigFace(rf) {
  // Nodding was inverted: a positive rotation about a bone's local X pitches it
  // FORWARD, so Kalidokit's "chin up" drove the head down. Measured identical on
  // VRM 0.x and 1.0 rigs — normalised humanoid space is spec-independent here,
  // so this is a flat negation, NOT the spec sign the arms need. Only pitch is
  // wrong; yaw and roll already read the right way round.
  const head = { x: -rf.head.x, y: rf.head.y, z: rf.head.z };
  rigRotation("neck", head, 0.5, 0.35);
  rigRotation("head", head, 0.5, 0.35);

  const eye = Kalidokit.Face.stabilizeBlink(rf.eye, rf.head.y);
  expr("blink", 1 - eye.l, 0.55);

  expr("aa", rf.mouth.shape.A);
  expr("ih", rf.mouth.shape.I);
  expr("ou", rf.mouth.shape.U);
  expr("ee", rf.mouth.shape.E);
  expr("oh", rf.mouth.shape.O);

  if (typeof rf.brow === "number") expr("surprised", clamp(rf.brow * 1.4, 0, 1), 0.3);

  const px = rf.pupil?.x ?? 0; // frame is already mirrored when needed
  const py = rf.pupil?.y ?? 0;
  expr("lookLeft", clamp(px, 0, 1), 0.5);
  expr("lookRight", clamp(-px, 0, 1), 0.5);
  expr("lookUp", clamp(-py, 0, 1), 0.5);
  expr("lookDown", clamp(py, 0, 1), 0.5);
}

// ── idle liveliness ───────────────────────────────────────────────────
// Keeps the avatar breathing/blinking/swaying so she's alive even when no face
// is being tracked. Breathing is always on (doesn't touch the tracked bones);
// the blink + sway only fill in when a face hasn't been seen recently.
let lastFaceAt = -1e9;

function applyBreathing(t) {
  const h = currentVrm?.humanoid;
  const chest =
    h?.getNormalizedBoneNode("chest") ||
    h?.getNormalizedBoneNode("upperChest") ||
    h?.getNormalizedBoneNode("spine");
  if (chest) chest.rotation.x = Math.sin(t * 1.1) * 0.022;
}

function applyIdle(t) {
  const neck = currentVrm?.humanoid?.getNormalizedBoneNode("neck");
  if (neck) {
    const e = new THREE.Euler(Math.sin(t * 0.5) * 0.03, Math.sin(t * 0.35) * 0.07, 0);
    neck.quaternion.slerp(new THREE.Quaternion().setFromEuler(e), 0.05);
  }
  // Natural auto-blink roughly every ~4.5s.
  const cyc = t % 4.5;
  const b = cyc < 0.14 ? Math.sin((cyc / 0.14) * Math.PI) : 0;
  expr("blink", b, 0.6);
}

// Body pose (arms, legs, torso) from Kalidokit's pose solver.
function rigPose(rp) {
  if (!rp) return;
  if (rp.Hips) rigRotation("hips", rp.Hips.rotation, 0.7, 0.3);
  if (rp.Spine) {
    rigRotation("chest", rp.Spine, 0.25, 0.3);
    rigRotation("spine", rp.Spine, 0.45, 0.3);
  }
  // Fix the arm swing axis (z): Kalidokit's output matches VRM0 rigs directly
  // but is vertically inverted against VRM1 rigs — same sign rule as the rest
  // pose, so arms-up reads as up on both spec versions.
  const s = armZSign(currentVrm);
  const armFix = (r) => (r ? { x: r.x, y: r.y, z: r.z * s } : r);
  rigRotation("rightUpperArm", armFix(rp.RightUpperArm), 1, 0.3);
  rigRotation("rightLowerArm", armFix(rp.RightLowerArm), 1, 0.3);
  rigRotation("leftUpperArm", armFix(rp.LeftUpperArm), 1, 0.3);
  rigRotation("leftLowerArm", armFix(rp.LeftLowerArm), 1, 0.3);
  rigRotation("rightUpperLeg", rp.RightUpperLeg, 1, 0.3);
  rigRotation("rightLowerLeg", rp.RightLowerLeg, 1, 0.3);
  rigRotation("leftUpperLeg", rp.LeftUpperLeg, 1, 0.3);
  rigRotation("leftLowerLeg", rp.LeftLowerLeg, 1, 0.3);
}

// ── Avatar Sync: one page tracks, every follower mirrors ─────────────
// The driver publishes the SOLVED Kalidokit rigs (small plain objects) over
// the Studio Bus; followers feed them straight into rigFace/rigPose, whose
// built-in lerp smooths network jitter for free.
let lastSyncSent = 0;
let followRig = null;
let followAt = -1e9;

const slimXYZ = (r) => (r ? { x: r.x || 0, y: r.y || 0, z: r.z || 0 } : null);

function slimFace(rf) {
  return {
    head: slimXYZ(rf.head),
    eye: { l: rf.eye?.l ?? 1, r: rf.eye?.r ?? 1 },
    mouth: {
      shape: {
        A: rf.mouth?.shape?.A || 0,
        I: rf.mouth?.shape?.I || 0,
        U: rf.mouth?.shape?.U || 0,
        E: rf.mouth?.shape?.E || 0,
        O: rf.mouth?.shape?.O || 0,
      },
    },
    brow: typeof rf.brow === "number" ? rf.brow : 0,
    pupil: { x: rf.pupil?.x || 0, y: rf.pupil?.y || 0 },
  };
}

function slimPose(rp) {
  if (!rp) return null;
  return {
    Hips: { rotation: slimXYZ(rp.Hips?.rotation) },
    Spine: slimXYZ(rp.Spine),
    RightUpperArm: slimXYZ(rp.RightUpperArm),
    RightLowerArm: slimXYZ(rp.RightLowerArm),
    LeftUpperArm: slimXYZ(rp.LeftUpperArm),
    LeftLowerArm: slimXYZ(rp.LeftLowerArm),
    RightUpperLeg: slimXYZ(rp.RightUpperLeg),
    RightLowerLeg: slimXYZ(rp.RightLowerLeg),
    LeftUpperLeg: slimXYZ(rp.LeftUpperLeg),
    LeftLowerLeg: slimXYZ(rp.LeftLowerLeg),
  };
}

if (follow && window.Bus) {
  Bus.on("avatar_rig", (r) => {
    if (r && r.f) {
      followRig = r;
      followAt = performance.now();
    }
  });
}

// ── Moves: record a performance, replay it on any avatar ─────────────
// A move is the same solved rig stream Avatar Sync broadcasts, written down
// with timestamps. Playback feeds the frames back through rigFace/rigPose —
// the exact path live tracking takes — so one recording drives every avatar,
// VRM 0.x or 1.0, and the built-in lerp gives the same smoothing as live.
const REC_MAX_SECONDS = 120;
const rec = { state: "idle", frames: [], t0: 0, last: 0, timer: null, take: null };
const player = { clip: null, t0: 0, i: 0, loop: false, from: 0, to: 0 };
const clipCache = new Map();

function authHeaders(extra = {}) {
  const headers = { ...extra };
  try {
    const token = localStorage.getItem("vs-token"); // set on the dashboard's Setup panel
    if (token) headers["X-Auth-Token"] = token;
  } catch {}
  return headers;
}

function recordFrame(now, rf, rp) {
  if (rec.state !== "recording" || now - rec.last < 30) return; // ~30 frames a second
  rec.last = now;
  rec.frames.push({ t: (now - rec.t0) / 1000, f: slimFace(rf), p: rp ? slimPose(rp) : null });
  if ((now - rec.t0) / 1000 >= REC_MAX_SECONDS) stopRecording();
}

// Bones a clip may have moved that nothing else will put back.
const BODY_BONES = [
  "hips", "spine", "chest",
  "leftUpperArm", "leftLowerArm", "rightUpperArm", "rightLowerArm",
  "leftUpperLeg", "leftLowerLeg", "rightUpperLeg", "rightLowerLeg",
];

function settleBody() {
  // Live body tracking will take the limbs back over by itself; without it,
  // return them to the arms-down rest pose instead of freezing mid-gesture.
  if (!currentVrm) return;
  const bone = (name) => currentVrm.humanoid?.getNormalizedBoneNode(name);
  // With no face being tracked the head would stay cocked at the last frame.
  if (!settings.tracking && !follow) bone("head")?.quaternion.identity();
  if ((settings.tracking && settings.body) || (follow && followRig?.p)) return;
  for (const name of BODY_BONES) bone(name)?.quaternion.identity();
  applyRestPose(currentVrm);
}

function playClip(clip, { loop = false, from = 0, to = null } = {}) {
  if (!clip?.frames?.length) return;
  player.clip = clip;
  player.loop = loop;
  player.from = Math.max(0, from);
  player.to = to == null ? clip.frames[clip.frames.length - 1].t : to;
  player.t0 = performance.now();
  player.i = 0;
  onPlayerChange();
}

function stopClip() {
  if (!player.clip) return;
  player.clip = null;
  settleBody();
  onPlayerChange();
}

// The frame to perform right now, or null when nothing is playing.
function playerFrame(now) {
  const clip = player.clip;
  if (!clip) return null;
  let t = player.from + (now - player.t0) / 1000;
  if (t >= player.to) {
    if (!player.loop) {
      stopClip();
      return null;
    }
    player.t0 = now;
    player.i = 0;
    t = player.from;
  }
  const frames = clip.frames;
  while (player.i + 1 < frames.length && frames[player.i + 1].t <= t) player.i++;
  return frames[player.i];
}

async function fetchClip(id) {
  if (clipCache.has(id)) return clipCache.get(id);
  const res = await fetch(`/api/avatar/animations/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error("that move no longer exists");
  const clip = await res.json();
  clipCache.set(id, clip);
  return clip;
}

// Every avatar page performs a triggered move — the studio tab and each OBS
// browser source alike — so an emote needs no tracker page open at all.
if (window.Bus) {
  Bus.on("avatar_play", async (msg) => {
    if (!msg?.id || rec.state === "recording") return;
    try {
      playClip(await fetchClip(msg.id), { loop: !!msg.loop });
    } catch (err) {
      setStatus(String(err?.message || err), true, true);
    }
  });
  Bus.on("avatar_stop", () => stopClip());
  Bus.on("avatar_animations", (msg) => {
    if (msg?.changed) clipCache.delete(msg.changed);
    if (!els.moves.classList.contains("hidden")) renderMoves();
  });
}

// ── main loop ─────────────────────────────────────────────────────────
const clock = new THREE.Clock();
let lastWebcamTime = -1;
let lastDetect = 0;
let frames = 0;
let fpsAt = performance.now();

function shouldDetect(now) {
  if (source.kind === "webcam") {
    if (els.video.currentTime === lastWebcamTime) return false;
    lastWebcamTime = els.video.currentTime;
    return true;
  }
  if (now - lastDetect < 33) return false; // ~30fps cap for stream sources
  lastDetect = now;
  return true;
}

function animate() {
  requestAnimationFrame(animate);
  const dt = clock.getDelta();

  if (settings.tracking && faceLandmarker) {
    const now = performance.now();
    if (shouldDetect(now)) {
      const frame = currentFrame();
      if (frame) {
        let sentFace = null;
        let sentPose = null;
        const res = faceLandmarker.detectForVideo(frame.image, now);
        const lm = res.faceLandmarks?.[0];
        if (lm) {
          const rf = Kalidokit.Face.solve(lm, {
            runtime: "mediapipe",
            imageSize: { width: frame.w, height: frame.h },
            smoothBlink: true,
          });
          if (rf) {
            if (!player.clip) rigFace(rf); // a playing move has the avatar
            lastFaceAt = now;
            sentFace = rf;
          }
        }
        // Full-body pose (opt-in) — arms, legs, torso.
        if (settings.body && poseLandmarker) {
          const pres = poseLandmarker.detectForVideo(frame.image, now);
          const p3d = pres.worldLandmarks?.[0];
          const p2d = pres.landmarks?.[0];
          if (p3d && p2d) {
            const rp = Kalidokit.Pose.solve(p3d, p2d, {
              runtime: "mediapipe",
              imageSize: { width: frame.w, height: frame.h },
              enableLegs: true,
            });
            if (rp) {
              if (!player.clip) rigPose(rp);
              sentPose = rp;
            }
          }
        }
        if (sentFace) recordFrame(now, sentFace, sentPose);
        // Avatar Sync driver: broadcast the solved pose so OBS followers on
        // other machines mirror this page exactly (~30Hz, <1KB).
        if (sentFace && !follow && window.Bus && now - lastSyncSent > 33) {
          lastSyncSent = now;
          Bus.publish("avatar_rig", {
            f: slimFace(sentFace),
            p: sentPose ? slimPose(sentPose) : null,
          });
        }
        frames++;
        if (now - fpsAt > 1000) {
          els.fps.textContent = `${frames} fps`;
          frames = 0;
          fpsAt = now;
        }
      }
    }
  }

  // Avatar Sync follower: mirror the driver page's latest pose. Reapplying
  // each frame lets rigFace/rigPose's lerp glide between network updates;
  // if the driver goes quiet the idle takes over naturally.
  // A recorded move, when one is playing, outranks live tracking and follow.
  const moveFrame = playerFrame(performance.now());
  if (moveFrame) {
    rigFace(moveFrame.f);
    if (moveFrame.p) rigPose(moveFrame.p);
    lastFaceAt = performance.now();
  } else if (follow && followRig && performance.now() - followAt < 1500) {
    rigFace(followRig.f);
    if (followRig.p) rigPose(followRig.p);
    lastFaceAt = performance.now();
  }

  if (currentVrm) {
    const tSec = clock.elapsedTime;
    applyBreathing(tSec);
    if (performance.now() - lastFaceAt > 400) applyIdle(tSec); // fill in when untracked
    currentVrm.update(dt);
  }
  renderer.render(scene, camera);
}

// ── controls ──────────────────────────────────────────────────────────
async function startTracking() {
  try {
    setStatus("starting…");
    if (!faceLandmarker) await initFaceLandmarker();
    const v = els.srcSelect.value;
    await startSource(v ? sourceFromValue(v) : source);
    settings.tracking = true;
    els.btnTrack.textContent = "Stop tracking";
    els.btnTrack.classList.add("rec");
    setStatus("tracking", false, true);
  } catch (err) {
    console.error(err);
    setStatus("tracking failed: " + (err?.message || err), true);
  }
}

function stopTracking() {
  settings.tracking = false;
  els.btnTrack.textContent = "Start tracking";
  els.btnTrack.classList.remove("rec");
  if (stream) stream.getTracks().forEach((t) => t.stop());
  setStatus("tracking paused", false, true);
}

els.btnTrack.addEventListener("click", () => (settings.tracking ? stopTracking() : startTracking()));

els.srcSelect.addEventListener("change", () => {
  if (settings.tracking) startSource(sourceFromValue(els.srcSelect.value)).catch((e) => setStatus(String(e), true));
});

els.vrmFile.addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  if (file) {
    uploadedFile = file; // kept so Save can upload it to the gallery
    currentVrmUrl = null; // a local blob can't be shared to another machine's OBS
    loadVRM(URL.createObjectURL(file));
  }
});

async function setBody(on) {
  settings.body = on;
  els.btnBody.classList.toggle("on", on);
  if (on) {
    if (!poseLandmarker) {
      setStatus("loading body model…");
      await initPose();
    }
    if (currentVrm) frameFullBody(currentVrm);
    setStatus("full-body tracking on — stand back so your body is in frame", false, true);
  } else if (currentVrm) {
    applyRestPose(currentVrm); // arms back to rest
    frameOnHead(currentVrm);
    setStatus("full-body off", false, true);
  }
}
els.btnBody.addEventListener("click", () =>
  setBody(!settings.body).catch((e) => {
    settings.body = false;
    els.btnBody.classList.remove("on");
    setStatus("body model failed: " + (e?.message || e), true);
  })
);

els.btnMirror.addEventListener("click", () => {
  settings.mirror = !settings.mirror;
  els.btnMirror.classList.toggle("on", settings.mirror);
});

els.btnBg.addEventListener("click", () => {
  document.body.classList.toggle("preview-bg");
  els.btnBg.classList.toggle("on");
});

// The OBS URL must reproduce the CURRENT look on another machine. A model
// loaded from a local file only exists in this browser session — push it to
// the gallery first so OBS elsewhere can fetch it by URL.
async function shareableVrmUrl() {
  if (currentVrmUrl) return currentVrmUrl;
  if (!uploadedFile) return null;
  setStatus("uploading avatar so OBS on other machines can load it…");
  const res = await fetch("/api/avatar/vrm", { method: "POST", body: uploadedFile });
  if (!res.ok) throw new Error("avatar upload failed");
  const d = await res.json();
  currentVrmUrl = d.url; // from now on this session shares the server copy
  return currentVrmUrl;
}

// Build a ready-to-paste OBS Browser Source URL from the current setup, using the
// machine's LAN IP so it works from OBS on another computer.
async function copyObsUrl() {
  let host = location.host;
  try {
    const d = await (await fetch("/api/status")).json();
    if (d.primary_ip) host = d.primary_ip + ":" + (d.port || location.port || 8765);
  } catch {
    /* fall back to current host */
  }
  let vrmUrl = null;
  try {
    vrmUrl = await shareableVrmUrl();
  } catch {
    setStatus("could not upload this avatar — try Save preset first, then copy", true);
    return;
  }
  const p = new URLSearchParams();
  p.set("obs", "1");
  // Webcam-driven pages can't share their camera with another machine — the
  // OBS copy mirrors THIS page's live tracking over the Studio Bus instead.
  if (source.kind === "stream") p.set("src", source.url);
  else p.set("follow", "1");
  if (vrmUrl && vrmUrl !== DEFAULT_VRM) p.set("vrm", vrmUrl);
  if (settings.body) p.set("body", "1");
  p.set("mirror", settings.mirror ? "1" : "0");
  p.set("zoom", view.distance.toFixed(2));
  p.set("pan", view.targetY.toFixed(3));
  if (view.ox) p.set("ox", view.ox.toFixed(3));
  if (view.oy) p.set("oy", view.oy.toFixed(3));
  const url = `${location.protocol}//${host}/avatar?${p.toString()}`;
  try {
    await navigator.clipboard.writeText(url);
    setStatus("OBS URL copied ✓  (add as a Browser Source)", false, true);
  } catch {
    setStatus(url, false); // clipboard blocked — show it to copy manually
  }
}
els.btnCopy.addEventListener("click", copyObsUrl);

// ── gallery / presets ─────────────────────────────────────────────────
function currentSettings() {
  return {
    mirror: settings.mirror,
    body: settings.body,
    zoom: +view.distance.toFixed(2),
    pan: +view.targetY.toFixed(3),
    ox: +view.ox.toFixed(3),
    oy: +view.oy.toFixed(3),
    src: source.kind === "stream" ? source.url : null,
  };
}

async function saveToGallery() {
  const name = prompt("Name this avatar:", "My Avatar");
  if (name === null) return;
  els.btnSave.disabled = true;
  try {
    // If it's a locally-uploaded VRM, persist it server-side so it survives and
    // can be loaded from OBS on any machine.
    let vrmUrl = currentVrmUrl;
    if (!vrmUrl && uploadedFile) {
      setStatus("uploading avatar…");
      const res = await fetch("/api/avatar/vrm", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: uploadedFile,
      });
      if (!res.ok) throw new Error("VRM upload failed");
      vrmUrl = (await res.json()).url;
      currentVrmUrl = vrmUrl; // now shareable
    }
    const res = await fetch("/api/avatar/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, vrm: vrmUrl, settings: currentSettings() }),
    });
    if (!res.ok) throw new Error("save failed");
    setStatus("saved to gallery ✓", false, true);
  } catch (err) {
    setStatus("save failed: " + (err?.message || err), true);
  } finally {
    els.btnSave.disabled = false;
  }
}
els.btnSave.addEventListener("click", saveToGallery);

async function loadPreset(p) {
  const s = p.settings || {};
  settings.mirror = s.mirror !== false;
  els.btnMirror.classList.toggle("on", settings.mirror);
  if (s.src) source = { kind: "stream", url: s.src };
  if (p.vrm) {
    currentVrmUrl = p.vrm;
    uploadedFile = null;
    await loadVRM(p.vrm);
  }
  // Apply framing after the avatar loads (loadVRM reframes to defaults first).
  if (typeof s.zoom === "number") view.distance = s.zoom;
  if (typeof s.pan === "number") view.targetY = s.pan;
  view.ox = typeof s.ox === "number" ? s.ox : 0;
  view.oy = typeof s.oy === "number" ? s.oy : 0;
  updateCamera();
  await setBody(!!s.body);
  els.gallery.classList.add("hidden");
  setStatus("loaded " + (p.name || "preset"), false, true);
}

function presetObsUrl(p) {
  const s = p.settings || {};
  const q = new URLSearchParams({ obs: "1" });
  if (p.vrm && p.vrm !== DEFAULT_VRM) q.set("vrm", p.vrm);
  if (s.src) q.set("src", s.src);
  else q.set("follow", "1"); // no shareable camera → mirror the live tracker
  if (s.body) q.set("body", "1");
  q.set("mirror", s.mirror === false ? "0" : "1");
  if (s.zoom != null) q.set("zoom", s.zoom);
  if (s.pan != null) q.set("pan", s.pan);
  if (s.ox) q.set("ox", s.ox);
  if (s.oy) q.set("oy", s.oy);
  return q;
}

async function renderGallery() {
  els.galleryList.innerHTML = "<div class='gallery-empty'>loading…</div>";
  let presets = [];
  try {
    presets = (await (await fetch("/api/avatar/presets")).json()).presets || [];
  } catch {
    els.galleryList.innerHTML = "<div class='gallery-empty'>couldn't load gallery</div>";
    return;
  }
  if (!presets.length) {
    els.galleryList.innerHTML =
      "<div class='gallery-empty'>No saved avatars yet.<br>Set one up and press <b>Save</b>.</div>";
    return;
  }
  let host = location.host;
  try {
    const d = await (await fetch("/api/status")).json();
    if (d.primary_ip) host = d.primary_ip + ":" + (d.port || location.port || 8765);
  } catch {}
  els.galleryList.innerHTML = "";
  presets.forEach((p) => {
    const s = p.settings || {};
    const tags = [s.body ? "full-body" : "face", s.src ? "stream" : "webcam"].join(" · ");
    const item = document.createElement("div");
    item.className = "g-item";
    item.innerHTML =
      `<div class="g-name"></div><div class="g-tags">${tags}</div>` +
      `<div class="g-actions"><button class="g-load">Load</button>` +
      `<button class="g-obs">Copy OBS URL</button>` +
      `<button class="g-del">Delete</button></div>`;
    item.querySelector(".g-name").textContent = p.name || "Avatar";
    item.querySelector(".g-load").addEventListener("click", () => loadPreset(p));
    item.querySelector(".g-obs").addEventListener("click", async () => {
      const url = `${location.protocol}//${host}/avatar?${presetObsUrl(p).toString()}`;
      try {
        await navigator.clipboard.writeText(url);
        setStatus("OBS URL copied ✓", false, true);
      } catch {
        setStatus(url, false);
      }
    });
    item.querySelector(".g-del").addEventListener("click", async () => {
      if (!confirm(`Delete "${p.name}"?`)) return;
      await fetch("/api/avatar/presets/" + p.id, { method: "DELETE" });
      renderGallery();
    });
    els.galleryList.appendChild(item);
  });
}

els.btnGallery.addEventListener("click", () => {
  els.rig?.classList.add("hidden"); // all three slide in from the right edge
  els.moves.classList.add("hidden");
  els.gallery.classList.toggle("hidden");
  if (!els.gallery.classList.contains("hidden")) renderGallery();
});
els.galleryClose.addEventListener("click", () => els.gallery.classList.add("hidden"));

// Scroll to zoom; drag to move the avatar around the screen; double-click to recenter.
els.canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    view.distance = clamp(view.distance * (e.deltaY > 0 ? 1.1 : 0.9), 0.3, 10);
    updateCamera();
  },
  { passive: false }
);
let dragging = false;
let dragX = 0;
let dragY = 0;
els.canvas.addEventListener("pointerdown", (e) => {
  dragging = true;
  dragX = e.clientX;
  dragY = e.clientY;
});
window.addEventListener("pointerup", () => (dragging = false));
window.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  const k = view.distance * 0.0016; // pan speed scales with zoom
  view.ox -= (e.clientX - dragX) * k; // drag right → avatar moves right
  view.oy += (e.clientY - dragY) * k; // drag down → avatar moves down
  dragX = e.clientX;
  dragY = e.clientY;
  updateCamera();
});
els.canvas.addEventListener("dblclick", () => {
  if (currentVrm) (settings.body ? frameFullBody : frameOnHead)(currentVrm);
});

// ── Rig Studio: raw .glb in, drivable VRM out ─────────────────────────
// The server does the work in headless Blender; this panel is the part you
// look at. It matters that you can SEE the result — a rig that binds the mesh
// but blinks the wrong spot looks identical in JSON — so every stage shows a
// render, and the eyes/mouth are placeable by clicking the face.
const rig = {
  status: null,     // /api/rig payload
  source: null,     // the uploaded model
  analysis: null,   // what Blender measured and guessed
  archetype: null,  // the kind of creature, once chosen
  eyes: [],         // hand-placed eye centres, normalised to the head shot
  mouth: null,
  target: "avatar", // what the rig is for: this studio's avatar, or a Code27
  faceFront: true,  // false once the user says the front/back guess was wrong
  job: null,        // the running (or finished) job snapshot
  busy: false,
};

const KIND_HINT = "Pick what it is — that decides where the bones go.";

// What the finished avatar is for. The rig is the same; the extras differ.
const RIG_TARGETS = [
  {
    id: "avatar",
    label: "This studio",
    hint: "For the avatar here and in OBS. Keeps the model's own materials.",
  },
  {
    id: "code27",
    label: "Code27",
    hint:
      "For a Code27 companion device. Switches to VRM's toon shader (MToon) and adds finger bones where the hands can be found, so its animations and gestures have something to move.",
  },
];
const CHECK_ICON = { pass: "✓", warn: "!", fail: "✕", info: "·" };

// The Code27 checklist the server ran on the finished file. It mirrors what
// Code27 publishes about custom characters; the device's own import check is
// the final word, and the note under the list says so.
function rigCheckHtml(check) {
  if (!check || !Array.isArray(check.items) || !check.items.length) return "";
  const c = check.counts || {};
  const verdict =
    check.verdict === "fail"
      ? `<span class="rig-verdict fail">${c.fail} problem${c.fail === 1 ? "" : "s"}</span>`
      : check.verdict === "warn"
        ? `<span class="rig-verdict warn">${c.warn} to know about</span>`
        : `<span class="rig-verdict pass">nothing missing</span>`;
  const rows = check.items
    .map(
      (i) =>
        `<li class="${escapeHtml(i.level)}"><i>${CHECK_ICON[i.level] || "·"}</i>` +
        `<div><b>${escapeHtml(i.title)}</b><span>${escapeHtml(i.detail)}</span></div></li>`
    )
    .join("");
  return (
    `<div class="rig-check"><h4>Code27 checklist ${verdict}</h4><ul>${rows}</ul>` +
    `<p class="muted">${escapeHtml(check.note || "")}</p></div>`
  );
}

function rigSet(html) {
  document.getElementById("rig-body").innerHTML = html;
}

function rigFoot(text, cls = "") {
  const foot = document.getElementById("rig-foot");
  foot.className = "gallery-foot " + cls;
  foot.textContent = text;
}

async function rigRefresh() {
  try {
    rig.status = await (await fetch("/api/rig")).json();
  } catch {
    rig.status = { available: false, error: "couldn't reach the server", archetypes: [] };
    return;
  }
  // Reopening the panel (or reloading the page) mid-rig should pick the job
  // back up rather than offering to start over while Blender is still going.
  if (!rig.job || !rig.busy) {
    const live = (rig.status.jobs || []).find((j) => j.state === "running" || j.state === "queued");
    if (live) {
      rig.job = live;
      rig.busy = true;
      rigPoll();
    }
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function rigRender() {
  if (!rig.status) return;
  if (!rig.status.available) {
    rigSet(
      `<div><h4>Blender isn't set up</h4>` +
        `<p>Auto-rigging runs in <b>Blender</b> with the <b>VRM add-on</b>, headless on this machine. ` +
        `Neither costs anything and it's a one-time setup:</p>` +
        `<p class="muted">1 · Install Blender from <b>blender.org</b>.<br>` +
        `2 · Open it → Edit → Preferences → Get Extensions → search <b>VRM</b> → install ` +
        `<b>VRM Add-on for Blender</b>.<br>3 · Come back and hit Re-check.</p>` +
        `<p class="bad">${escapeHtml(rig.status.error || "")}</p></div>` +
        `<button id="rig-recheck" type="button">Re-check</button>`
    );
    document.getElementById("rig-recheck").addEventListener("click", async () => {
      rigFoot("looking for Blender…");
      rig.status = await (await fetch("/api/rig/rescan", { method: "POST" })).json();
      rigRender();
      rigFoot(rig.status.available ? "Blender found ✓" : "still not finding it", rig.status.available ? "" : "bad");
    });
    return;
  }

  if (rig.job && rig.job.state === "done" && rig.job.kind === "rig") return rigRenderResult();
  if (rig.busy) return rigRenderProgress();
  if (rig.analysis) return rigRenderSetup();
  rigRenderDrop();
}

function rigRenderDrop() {
  rigSet(
    `<div class="rig-drop" id="rig-drop">` +
      `<p><b>Drop a model here</b></p>` +
      `<p class="muted">.glb · .gltf · .fbx — straight out of Tripo, Meshy, Blender, anywhere</p>` +
      `<p style="margin-top:10px"><label class="file">Choose a file<input id="rig-file" type="file" accept=".glb,.gltf,.fbx" /></label></p>` +
    `</div>` +
    `<p class="muted">It gets a skeleton, skin weights, blink and mouth shapes, then comes back as a VRM ` +
      `you can drive with your face — about a minute, all on this machine.</p>`
  );
  const drop = document.getElementById("rig-drop");
  document.getElementById("rig-file").addEventListener("change", (e) => {
    if (e.target.files?.[0]) rigUpload(e.target.files[0]);
  });
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove("over");
    })
  );
  drop.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) rigUpload(file);
  });
}

async function rigUpload(file) {
  const mb = (file.size / 1e6).toFixed(0);
  rigFoot(`uploading ${file.name} (${mb} MB)…`);
  rig.busy = true;
  rigRenderProgress("uploading the model", 0.05);
  try {
    const res = await fetch("/api/rig/sources", {
      method: "POST",
      // Header values must be Latin-1; a model called ペンギン.glb would
      // throw before the request left the page.
      headers: { "x-file-name": encodeURIComponent(file.name) },
      body: file,
    });
    if (!res.ok) throw new Error((await res.json()).detail || "upload failed");
    rig.source = await res.json();
    rig.analysis = null;
    rig.eyes = [];
    rig.mouth = null;
    rig.faceFront = true;
    await rigStart("analyze", { source_id: rig.source.id });
  } catch (err) {
    rig.busy = false;
    rigRender();
    rigFoot(String(err.message || err), "bad");
  }
}

async function rigStart(kind, body) {
  const url = kind === "analyze" ? "/api/rig/analyze" : "/api/rig/jobs";
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json()).detail || `${kind} failed to start`);
  rig.job = await res.json();
  rig.busy = true;
  rigRenderProgress();
  rigPoll();
}

// The bus pushes progress, but a page that just opened (or a dropped socket)
// still needs an answer — so poll slowly as a backstop and let the bus make it
// feel live.
let rigPollTimer = null;
function rigPoll() {
  clearTimeout(rigPollTimer);
  rigPollTimer = setTimeout(async () => {
    if (!rig.job || !rig.busy) return;
    try {
      const res = await fetch("/api/rig/jobs/" + rig.job.id);
      if (res.status === 404) {
        // The server forgot this job (restarted, or it aged out of the
        // history). Polling forever behind a frozen bar helps nobody.
        rig.busy = false;
        rig.job = null;
        rigRender();
        rigFoot("lost track of that job — the server restarted. Try again.", "bad");
        return;
      }
      rigOnJob(await res.json());
    } catch {
      /* the bus or the next poll will catch up */
    }
    if (rig.busy) rigPoll();
  }, 2000);
}

const TERMINAL = ["done", "failed", "cancelled"];

function rigOnJob(job) {
  if (!job || !rig.job || job.id !== rig.job.id) return;
  // The bus re-sends retained state to every new subscriber, so a finished job
  // arrives again whenever the socket reconnects. Handling it twice would
  // rebuild the setup screen underneath the user — throwing away the archetype
  // they picked and the name they typed.
  if (TERMINAL.includes(rig.job.state) && rig.job.state === job.state) return;
  rig.job = job;
  if (job.state === "running" || job.state === "queued") {
    rigRenderProgress();
    return;
  }
  rig.busy = false;
  clearTimeout(rigPollTimer);
  if (job.state !== "done") {
    rigRender();
    rigFoot(job.error ? job.error.split("\n")[0].slice(0, 160) : "cancelled", "bad");
    return;
  }
  if (job.kind === "analyze") {
    rig.analysis = job.result;
    rig.archetype = job.result.archetype;
    rigRenderSetup();
    rigFoot("Check the guess, then rig it.");
  } else {
    rigRenderResult();
    rigFoot("Rigged ✓ — load it, or save it to your gallery.");
  }
}

function rigRenderProgress(step, progress) {
  const job = rig.job || {};
  const label = step || job.step || "working…";
  const pct = Math.round(100 * (progress != null ? progress : job.progress || 0));
  const log = (job.log || []).slice(-8).join("\n");
  rigSet(
    `<div><h4>${job.kind === "rig" ? "Rigging" : "Reading the model"}</h4>` +
      `<p>${escapeHtml(label)} · ${pct}%</p>` +
      `<div class="rig-bar"><span style="width:${pct}%"></span></div></div>` +
      (log ? `<div class="rig-log">${escapeHtml(log)}</div>` : "") +
      // Only offer Cancel once there's a job to cancel; during the upload
      // there's nothing on the server yet to stop.
      (rig.job ? `<button id="rig-cancel" type="button">Cancel</button>` : "")
  );
  const cancel = document.getElementById("rig-cancel");
  if (cancel)
    cancel.addEventListener("click", async () => {
      if (rig.job) await fetch(`/api/rig/jobs/${rig.job.id}/cancel`, { method: "POST" });
      rig.busy = false;
      rigRender();
    });
}

function rigRenderSetup() {
  const a = rig.analysis;
  const kinds = (rig.status.archetypes || [])
    .map(
      (k) =>
        `<button class="rig-kind${k.id === rig.archetype ? " on" : ""}" data-kind="${k.id}" ` +
        `title="${escapeHtml(k.description)}">${escapeHtml(k.label)}</button>`
    )
    .join("");
  const chosen = (rig.status.archetypes || []).find((k) => k.id === rig.archetype);
  const turned = a.facing?.turned_deg;
  rigSet(
    `<div class="rig-shots"><img src="${a.previews.front}" alt="front"><img src="${a.previews.side}" alt="side"></div>` +
      `<p class="muted">Turned ${turned}° to face front${turned ? "" : " (it already did)"} · ` +
      `${a.measure.tris.toLocaleString()} triangles after simplifying</p>` +
      `<p class="rig-row"><button id="rig-flip" type="button" title="The first picture should show its face. If it shows its back, press this.">` +
      `Showing its back? Turn it round</button></p>` +
      `<div><h4>What is it?</h4><div class="rig-kinds">${kinds}</div>` +
      `<ul class="rig-why">${(a.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` +
      `<p class="muted" id="rig-kind-desc">${escapeHtml(chosen ? chosen.description : KIND_HINT)}</p></div>` +
      `<hr>` +
      `<div><h4>Face</h4><p class="muted">Click the left eye, the right eye, then the mouth. ` +
      `Skip it and they're guessed from the shape — usually close, rarely perfect.</p>` +
      `<div class="rig-face" id="rig-face"><img id="rig-face-img" src="${a.previews.head}" alt="head"></div>` +
      `<p class="rig-row" style="margin-top:8px"><button id="rig-face-reset" type="button">Clear marks</button>` +
      `<span class="muted" id="rig-face-state"></span></p></div>` +
      `<hr>` +
      `<div><h4>Made for</h4><div class="rig-kinds">${RIG_TARGETS.map(
        (t) =>
          `<button class="rig-kind${t.id === rig.target ? " on" : ""}" data-target="${t.id}" type="button">${escapeHtml(t.label)}</button>`
      ).join("")}</div>` +
      `<p class="muted" id="rig-target-desc" style="margin-top:8px">${escapeHtml(
        (RIG_TARGETS.find((t) => t.id === rig.target) || RIG_TARGETS[0]).hint
      )}</p></div>` +
      `<hr>` +
      `<div><h4>Details</h4>` +
      `<p class="rig-row"><input id="rig-name" type="text" placeholder="Name" value="${escapeHtml(
        rig.source?.name?.replace(/\.[^.]+$/, "") || "Avatar"
      )}"></p>` +
      `<p class="rig-row" style="margin-top:8px"><span class="muted">Height</span>` +
      `<input id="rig-height" type="number" step="0.05" min="0.1" max="4" value="${a.suggested_height_m}"><span class="muted">m</span></p></div>` +
      `<div class="rig-row"><button class="primary" id="rig-go" type="button">Rig it</button>` +
      `<button id="rig-restart" type="button">Different file</button></div>`
  );

  document.querySelectorAll(".rig-kind[data-kind]").forEach((btn) =>
    btn.addEventListener("click", () => {
      rig.archetype = btn.dataset.kind;
      document.querySelectorAll(".rig-kind[data-kind]").forEach((b) => b.classList.toggle("on", b === btn));
      const k = (rig.status.archetypes || []).find((x) => x.id === rig.archetype);
      document.getElementById("rig-kind-desc").textContent = k ? k.description : KIND_HINT;
    })
  );
  document.querySelectorAll(".rig-kind[data-target]").forEach((btn) =>
    btn.addEventListener("click", () => {
      rig.target = btn.dataset.target;
      document.querySelectorAll(".rig-kind[data-target]").forEach((b) => b.classList.toggle("on", b === btn));
      const t = RIG_TARGETS.find((x) => x.id === rig.target);
      document.getElementById("rig-target-desc").textContent = t ? t.hint : "";
    })
  );

  const face = document.getElementById("rig-face");
  const drawPins = () => {
    face.querySelectorAll(".rig-pin").forEach((p) => p.remove());
    const pin = (pt, cls) => {
      const el = document.createElement("div");
      el.className = "rig-pin " + cls;
      el.style.left = pt.u * 100 + "%";
      el.style.top = pt.v * 100 + "%";
      face.appendChild(el);
    };
    rig.eyes.forEach((e) => pin(e, ""));
    if (rig.mouth) pin(rig.mouth, "mouth");
    document.getElementById("rig-face-state").textContent =
      rig.mouth ? "eyes + mouth set" : rig.eyes.length ? `${rig.eyes.length}/2 eyes` : "using the guess";
  };
  face.addEventListener("click", (e) => {
    const box = document.getElementById("rig-face-img").getBoundingClientRect();
    const pt = { u: (e.clientX - box.left) / box.width, v: (e.clientY - box.top) / box.height };
    if (rig.eyes.length < 2) rig.eyes.push(pt);
    else rig.mouth = pt;
    drawPins();
  });
  document.getElementById("rig-face-reset").addEventListener("click", () => {
    rig.eyes = [];
    rig.mouth = null;
    drawPins();
  });
  drawPins();

  document.getElementById("rig-flip").addEventListener("click", async () => {
    // Front and back are guessed from the silhouette, and long hair or a
    // backpack fools it. Everything downstream (eyes, mouth, left vs right)
    // hangs off this, so re-read the model the other way round.
    rig.faceFront = !rig.faceFront;
    rig.eyes = [];
    rig.mouth = null;
    try {
      await rigStart("analyze", { source_id: rig.source.id, face_front: rig.faceFront });
    } catch (err) {
      rigFoot(String(err.message || err), "bad");
    }
  });
  document.getElementById("rig-restart").addEventListener("click", () => {
    rig.analysis = null;
    rig.job = null;
    rigRender();
  });
  document.getElementById("rig-go").addEventListener("click", async () => {
    try {
      await rigStart("rig", {
        source_id: rig.source.id,
        archetype: rig.archetype || "auto",
        name: document.getElementById("rig-name").value || "Avatar",
        height_m: parseFloat(document.getElementById("rig-height").value) || 0,
        eyes: rig.eyes.length === 2 ? rig.eyes : null,
        mouth: rig.mouth,
        target: rig.target,
        face_front: rig.faceFront,
      });
    } catch (err) {
      rigFoot(String(err.message || err), "bad");
    }
  });
}

function rigRenderResult() {
  const r = rig.job.result;
  const shot = (key, label) =>
    r.previews[key]
      ? `<div style="flex:1"><img src="${r.previews[key]}?v=${rig.job.id}" alt="${label}">` +
        `<p class="muted" style="text-align:center">${label}</p></div>`
      : "";
  const travel = r.face?.travel || {};
  const faint = Object.entries(travel).filter(([, v]) => v < 0.004);
  const how =
    r.weights.method === "source"
      ? "Kept the skeleton and weights the model came with"
      : r.weights.method === "automatic"
        ? "Heat weights"
        : "Proximity weights";
  const feelings = ["happy", "angry", "sad", "relaxed", "surprised"].filter((k) => r.previews[k]);
  const extras = [
    r.fingers ? `${r.fingers} finger bones` : "",
    r.mtoon ? "MToon materials" : "",
    r.separated_faces ? "arms cut free of the body" : "",
  ].filter(Boolean);
  rigSet(
    `<div><h4>${escapeHtml(r.archetype_label)}</h4>` +
      `<p class="good">Rigged in ${r.seconds}s · ${r.bones} bones · ` +
      `${(r.bytes / 1e6).toFixed(1)} MB · ${r.height_m} m tall</p>` +
      `<p class="muted">${how}, ` +
      `${r.weights.covered.toLocaleString()}/${r.weights.total.toLocaleString()} vertices bound` +
      `${extras.length ? " · " + escapeHtml(extras.join(" · ")) : ""}</p>` +
      (r.fingers_note ? `<p class="muted">${escapeHtml(r.fingers_note)}</p>` : "") +
      `</div>` +
      `<div class="rig-shots">${shot("front", "rig")}${shot("side", "side")}${shot("tpose", "T-pose")}</div>` +
      (r.previews.tpose
        ? `<p class="muted">The T-pose is how the file actually rests, and the first time the arms are asked to move — look at the armpits and hands there.</p>`
        : "") +
      `<div class="rig-shots">${shot("face", "face")}${shot("blink", "blink")}${shot("mouth", "mouth")}</div>` +
      (feelings.length
        ? `<div class="rig-shots">${feelings.slice(0, 3).map((k) => shot(k, k)).join("")}</div>` +
          (feelings.length > 3
            ? `<div class="rig-shots">${feelings.slice(3).map((k) => shot(k, k)).join("")}<div style="flex:1"></div></div>`
            : "")
        : "") +
      (faint.length
        ? `<p class="warn">The ${faint
            .map(([k]) => k.replaceAll("_", " "))
            .join(" and ")} shape${faint.length > 1 ? "s" : ""} barely move${faint.length > 1 ? "" : "s"}. Re-run and click the eyes and mouth on the face shot.</p>`
        : `<p class="muted">Check the blink and mouth shots — that's the tracking driving the face.${
            feelings.length ? " The five emotions are what a Code27 or an AI persona asks for by name." : ""
          }</p>`) +
      rigCheckHtml(r.check) +
      `<div class="rig-row"><button class="primary" id="rig-load" type="button">Load it</button>` +
      `<button id="rig-save" type="button">Save to gallery</button></div>` +
      `<div class="rig-row"><button id="rig-again" type="button">Rig again, differently</button>` +
      `<button id="rig-new" type="button">Another file</button></div>`
  );
  document.getElementById("rig-load").addEventListener("click", async () => {
    currentVrmUrl = r.vrm;
    uploadedFile = null;
    // Await the load before reframing: setBody frames around the avatar's
    // bones, and firing it early frames the one being replaced.
    await loadVRM(r.vrm);
    await setBody(r.tracking === "full").catch(() => {});
    els.rig.classList.add("hidden");
  });
  document.getElementById("rig-save").addEventListener("click", async () => {
    // Deliberately does NOT touch currentVrmUrl: the viewer is still showing
    // whatever was loaded before, and Copy OBS URL must keep describing that,
    // not a model that only exists in the gallery. Load it first if you want
    // both.
    const res = await fetch("/api/avatar/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: rig.job.label || r.archetype_label,
        vrm: r.vrm,
        settings: { ...currentSettings(), body: r.tracking === "full" },
      }),
    });
    rigFoot(res.ok ? "saved to your gallery ✓" : "couldn't save it", res.ok ? "good" : "bad");
  });
  document.getElementById("rig-again").addEventListener("click", () => {
    rig.job = null;
    rigRenderSetup();
  });
  document.getElementById("rig-new").addEventListener("click", () => {
    rig.analysis = null;
    rig.job = null;
    rig.source = null;
    rigRender();
  });
}

els.btnRig.addEventListener("click", async () => {
  const opening = els.rig.classList.contains("hidden");
  els.rig.classList.toggle("hidden");
  els.gallery.classList.add("hidden");
  if (!opening) return;
  rigSet("<p class='muted'>looking for Blender…</p>");
  await rigRefresh();
  rigRender();
});
els.rigClose.addEventListener("click", () => els.rig.classList.add("hidden"));

// Progress from the server, so the panel is live even if this tab was opened
// halfway through a rig running for someone else on the rig.
if (window.Bus) {
  Bus.on("rig", (payload) => {
    if (!rig.job) return;
    const mine = (payload.jobs || []).find((j) => j.id === rig.job.id);
    if (mine) rigOnJob(mine);
  });
}

// ── Moves panel: record · review & trim · gallery · export ───────────
function fmtSeconds(s) {
  return `${(+s || 0).toFixed(1)}s`;
}

function onPlayerChange() {
  els.btnRec.disabled = !!player.clip && rec.state === "idle";
  if (!els.moves.classList.contains("hidden") && rec.state !== "review") renderMoves();
}

function setRecUi() {
  const label = { idle: "● Record", countdown: "Get ready…", recording: "■ Stop", review: "● Record" }[rec.state];
  els.btnRec.textContent = label;
  els.btnRec.classList.toggle("rec", rec.state === "recording" || rec.state === "countdown");
}

async function startRecording() {
  if (rec.state !== "idle" && rec.state !== "review") return;
  stopClip();
  if (!settings.tracking) await startTracking();
  if (!settings.tracking) return; // camera refused — startTracking already said why
  rec.take = null;
  rec.state = "countdown";
  setRecUi();
  let n = 3;
  const tick = () => {
    if (rec.state !== "countdown") return;
    if (n > 0) {
      setStatus(`recording in ${n}…`);
      n -= 1;
      rec.timer = setTimeout(tick, 1000);
      return;
    }
    rec.frames = [];
    rec.t0 = performance.now();
    rec.last = 0;
    rec.state = "recording";
    setRecUi();
    setStatus(`● recording — press Stop when you're done (up to ${REC_MAX_SECONDS}s)`);
    // Say what is being captured while it happens — otherwise a take with
    // your face out of shot only turns out empty after you press Stop.
    clearInterval(rec.meter);
    rec.meter = setInterval(() => {
      if (rec.state !== "recording") return clearInterval(rec.meter);
      const got = rec.frames.length ? rec.frames[rec.frames.length - 1].t : 0;
      if (performance.now() - lastFaceAt > 1500) {
        setStatus("● recording — but I can't see your face. Face the camera this avatar is tracking.", true);
      } else {
        setStatus(`● recording ${got.toFixed(1)}s — press Stop when you're done`);
      }
    }, 500);
  };
  tick();
}

function stopRecording() {
  clearTimeout(rec.timer);
  clearInterval(rec.meter);
  if (rec.state === "countdown") {
    rec.state = "idle";
    setRecUi();
    setStatus("recording cancelled", false, true);
    return;
  }
  if (rec.state !== "recording") return;
  const frames = rec.frames;
  rec.frames = [];
  if (frames.length < 5) {
    rec.state = "idle";
    setRecUi();
    setStatus("nothing was captured — the tracker has to see your face while you record", true);
    return;
  }
  const duration = frames[frames.length - 1].t;
  rec.take = { name: "", frames, duration, from: 0, to: duration };
  rec.state = "review";
  setRecUi();
  setStatus(`captured ${fmtSeconds(duration)} — name it and save`, false, true);
  els.gallery.classList.add("hidden");
  els.rig?.classList.add("hidden");
  els.moves.classList.remove("hidden");
  renderReview();
  playClip(rec.take, { loop: true });
}

els.btnRec.addEventListener("click", () => {
  if (rec.state === "recording" || rec.state === "countdown") stopRecording();
  else startRecording().catch((e) => setStatus("could not record: " + (e?.message || e), true));
});

function discardTake() {
  stopClip();
  rec.take = null;
  rec.state = "idle";
  setRecUi();
  renderMoves();
}

// Review the take you just made: it loops on the avatar while you trim the
// reach-for-the-button moments off each end and give it a name.
function renderReview() {
  const take = rec.take;
  const box = els.movesList;
  box.replaceChildren();
  const card = document.createElement("div");
  card.className = "g-item m-review";

  const title = document.createElement("div");
  title.className = "g-name";
  title.textContent = "New move";
  const name = document.createElement("input");
  name.type = "text";
  name.maxLength = 60;
  name.placeholder = "Name it — wave, dance, shrug…";
  name.className = "m-input";
  name.value = take.name;
  name.addEventListener("input", () => (take.name = name.value));

  const length = document.createElement("div");
  length.className = "g-tags";
  const showLength = () =>
    (length.textContent = `keeping ${fmtSeconds(take.to - take.from)} of ${fmtSeconds(take.duration)} · it loops on the avatar while you trim`);

  const slider = (labelText, key) => {
    const row = document.createElement("label");
    row.className = "m-trim";
    const span = document.createElement("span");
    span.textContent = labelText;
    const input = document.createElement("input");
    input.type = "range";
    input.min = "0";
    input.max = String(take.duration);
    input.step = "0.05";
    input.value = String(take[key]);
    input.addEventListener("input", () => {
      let v = parseFloat(input.value);
      // keep at least a quarter second between the two handles
      if (key === "from") v = Math.min(v, take.to - 0.25);
      else v = Math.max(v, take.from + 0.25);
      take[key] = Math.max(0, Math.min(take.duration, v));
      input.value = String(take[key]);
      showLength();
      playClip(take, { loop: true, from: take.from, to: take.to });
    });
    row.append(span, input);
    return row;
  };

  const actions = document.createElement("div");
  actions.className = "g-actions";
  const save = document.createElement("button");
  save.textContent = "Save move";
  save.className = "m-primary";
  const again = document.createElement("button");
  again.textContent = "Record again";
  const discard = document.createElement("button");
  discard.textContent = "Discard";
  discard.className = "g-del";
  const note = document.createElement("div");
  note.className = "g-tags m-note";

  save.addEventListener("click", async () => {
    save.disabled = true;
    note.textContent = "saving…";
    try {
      const frames = take.frames
        .filter((f) => f.t >= take.from && f.t <= take.to)
        .map((f) => ({ ...f, t: f.t - take.from }));
      const res = await fetch("/api/avatar/animations", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ name: take.name.trim(), frames }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.status === 401) throw new Error("the admin password is needed — enter it under Setup on the dashboard");
      if (!res.ok) throw new Error(body.detail || "could not save");
      setStatus(`saved “${body.animation.name}” ✓`, false, true);
      discardTake();
    } catch (err) {
      note.textContent = String(err?.message || err);
      save.disabled = false;
    }
  });
  again.addEventListener("click", () => {
    discardTake();
    startRecording();
  });
  discard.addEventListener("click", discardTake);

  actions.append(save, again, discard);
  card.append(title, name, slider("Start", "from"), slider("End", "to"), length, actions, note);
  box.appendChild(card);
  showLength();
  name.focus();
}

async function renderMoves() {
  if (rec.state === "review") return renderReview();
  const box = els.movesList;
  let clips = [];
  try {
    clips = (await (await fetch("/api/avatar/animations")).json()).animations || [];
  } catch {
    box.innerHTML = "<div class='gallery-empty'>couldn't load your moves</div>";
    return;
  }
  box.replaceChildren();
  if (!clips.length) {
    const empty = document.createElement("div");
    empty.className = "gallery-empty";
    empty.innerHTML = "No moves yet.<br>Press <b>● Record</b>, act it out, press <b>■ Stop</b>.";
    box.appendChild(empty);
    return;
  }
  clips
    .slice()
    .reverse()
    .forEach((c) => {
      const playing = player.clip?.id === c.id;
      const item = document.createElement("div");
      item.className = "g-item" + (playing ? " m-playing" : "");
      const name = document.createElement("div");
      name.className = "g-name";
      name.textContent = c.name;
      const tags = document.createElement("div");
      tags.className = "g-tags";
      tags.textContent = `${fmtSeconds(c.duration)} · ${c.body ? "face + body" : "face"}`;
      const actions = document.createElement("div");
      actions.className = "g-actions";
      const button = (text, handler, cls = "") => {
        const b = document.createElement("button");
        b.textContent = text;
        if (cls) b.className = cls;
        b.addEventListener("click", () => handler(b));
        actions.appendChild(b);
        return b;
      };
      const trigger = async (loop) => {
        // Through the server, so OBS avatar sources perform it too.
        const res = await fetch(`/api/avatar/animations/${c.id}/play`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ loop }),
        }).catch(() => null);
        if (!res || !res.ok) playClip(await fetchClip(c.id), { loop }); // server away: play here only
      };
      if (playing) {
        button("■ Stop", () => fetch("/api/avatar/animations/stop", { method: "POST" }).catch(() => stopClip()), "m-primary");
      } else {
        button("▶ Play", () => trigger(false), "m-primary");
        button("↻ Loop", () => trigger(true));
      }
      button("Rename", () => {
        const input = document.createElement("input");
        input.type = "text";
        input.maxLength = 60;
        input.className = "m-input";
        input.value = c.name;
        name.replaceChildren(input);
        input.focus();
        input.select();
        const commit = async () => {
          const value = input.value.trim();
          if (value && value !== c.name) {
            await fetch(`/api/avatar/animations/${c.id}`, {
              method: "PATCH",
              headers: authHeaders({ "Content-Type": "application/json" }),
              body: JSON.stringify({ name: value }),
            });
          }
          renderMoves();
        };
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") renderMoves();
        });
        input.addEventListener("blur", commit);
      });
      button("Export .vrma", async (b) => {
        b.disabled = true;
        try {
          exportVrma(await fetchClip(c.id));
        } catch (err) {
          setStatus("export failed: " + (err?.message || err), true);
        } finally {
          b.disabled = false;
        }
      });
      button(
        "Delete",
        async (b) => {
          // two clicks instead of a blocking confirm() dialog
          if (b.dataset.armed !== "1") {
            b.dataset.armed = "1";
            b.textContent = "Really delete?";
            setTimeout(() => {
              b.dataset.armed = "";
              b.textContent = "Delete";
            }, 3000);
            return;
          }
          await fetch(`/api/avatar/animations/${c.id}`, { method: "DELETE", headers: authHeaders() });
          clipCache.delete(c.id);
          renderMoves();
        },
        "g-del"
      );
      item.append(name, tags, actions);
      box.appendChild(item);
    });
}

els.btnMoves.addEventListener("click", () => {
  els.gallery.classList.add("hidden");
  els.rig?.classList.add("hidden");
  els.moves.classList.toggle("hidden");
  if (!els.moves.classList.contains("hidden")) renderMoves();
});
els.movesClose.addEventListener("click", () => els.moves.classList.add("hidden"));

// ── export: bake a move onto this avatar as a standard .vrma file ─────
// Runs the whole performance on the loaded avatar off-screen (same code path
// as playback, stepped at 60 Hz so the smoothing matches what you saw) and
// samples the resulting bone rotations 30 times a second.
const EXPORT_EXPRESSIONS = ["blink", "aa", "ih", "ou", "ee", "oh", "surprised", "lookLeft", "lookRight", "lookUp", "lookDown"];
const EXPORT_ROTATIONS = ["neck", "head", ...BODY_BONES];

function bakeClip(clip) {
  const vrm = currentVrm;
  const humanoid = vrm?.humanoid;
  if (!humanoid?.getNormalizedBoneNode("hips")) throw new Error("load an avatar first");
  const em = vrm.expressionManager;
  const node = (n) => humanoid.getNormalizedBoneNode(n);
  const names = HUMANOID_ORDER.filter((n) => node(n));
  const exprNames = EXPORT_EXPRESSIONS.filter((n) => em?.getExpression(n) != null);
  const keepQuat = names.map((n) => node(n).quaternion.clone());
  const keepExpr = exprNames.map((n) => em.getValue(n) ?? 0);
  // VRM 0.x avatars face the other way and are spun round at load; .vrma is
  // always written VRM 1.0-style, so mirror X and Z back for those.
  const flip = vrm.meta?.metaVersion === "0";

  try {
    // T-pose → where each bone sits relative to its parent
    names.forEach((n) => node(n).quaternion.identity());
    vrm.scene.updateMatrixWorld(true);
    const world = new Map(names.map((n) => [n, node(n).getWorldPosition(new THREE.Vector3())]));
    const bones = names.map((n) => {
      const parent = nearestParent(n, (x) => world.has(x));
      const p = world.get(n).clone();
      if (parent) p.sub(world.get(parent));
      if (flip) p.set(-p.x, p.y, -p.z);
      return { name: n, parent, translation: [p.x, p.y, p.z] };
    });

    applyRestPose(vrm);
    exprNames.forEach((n) => em.setValue(n, 0));
    const duration = clip.frames[clip.frames.length - 1].t;
    const count = Math.max(2, Math.round(duration * 30) + 1);
    const tracked = EXPORT_ROTATIONS.filter((n) => node(n));
    const times = new Float32Array(count);
    const rotations = Object.fromEntries(tracked.map((n) => [n, new Float32Array(count * 4)]));
    const expressions = Object.fromEntries(exprNames.map((n) => [n, new Float32Array(count)]));
    let cursor = 0;
    for (let step = 0, out = 0; out < count; step++) {
      const t = step / 60;
      while (cursor + 1 < clip.frames.length && clip.frames[cursor + 1].t <= t) cursor++;
      const frame = clip.frames[cursor];
      rigFace(frame.f);
      if (frame.p) rigPose(frame.p);
      if (step % 2) continue;
      times[out] = out / 30;
      for (const n of tracked) {
        const q = node(n).quaternion;
        rotations[n].set(flip ? [-q.x, q.y, -q.z, q.w] : [q.x, q.y, q.z, q.w], out * 4);
      }
      for (const n of exprNames) expressions[n][out] = em.getValue(n) ?? 0;
      out++;
    }
    return { bones, times, rotations, expressions, name: clip.name || "move" };
  } finally {
    names.forEach((n, i) => node(n).quaternion.copy(keepQuat[i]));
    exprNames.forEach((n, i) => em.setValue(n, keepExpr[i]));
  }
}

function exportVrma(clip) {
  const bytes = buildVrma(bakeClip(clip));
  const safe = (clip.name || "move").replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-") || "move";
  const url = URL.createObjectURL(new Blob([bytes], { type: "model/gltf-binary" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safe}.vrma`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  setStatus(`exported ${safe}.vrma ✓`, false, true);
}

// For tests and tinkering from the console.
window.__moves = { rec, player, playClip, stopClip, bakeClip, buildVrma, fetchClip, vrm: () => currentVrm };

// ── boot ──────────────────────────────────────────────────────────────
window.__avatarBoot = true; // tells the HTML watchdog the module started OK
els.btnMirror.classList.toggle("on", settings.mirror);
els.btnBody.classList.toggle("on", settings.body);
animate();
loadVRM(currentVrmUrl);
populateSources();
if (settings.body) initPose().catch((e) => setStatus("body model: " + (e?.message || e), true));
if (autostart) {
  startTracking();
} else if (follow) {
  setStatus("mirroring the rig's tracker — keep the tracking page open", false, true);
} else {
  setStatus("ready — press Start tracking (grant camera access)", false);
}
