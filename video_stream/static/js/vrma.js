// Writes a VRM Animation (.vrma) file — the standard, portable animation
// format for VRM avatars (glTF 2.0 + the VRMC_vrm_animation extension).
//
// Pure data in, bytes out: no three.js, no DOM, so it can be unit-tested
// from Node. The caller hands over a baked performance:
//
//   bones:       [{ name, parent, translation: [x, y, z] }]
//                humanoid bones, parents listed before children, translations
//                relative to the parent, in the avatar's T-pose (metres,
//                VRM 1.0 orientation: +Y up, the avatar faces +Z)
//   times:       Float32Array of N keyframe times in seconds, ascending
//   rotations:   { boneName: Float32Array(N * 4) }  local quaternions (x,y,z,w)
//                relative to the T-pose, i.e. three-vrm "normalized" rotations
//   expressions: { presetName: Float32Array(N) }    weights 0..1
//
// The skeleton it writes has no rest rotations at all, which is what lets
// any VRMA player retarget the clip onto a different avatar by bone name.

const PRESET_EXPRESSIONS = new Set([
  "happy", "angry", "sad", "relaxed", "surprised",
  "aa", "ih", "ou", "ee", "oh",
  "blink", "blinkLeft", "blinkRight",
  "lookUp", "lookDown", "lookLeft", "lookRight", "neutral",
]);

function pad4(n) {
  return (4 - (n % 4)) % 4;
}

export function buildVrma({ bones, times, rotations = {}, expressions = {}, name = "move" }) {
  const count = times.length;
  if (!count) throw new Error("nothing to export — the move has no frames");
  if (!bones.length || bones[0].parent) throw new Error("the first bone must be the root (hips)");

  const chunks = []; // Float32Array pieces of the binary buffer
  const bufferViews = [];
  const accessors = [];
  let offset = 0;

  function addAccessor(data, type, minMax) {
    const bytes = data.byteLength;
    bufferViews.push({ buffer: 0, byteOffset: offset, byteLength: bytes });
    const width = { SCALAR: 1, VEC3: 3, VEC4: 4 }[type];
    const acc = { bufferView: bufferViews.length - 1, componentType: 5126, count: data.length / width, type };
    if (minMax) Object.assign(acc, minMax);
    accessors.push(acc);
    chunks.push(data);
    offset += bytes; // Float32 data is always a multiple of 4 — no padding needed
    return accessors.length - 1;
  }

  // ── nodes: the humanoid skeleton, then one node per animated expression ──
  const nodes = [];
  const indexOf = new Map();
  const humanBones = {};
  bones.forEach((b, i) => {
    if (b.parent && !indexOf.has(b.parent)) {
      throw new Error(`bone "${b.name}" is listed before its parent "${b.parent}"`);
    }
    indexOf.set(b.name, i);
    nodes.push({ name: b.name, translation: Array.from(b.translation, (v) => +v.toFixed(6)) });
    humanBones[b.name] = { node: i };
    if (b.parent) {
      const parent = nodes[indexOf.get(b.parent)];
      (parent.children ||= []).push(i);
    }
  });

  const time = Float32Array.from(times);
  const timeAcc = addAccessor(time, "SCALAR", { min: [time[0]], max: [time[count - 1]] });

  const samplers = [];
  const channels = [];
  function addChannel(node, path, data, type) {
    samplers.push({ input: timeAcc, output: addAccessor(data, type), interpolation: "LINEAR" });
    channels.push({ sampler: samplers.length - 1, target: { node, path } });
  }

  for (const [bone, quats] of Object.entries(rotations)) {
    if (!indexOf.has(bone)) continue; // the avatar has no such bone
    if (quats.length !== count * 4) throw new Error(`rotation track "${bone}" has the wrong length`);
    addChannel(indexOf.get(bone), "rotation", Float32Array.from(quats), "VEC4");
  }

  // An expression is carried by a node whose translation.x is the weight.
  const preset = {};
  const custom = {};
  for (const [expression, weights] of Object.entries(expressions)) {
    if (weights.length !== count) throw new Error(`expression track "${expression}" has the wrong length`);
    const node = nodes.length;
    nodes.push({ name: `expression:${expression}`, translation: [0, 0, 0] });
    const xyz = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) xyz[i * 3] = Math.min(1, Math.max(0, weights[i]));
    addChannel(node, "translation", xyz, "VEC3");
    (PRESET_EXPRESSIONS.has(expression) ? preset : custom)[expression] = { node };
  }

  const sceneNodes = [0];
  for (let i = bones.length; i < nodes.length; i++) sceneNodes.push(i);

  const extension = { specVersion: "1.0", humanoid: { humanBones } };
  if (Object.keys(preset).length || Object.keys(custom).length) {
    extension.expressions = {};
    if (Object.keys(preset).length) extension.expressions.preset = preset;
    if (Object.keys(custom).length) extension.expressions.custom = custom;
  }

  const gltf = {
    asset: { version: "2.0", generator: "video-stream move recorder" },
    extensionsUsed: ["VRMC_vrm_animation"],
    extensions: { VRMC_vrm_animation: extension },
    scene: 0,
    scenes: [{ nodes: sceneNodes }],
    nodes,
    animations: [{ name, channels, samplers }],
    buffers: [{ byteLength: offset }],
    bufferViews,
    accessors,
  };

  // ── pack as binary glTF (.glb container) ──
  const jsonBytes = new TextEncoder().encode(JSON.stringify(gltf));
  const jsonPad = pad4(jsonBytes.length);
  const binPad = pad4(offset);
  const total = 12 + 8 + jsonBytes.length + jsonPad + 8 + offset + binPad;
  const out = new ArrayBuffer(total);
  const view = new DataView(out);
  const bytes = new Uint8Array(out);
  view.setUint32(0, 0x46546c67, true); // "glTF"
  view.setUint32(4, 2, true);
  view.setUint32(8, total, true);
  view.setUint32(12, jsonBytes.length + jsonPad, true);
  view.setUint32(16, 0x4e4f534a, true); // "JSON"
  bytes.set(jsonBytes, 20);
  bytes.fill(0x20, 20 + jsonBytes.length, 20 + jsonBytes.length + jsonPad); // pad with spaces
  let p = 20 + jsonBytes.length + jsonPad;
  view.setUint32(p, offset + binPad, true);
  view.setUint32(p + 4, 0x004e4942, true); // "BIN\0"
  p += 8;
  for (const chunk of chunks) {
    bytes.set(new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength), p);
    p += chunk.byteLength;
  }
  return out;
}

// The VRM humanoid tree — who each bone hangs off. Used to pick the nearest
// bone an avatar actually has as the parent (upperChest and shoulders are
// optional, so an arm may hang straight off the chest).
export const HUMANOID_PARENT = {
  hips: null,
  spine: "hips", chest: "spine", upperChest: "chest", neck: "upperChest", head: "neck",
  leftEye: "head", rightEye: "head", jaw: "head",
  leftShoulder: "upperChest", leftUpperArm: "leftShoulder", leftLowerArm: "leftUpperArm", leftHand: "leftLowerArm",
  rightShoulder: "upperChest", rightUpperArm: "rightShoulder", rightLowerArm: "rightUpperArm", rightHand: "rightLowerArm",
  leftUpperLeg: "hips", leftLowerLeg: "leftUpperLeg", leftFoot: "leftLowerLeg", leftToes: "leftFoot",
  rightUpperLeg: "hips", rightLowerLeg: "rightUpperLeg", rightFoot: "rightLowerLeg", rightToes: "rightFoot",
};

// Parents-before-children order, so a skeleton built from it is always valid.
export const HUMANOID_ORDER = [
  "hips", "spine", "chest", "upperChest", "neck", "head", "leftEye", "rightEye", "jaw",
  "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
  "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
  "leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes",
  "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes",
];

export function nearestParent(bone, has) {
  let parent = HUMANOID_PARENT[bone];
  while (parent && !has(parent)) parent = HUMANOID_PARENT[parent];
  return parent || null;
}
