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
import { FaceLandmarker, FilesetResolver } from "/static/vendor/mediapipe/vision_bundle.mjs";

const DEFAULT_VRM = "/static/models/avatar.vrm";
const FACE_MODEL = "/static/models/face_landmarker.task";
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
  btnMirror: document.getElementById("btn-mirror"),
  btnBg: document.getElementById("btn-bg"),
  btnCopy: document.getElementById("btn-copy"),
};

// ── URL config ────────────────────────────────────────────────────────
const params = new URLSearchParams(location.search);
const initialSrc = params.get("src"); // e.g. "/stream/2" or "http://host:8765/stream/0"
const initialVrm = params.get("vrm");
const urlZoom = parseFloat(params.get("zoom"));
const urlPan = parseFloat(params.get("pan"));
const autostart = params.has("autostart") || !!initialSrc;

const settings = { mirror: params.get("mirror") !== "0", tracking: false };
// Current tracking source: a local webcam, or a camera stream URL.
let source = initialSrc
  ? { kind: "stream", url: initialSrc }
  : { kind: "webcam", deviceId: null };

let currentVrm = null;
let currentVrmUrl = initialVrm || DEFAULT_VRM;
let faceLandmarker = null;
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
const view = { cx: 0, cz: 0, targetY: 1.1, distance: 1.5 };

function updateCamera() {
  lookTarget.set(view.cx, view.targetY, view.cz);
  camera.position.set(view.cx, view.targetY + 0.12, view.cz + view.distance);
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
  view.targetY = isNaN(urlPan) ? p.y - 0.35 : urlPan;
  view.distance = isNaN(urlZoom) ? 1.5 : urlZoom;
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
    frameOnHead(vrm);
    setStatus(settings.tracking ? "tracking" : "avatar loaded — press Start tracking", false, settings.tracking);
  } catch (err) {
    console.error(err);
    setStatus("could not load avatar — is it a valid .vrm? " + (err?.message || ""), true);
  }
}

// ── MediaPipe face landmarker ─────────────────────────────────────────
async function initFaceLandmarker() {
  const fileset = await FilesetResolver.forVisionTasks(WASM_PATH);
  faceLandmarker = await FaceLandmarker.createFromOptions(fileset, {
    baseOptions: { modelAssetPath: FACE_MODEL, delegate: "GPU" },
    runningMode: "VIDEO",
    numFaces: 1,
    outputFaceBlendshapes: true,
    outputFacialTransformationMatrixes: true,
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
function currentFrame() {
  if (source.kind === "webcam") {
    if (els.video.readyState < 2 || !els.video.videoWidth) return null;
    return { image: els.video, w: els.video.videoWidth, h: els.video.videoHeight };
  }
  const img = els.srcImg;
  if (!img.naturalWidth) return null;
  if (work.width !== img.naturalWidth) {
    work.width = img.naturalWidth;
    work.height = img.naturalHeight;
  }
  workCtx.drawImage(img, 0, 0);
  return { image: work, w: work.width, h: work.height };
}

// ── retargeting: Kalidokit rig → VRM ──────────────────────────────────
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function rigRotation(name, rot, dampen = 1, lerp = 0.3) {
  const bone = currentVrm?.humanoid?.getNormalizedBoneNode(name);
  if (!bone) return;
  const m = settings.mirror ? -1 : 1;
  const euler = new THREE.Euler(rot.x * dampen, rot.y * dampen * m, rot.z * dampen * m, "XYZ");
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

  const px = (settings.mirror ? -1 : 1) * (rf.pupil?.x ?? 0);
  const py = rf.pupil?.y ?? 0;
  expr("lookLeft", clamp(px, 0, 1), 0.5);
  expr("lookRight", clamp(-px, 0, 1), 0.5);
  expr("lookUp", clamp(-py, 0, 1), 0.5);
  expr("lookDown", clamp(py, 0, 1), 0.5);
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
          if (rf) rigFace(rf);
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

  if (currentVrm) currentVrm.update(dt);
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
    currentVrmUrl = null; // a local blob can't be shared to another machine's OBS
    loadVRM(URL.createObjectURL(file));
  }
});

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
  p.set("mirror", settings.mirror ? "1" : "0");
  p.set("zoom", view.distance.toFixed(2));
  p.set("pan", view.targetY.toFixed(3));
  const url = `${location.protocol}//${host}/avatar?${p.toString()}`;
  try {
    await navigator.clipboard.writeText(url);
    setStatus("OBS URL copied ✓  (add as a Browser Source)", false, true);
  } catch {
    setStatus(url, false); // clipboard blocked — show it to copy manually
  }
}
els.btnCopy.addEventListener("click", copyObsUrl);

// Scroll to zoom; drag up/down to pan the framing.
els.canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    view.distance = clamp(view.distance * (e.deltaY > 0 ? 1.1 : 0.9), 0.3, 5);
    updateCamera();
  },
  { passive: false }
);
let dragging = false;
let dragY = 0;
els.canvas.addEventListener("pointerdown", (e) => {
  dragging = true;
  dragY = e.clientY;
});
window.addEventListener("pointerup", () => (dragging = false));
window.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  view.targetY += (e.clientY - dragY) * 0.004;
  dragY = e.clientY;
  updateCamera();
});

// ── boot ──────────────────────────────────────────────────────────────
window.__avatarBoot = true; // tells the HTML watchdog the module started OK
els.btnMirror.classList.toggle("on", settings.mirror);
animate();
loadVRM(currentVrmUrl);
populateSources();
if (autostart) {
  startTracking();
} else {
  setStatus("ready — press Start tracking (grant camera access)", false);
}
