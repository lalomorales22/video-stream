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
};

// ── URL config ────────────────────────────────────────────────────────
const params = new URLSearchParams(location.search);
const initialSrc = params.get("src"); // e.g. "/stream/2" or "http://host:8765/stream/0"
const initialVrm = params.get("vrm");
const urlZoom = parseFloat(params.get("zoom"));
const urlPan = parseFloat(params.get("pan"));
const urlOx = parseFloat(params.get("ox"));
const urlOy = parseFloat(params.get("oy"));
const autostart = params.has("autostart") || !!initialSrc;

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

// Bring the arms down from the default T-pose into a natural rest position.
function applyRestPose(vrm) {
  const h = vrm.humanoid;
  const set = (name, x, y, z) => {
    const b = h?.getNormalizedBoneNode(name);
    if (b) b.rotation.set(x, y, z);
  };
  set("leftUpperArm", 0, 0, -1.3);
  set("rightUpperArm", 0, 0, 1.3);
  set("leftLowerArm", 0, 0, -0.1);
  set("rightLowerArm", 0, 0, 0.1);
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
  rigRotation("neck", rf.head, 0.5, 0.35);
  rigRotation("head", rf.head, 0.5, 0.35);

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
  // Negate the arm swing axis (z): Kalidokit's arm output comes in vertically
  // inverted against three-vrm's normalized arm bones, so arms-down read as up.
  const armFix = (r) => (r ? { x: r.x, y: r.y, z: -r.z } : r);
  rigRotation("rightUpperArm", armFix(rp.RightUpperArm), 1, 0.3);
  rigRotation("rightLowerArm", armFix(rp.RightLowerArm), 1, 0.3);
  rigRotation("leftUpperArm", armFix(rp.LeftUpperArm), 1, 0.3);
  rigRotation("leftLowerArm", armFix(rp.LeftLowerArm), 1, 0.3);
  rigRotation("rightUpperLeg", rp.RightUpperLeg, 1, 0.3);
  rigRotation("rightLowerLeg", rp.RightLowerLeg, 1, 0.3);
  rigRotation("leftUpperLeg", rp.LeftUpperLeg, 1, 0.3);
  rigRotation("leftLowerLeg", rp.LeftLowerLeg, 1, 0.3);
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
        const res = faceLandmarker.detectForVideo(frame.image, now);
        const lm = res.faceLandmarks?.[0];
        if (lm) {
          const rf = Kalidokit.Face.solve(lm, {
            runtime: "mediapipe",
            imageSize: { width: frame.w, height: frame.h },
            smoothBlink: true,
          });
          if (rf) {
            rigFace(rf);
            lastFaceAt = now;
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
            if (rp) rigPose(rp);
          }
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
  const p = new URLSearchParams();
  p.set("obs", "1");
  if (source.kind === "stream") p.set("src", source.url);
  else p.set("autostart", "1");
  if (currentVrmUrl && currentVrmUrl !== DEFAULT_VRM) p.set("vrm", currentVrmUrl);
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
} else {
  setStatus("ready — press Start tracking (grant camera access)", false);
}
