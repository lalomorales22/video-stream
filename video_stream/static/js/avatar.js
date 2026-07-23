// Path B — browser VTuber avatar.
//
// webcam → MediaPipe FaceLandmarker → Kalidokit → three-vrm avatar → transparent
// canvas → OBS Browser Source. Everything runs client-side; the server only serves
// the files. See path_b.md for the full design.

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
  status: document.getElementById("status"),
  fps: document.getElementById("fps"),
  bar: document.getElementById("bar"),
  btnTrack: document.getElementById("btn-track"),
  camSelect: document.getElementById("cam-select"),
  vrmFile: document.getElementById("vrm-file"),
  btnMirror: document.getElementById("btn-mirror"),
  btnBg: document.getElementById("btn-bg"),
};

const settings = { mirror: true, tracking: false };
let currentVrm = null;
let faceLandmarker = null;
let stream = null;

function setStatus(msg, isError = false, autohide = false) {
  els.status.textContent = msg;
  els.status.classList.toggle("err", isError);
  els.status.classList.remove("hide");
  if (autohide) setTimeout(() => els.status.classList.add("hide"), 1800);
}

// ── three.js scene ────────────────────────────────────────────────────
const renderer = new THREE.WebGLRenderer({
  canvas: els.canvas,
  alpha: true,
  antialias: true,
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x000000, 0); // transparent for OBS

const scene = new THREE.Scene();

// Framed on the head / upper body for the close-up "talking avatar" case.
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
    // Normalize VRM0 avatars to face the camera like VRM1.
    VRMUtils.rotateVRM0(vrm);
    // Trim work the renderer doesn't need for a single avatar.
    VRMUtils.removeUnnecessaryVertices(vrm.scene);
    VRMUtils.combineSkeletons(vrm.scene);
    vrm.scene.traverse((o) => (o.frustumCulled = false));
    scene.add(vrm.scene);
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

// ── webcam ────────────────────────────────────────────────────────────
async function listCameras() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const cams = devices.filter((d) => d.kind === "videoinput");
    els.camSelect.innerHTML = "";
    cams.forEach((c, i) => {
      const opt = document.createElement("option");
      opt.value = c.deviceId;
      opt.textContent = c.label || `Camera ${i + 1}`;
      els.camSelect.appendChild(opt);
    });
  } catch (err) {
    console.error(err);
  }
}

async function startWebcam(deviceId) {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = await navigator.mediaDevices.getUserMedia({
    video: deviceId
      ? { deviceId: { exact: deviceId } }
      : { width: 640, height: 480, facingMode: "user" },
    audio: false,
  });
  els.video.srcObject = stream;
  await els.video.play();
  await listCameras(); // labels populate once permission is granted
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
  // Head/neck pose. Split across neck + head for a natural bend.
  rigRotation("neck", rf.head, 0.5, 0.35);
  rigRotation("head", rf.head, 0.5, 0.35);

  // Blink (Kalidokit eye: 1 = open; VRM 'blink' expression: 1 = closed).
  const eye = Kalidokit.Face.stabilizeBlink(rf.eye, rf.head.y);
  expr("blink", 1 - eye.l, 0.55);

  // Mouth visemes from the solved mouth shape.
  expr("aa", rf.mouth.shape.A);
  expr("ih", rf.mouth.shape.I);
  expr("ou", rf.mouth.shape.U);
  expr("ee", rf.mouth.shape.E);
  expr("oh", rf.mouth.shape.O);

  // Brows → a touch of "surprised" so raised brows read (VRM has no brow preset).
  if (typeof rf.brow === "number") expr("surprised", clamp(rf.brow * 1.4, 0, 1), 0.3);

  // Eye gaze via lookAt expressions.
  const px = (settings.mirror ? -1 : 1) * (rf.pupil?.x ?? 0);
  const py = rf.pupil?.y ?? 0;
  expr("lookLeft", clamp(px, 0, 1), 0.5);
  expr("lookRight", clamp(-px, 0, 1), 0.5);
  expr("lookUp", clamp(-py, 0, 1), 0.5);
  expr("lookDown", clamp(py, 0, 1), 0.5);
}

// ── main loop ─────────────────────────────────────────────────────────
const clock = new THREE.Clock();
let lastVideoTime = -1;
let frames = 0;
let fpsAt = performance.now();

function animate() {
  requestAnimationFrame(animate);
  const dt = clock.getDelta();

  if (settings.tracking && faceLandmarker && els.video.readyState >= 2) {
    const t = els.video.currentTime;
    if (t !== lastVideoTime) {
      lastVideoTime = t;
      const res = faceLandmarker.detectForVideo(els.video, performance.now());
      const lm = res.faceLandmarks?.[0];
      if (lm) {
        const rf = Kalidokit.Face.solve(lm, {
          runtime: "mediapipe",
          video: els.video,
          smoothBlink: true,
        });
        if (rf) rigFace(rf);
      }
      // FPS readout
      frames++;
      const now = performance.now();
      if (now - fpsAt > 1000) {
        els.fps.textContent = `${frames} fps`;
        frames = 0;
        fpsAt = now;
      }
    }
  }

  if (currentVrm) currentVrm.update(dt);
  renderer.render(scene, camera);
}

// ── controls ──────────────────────────────────────────────────────────
async function toggleTracking() {
  if (settings.tracking) {
    settings.tracking = false;
    els.btnTrack.textContent = "Start tracking";
    els.btnTrack.classList.remove("rec");
    if (stream) stream.getTracks().forEach((t) => t.stop());
    setStatus("tracking paused", false, true);
    return;
  }
  try {
    setStatus("starting camera…");
    if (!faceLandmarker) await initFaceLandmarker();
    await startWebcam(els.camSelect.value || null);
    settings.tracking = true;
    els.btnTrack.textContent = "Stop tracking";
    els.btnTrack.classList.add("rec");
    setStatus("tracking", false, true);
  } catch (err) {
    console.error(err);
    setStatus("camera/tracking failed: " + (err?.message || err), true);
  }
}

els.btnTrack.addEventListener("click", toggleTracking);

els.camSelect.addEventListener("change", () => {
  if (settings.tracking) startWebcam(els.camSelect.value).catch((e) => setStatus(String(e), true));
});

els.vrmFile.addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  if (file) loadVRM(URL.createObjectURL(file));
});

els.btnMirror.addEventListener("click", () => {
  settings.mirror = !settings.mirror;
  els.btnMirror.classList.toggle("on", settings.mirror);
});

els.btnBg.addEventListener("click", () => {
  document.body.classList.toggle("preview-bg");
  els.btnBg.classList.toggle("on");
});

// ── boot ──────────────────────────────────────────────────────────────
animate();
loadVRM(DEFAULT_VRM);
listCameras();
setStatus("ready — press Start tracking (grant camera access)", false);
