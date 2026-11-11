"""Will this VRM work on a Code27? A checklist, read straight off the file.

The Code27 companion device takes "standard humanoid VRM models", plays its own
library of full-body animations on them through the VRM humanoid bone map,
lip-syncs with the VRM mouth shapes, and emotes with the VRM expression
presets. Its maker's published guidance (support pages and the "bring your VRM
character to life" article) adds: materials should be MToon, the rest pose a
clean T-pose, and spring bones give hair and clothes their physics.

This module checks a file against THAT — the published requirements. It is not
Code27's own import check (theirs runs on the device, since firmware 1.5.0, and
nobody outside the company has its rules), so a clean sheet here means "nothing
we know of is missing", not "guaranteed to import".

Pure Python + numpy, no Blender: it parses the GLB container and reads the
JSON, plus the skin weights out of the binary chunk — because the one failure
that matters most is invisible in the JSON. A mesh whose every vertex is tied
to a single bone is a valid file and a statue.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import numpy as np

NOTE = (
    "Checked against Code27's published requirements for custom characters. "
    "This is not Code27's own import check, which runs on the device."
)

# VRM 1.0's fifteen required humanoid bones (the same fifteen VRM 0.x needs).
REQUIRED_BONES = (
    "hips", "spine", "head",
    "leftUpperArm", "leftLowerArm", "leftHand",
    "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "leftFoot",
    "rightUpperLeg", "rightLowerLeg", "rightFoot",
)
MOUTH = ("aa", "ih", "ou", "ee", "oh")
BLINK = ("blink",)
EMOTIONS = ("happy", "angry", "sad", "relaxed", "surprised")
LOOK = ("lookUp", "lookDown", "lookLeft", "lookRight")
# VRM 0.x called the same things by other names.
_VRM0_PRESETS = {
    "a": "aa", "i": "ih", "u": "ou", "e": "ee", "o": "oh",
    "blink": "blink", "blink_l": "blinkLeft", "blink_r": "blinkRight",
    "joy": "happy", "angry": "angry", "sorrow": "sad", "fun": "relaxed",
    "lookup": "lookUp", "lookdown": "lookDown", "lookleft": "lookLeft", "lookright": "lookRight",
    "neutral": "neutral",
}

_COMPONENT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_MAX_JSON = 64_000_000


class NotAVrm(ValueError):
    pass


def read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    """The JSON document and the binary chunk of a .glb/.vrm."""
    with open(path, "rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"glTF":
            raise NotAVrm("not a binary glTF file")
        length, kind = struct.unpack("<I4s", handle.read(8))
        if kind != b"JSON" or length > _MAX_JSON:
            raise NotAVrm("unreadable glTF header")
        try:
            doc = json.loads(handle.read(length))
        except ValueError as exc:
            raise NotAVrm("the glTF JSON is corrupt") from exc
        blob = b""
        chunk = handle.read(8)
        if len(chunk) == 8:
            blob_length, blob_kind = struct.unpack("<I4s", chunk)
            if blob_kind == b"BIN\x00":
                blob = handle.read(blob_length)
    if not isinstance(doc, dict):
        raise NotAVrm("not a glTF document")
    return doc, blob


def _accessor(doc: dict[str, Any], blob: bytes, index: int) -> np.ndarray | None:
    """One accessor as an (n, width) array, or None if it can't be read."""
    try:
        acc = doc["accessors"][index]
        view = doc["bufferViews"][acc["bufferView"]]
        dtype = np.dtype(_COMPONENT[acc["componentType"]])
        width = _WIDTH[acc["type"]]
        count = int(acc["count"])
        start = int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
        size = dtype.itemsize * width
        stride = int(view.get("byteStride") or size)
        if count <= 0 or start + stride * (count - 1) + size > len(blob):
            return None
        if stride == size:
            return np.frombuffer(blob, dtype=dtype, count=count * width, offset=start).reshape(count, width)
        raw = np.frombuffer(blob, dtype=np.uint8, count=stride * (count - 1) + size, offset=start)
        rows = np.lib.stride_tricks.as_strided(raw, shape=(count, size), strides=(stride, 1))
        return np.ascontiguousarray(rows).view(dtype).reshape(count, width)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _humanoid(doc: dict[str, Any]) -> tuple[str, dict[str, int], dict[str, bool]]:
    """(spec version, bone name → node index, expression preset → is it bound)."""
    ext = doc.get("extensions") or {}
    if "VRMC_vrm" in ext:
        vrm = ext["VRMC_vrm"]
        bones = {
            name: entry.get("node")
            for name, entry in ((vrm.get("humanoid") or {}).get("humanBones") or {}).items()
            if isinstance(entry, dict) and isinstance(entry.get("node"), int)
        }
        presets = ((vrm.get("expressions") or {}).get("preset") or {})
        bound = {name: bool((entry or {}).get("morphTargetBinds")) for name, entry in presets.items()}
        return str(vrm.get("specVersion") or "1.0"), bones, bound
    if "VRM" in ext:
        vrm = ext["VRM"]
        bones = {}
        for entry in ((vrm.get("humanoid") or {}).get("humanBones") or []):
            if isinstance(entry, dict) and isinstance(entry.get("node"), int) and entry.get("bone"):
                bones[str(entry["bone"])] = entry["node"]
        bound = {}
        for group in ((vrm.get("blendShapeMaster") or {}).get("blendShapeGroups") or []):
            name = _VRM0_PRESETS.get(str(group.get("presetName") or "").lower())
            if name:
                bound[name] = bool(group.get("binds"))
        return "0.x", bones, bound
    raise NotAVrm("no VRM data in this file — it's a plain glTF model, not a VRM")


def _node_positions(doc: dict[str, Any]) -> dict[int, np.ndarray]:
    """World position of every node, from the rest transforms."""
    nodes = doc.get("nodes") or []

    def local(node: dict[str, Any]) -> np.ndarray:
        if "matrix" in node:
            return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
        m = np.eye(4)
        x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
        rot = np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ]
        )
        m[:3, :3] = rot * np.array(node.get("scale", (1.0, 1.0, 1.0)), dtype=np.float64)
        m[:3, 3] = node.get("translation", (0.0, 0.0, 0.0))
        return m

    parent: dict[int, int] = {}
    for index, node in enumerate(nodes):
        for child in node.get("children") or []:
            if isinstance(child, int):
                parent[child] = index
    world: dict[int, np.ndarray] = {}

    def resolve(index: int, depth: int = 0) -> np.ndarray:
        if index in world:
            return world[index]
        m = local(nodes[index])
        if index in parent and depth < 256:
            m = resolve(parent[index], depth + 1) @ m
        world[index] = m
        return m

    return {i: resolve(i)[:3, 3] for i in range(len(nodes))}


def _skin_report(doc: dict[str, Any], blob: bytes, bones: dict[str, int]) -> dict[str, Any]:
    """How the mesh is actually tied to the skeleton."""
    human_nodes = set(bones.values())
    nodes = doc.get("nodes") or []
    skins = doc.get("skins") or []
    total = on_humanoid = unreadable = 0
    per_joint: dict[int, int] = {}
    for node in nodes:
        if "mesh" not in node:
            continue
        mesh = (doc.get("meshes") or [])[node["mesh"]]
        joints = skins[node["skin"]].get("joints", []) if isinstance(node.get("skin"), int) and node["skin"] < len(skins) else None
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            count = int((doc["accessors"][attrs["POSITION"]]).get("count", 0)) if "POSITION" in attrs else 0
            total += count
            if joints is None or "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
                per_joint[-1] = per_joint.get(-1, 0) + count
                continue
            j = _accessor(doc, blob, attrs["JOINTS_0"])
            w = _accessor(doc, blob, attrs["WEIGHTS_0"])
            if j is None or w is None or len(j) != len(w):
                unreadable += count
                continue
            lead = j[np.arange(len(j)), np.argmax(w.astype(np.float64), axis=1)].astype(np.int64)
            lead = np.clip(lead, 0, max(len(joints) - 1, 0))
            ids, counts = np.unique(lead, return_counts=True)
            for joint_slot, n in zip(ids, counts):
                node_index = joints[int(joint_slot)] if joints else -1
                per_joint[node_index] = per_joint.get(node_index, 0) + int(n)
                if node_index in human_nodes:
                    on_humanoid += int(n)
    biggest = max(per_joint.values(), default=0)
    return {
        "vertices": total,
        "on_humanoid": on_humanoid,
        "unreadable": unreadable,
        "largest_share": (biggest / total) if total else 0.0,
        "bones_used": sum(1 for node_index, n in per_joint.items() if node_index in human_nodes and n > 0),
    }


def _item(key: str, level: str, title: str, detail: str) -> dict[str, str]:
    return {"id": key, "level": level, "title": title, "detail": detail}


def check_code27(path: str | Path) -> dict[str, Any]:
    """The checklist. ``verdict`` is the worst level on it: pass, warn or fail."""
    path = Path(path)
    items: list[dict[str, str]] = []
    try:
        doc, blob = read_glb(path)
        version, bones, bound = _humanoid(doc)
    except (OSError, NotAVrm) as exc:
        return {
            "target": "code27",
            "verdict": "fail",
            "note": NOTE,
            "items": [_item("format", "fail", "Not a VRM", str(exc))],
        }

    items.append(
        _item("format", "pass", f"VRM {version}", "Code27 takes standard humanoid VRM models, 0.x and 1.x.")
    )

    missing = [b for b in REQUIRED_BONES if b not in bones]
    if missing:
        items.append(
            _item(
                "bones", "fail", f"{len(missing)} required bone{'s' if len(missing) != 1 else ''} missing",
                "Without these the device's animations have nothing to drive: " + ", ".join(missing) + ".",
            )
        )
    else:
        items.append(
            _item("bones", "pass", "All 15 required bones", f"{len(bones)} humanoid bones mapped in total.")
        )

    skin = _skin_report(doc, blob, bones)
    if not skin["vertices"]:
        items.append(_item("skin", "fail", "No mesh", "There's a skeleton here but nothing for it to move."))
    elif skin["unreadable"] >= skin["vertices"]:
        items.append(_item("skin", "warn", "Couldn't read the skin weights", "The mesh is compressed or stored in a way this check can't open."))
    elif skin["largest_share"] > 0.9 or skin["on_humanoid"] < 0.5 * skin["vertices"]:
        items.append(
            _item(
                "skin", "fail", "The body isn't bound to the skeleton",
                f"{skin['largest_share']:.0%} of the mesh follows a single bone, so it will slide around as a "
                "statue instead of moving. Rig it again.",
            )
        )
    else:
        items.append(
            _item(
                "skin", "pass", "Body follows the skeleton",
                f"{skin['bones_used']} bones each move part of the mesh.",
            )
        )

    finger_bones = [b for b in bones if any(f in b for f in ("Thumb", "Index", "Middle", "Ring", "Little"))]
    if finger_bones:
        items.append(_item("fingers", "pass", "Finger bones", f"{len(finger_bones)} finger bones — hands can open, close and gesture."))
    else:
        items.append(
            _item(
                "fingers", "warn", "No finger bones",
                "Optional in VRM, but gestures and dances will play with stiff, mitten-like hands.",
            )
        )

    pose = _t_pose(doc, bones)
    if pose is not None:
        angle, ok = pose
        if ok:
            items.append(_item("pose", "pass", "Rest pose is a T-pose", "Arms straight out, which is what animations are measured from."))
        else:
            items.append(
                _item(
                    "pose", "warn", "Rest pose isn't a T-pose",
                    f"The arms rest about {angle:.0f}° off horizontal, so every animation will be off by that much.",
                )
            )

    def have(names: tuple[str, ...]) -> list[str]:
        return [n for n in names if bound.get(n)]

    mouth = have(MOUTH)
    if len(mouth) == len(MOUTH):
        items.append(_item("lipsync", "pass", "Mouth shapes for lip-sync", "aa, ih, ou, ee and oh are all there."))
    elif mouth:
        gone = [n for n in MOUTH if n not in mouth]
        items.append(_item("lipsync", "warn", "Some mouth shapes missing", "Lip-sync will skip: " + ", ".join(gone) + "."))
    else:
        items.append(_item("lipsync", "fail", "No mouth shapes", "The character will talk without moving its mouth."))

    if have(BLINK):
        items.append(_item("blink", "pass", "Blink", "Eyes can close."))
    else:
        items.append(_item("blink", "warn", "No blink", "The character will never blink."))

    feelings = have(EMOTIONS)
    if len(feelings) == len(EMOTIONS):
        items.append(_item("emotions", "pass", "All five emotions", "happy, angry, sad, relaxed and surprised."))
    else:
        gone = [n for n in EMOTIONS if n not in feelings]
        items.append(
            _item(
                "emotions", "warn", f"{len(gone)} of 5 emotions missing",
                "The device's import check flags these, and the face stays blank for: " + ", ".join(gone) + ".",
            )
        )

    looks = have(LOOK)
    has_eye_bones = "leftEye" in bones and "rightEye" in bones
    if has_eye_bones or len(looks) == len(LOOK):
        items.append(_item("look", "pass", "Eyes can look around", "Eye bones or look shapes are present."))
    else:
        items.append(_item("look", "info", "Eyes don't move", "No eye bones or look-direction shapes. The head still turns."))

    materials = doc.get("materials") or []
    if "VRMC_vrm" in (doc.get("extensions") or {}):
        toon = sum(1 for m in materials if "VRMC_materials_mtoon" in (m.get("extensions") or {}))
    else:
        props = ((doc.get("extensions") or {}).get("VRM") or {}).get("materialProperties") or []
        toon = sum(1 for m in props if "MToon" in str(m.get("shader", "")))
    if materials and toon == len(materials):
        items.append(_item("materials", "pass", "MToon materials", f"All {len(materials)} use VRM's toon shader."))
    else:
        items.append(
            _item(
                "materials", "warn", f"{len(materials) - toon} of {len(materials)} materials aren't MToon",
                "Code27's guide says other shader setups don't survive export, so colours and shading may look different on the device.",
            )
        )

    ext = doc.get("extensions") or {}
    springs = bool((ext.get("VRMC_springBone") or {}).get("springs")) or bool(
        ((ext.get("VRM") or {}).get("secondaryAnimation") or {}).get("boneGroups")
    )
    if springs:
        items.append(_item("springs", "pass", "Spring bones", "Hair or clothing has physics."))
    else:
        items.append(
            _item("springs", "warn", "No spring bones", "Hair and clothes will move rigidly with the body. Fine for most characters.")
        )

    triangles = 0
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            try:
                if "indices" in prim:
                    triangles += int(doc["accessors"][prim["indices"]]["count"]) // 3
                else:
                    triangles += int(doc["accessors"][prim["attributes"]["POSITION"]]["count"]) // 3
            except (KeyError, IndexError, TypeError):
                pass
    try:
        size_mb = path.stat().st_size / 1e6
    except OSError:
        size_mb = 0.0
    items.append(
        _item(
            "size", "info", f"{triangles:,} triangles · {size_mb:.1f} MB",
            f"{len(doc.get('textures') or [])} textures. Code27 publishes no limits; this is in the range of ordinary VRoid models.",
        )
    )

    levels = {i["level"] for i in items}
    verdict = "fail" if "fail" in levels else "warn" if "warn" in levels else "pass"
    return {
        "target": "code27",
        "verdict": verdict,
        "vrm_version": version,
        "counts": {level: sum(1 for i in items if i["level"] == level) for level in ("pass", "warn", "fail", "info")},
        "note": NOTE,
        "items": items,
    }


def _t_pose(doc: dict[str, Any], bones: dict[str, int]) -> tuple[float, bool] | None:
    """(degrees the arms sit off horizontal, close enough?) or None if unknown."""
    needed = ("leftUpperArm", "leftLowerArm", "rightUpperArm", "rightLowerArm")
    if any(b not in bones for b in needed):
        return None
    try:
        where = _node_positions(doc)
        worst = 0.0
        for upper, lower in (("leftUpperArm", "leftLowerArm"), ("rightUpperArm", "rightLowerArm")):
            arm = where[bones[lower]] - where[bones[upper]]
            length = float(np.linalg.norm(arm))
            if length < 1e-6:
                return None
            # glTF: X is side to side. A T-pose arm runs along it.
            off = float(np.degrees(np.arccos(min(1.0, abs(float(arm[0])) / length))))
            worst = max(worst, off)
        return worst, worst <= 20.0
    except (KeyError, IndexError, TypeError, ValueError):
        return None
