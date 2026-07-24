"""Auto-rig, the half that runs inside Blender.

Launched by ``video_stream/autorig.py`` as::

    blender --background --python autorig_blender.py -- --verb rig --args job.json

and it talks back over stdout: ``RIGEVENT>{json}`` lines are progress, one
``RIGRESULT>{json}`` line at the end is the answer. Nothing here imports
``video_stream`` — this is Blender's Python, a different interpreter with a
different set of modules.

Three verbs:

``probe``    what Blender is this, is the VRM add-on here.
``analyze``  import, measure, guess the archetype, render previews. Cheap
             enough to run on upload, and everything it measures is handed
             back so the rig pass doesn't have to guess twice.
``rig``      the real thing: decimate, face the model the way VRM expects,
             build the archetype's skeleton fitted to the measured landmarks,
             skin it, generate blink/mouth shape keys, write the VRM humanoid
             mapping and expressions, export.

Two conventions worth knowing before reading the geometry code:

* **Blender -Y is the avatar's front.** The glTF exporter maps Blender
  ``(x, y, z)`` to glTF ``(x, z, -y)``, so a model looking down Blender -Y
  looks down glTF +Z, which is where VRM 1.0 says the front is. Tripo exports
  face +X, so nearly every model gets spun a quarter turn on the way in.
* **Everything is a fraction of height.** The recipes in ``archetypes.json``
  never mention metres, so the same penguin recipe fits a 12 cm figurine and a
  3 m one; the fitter warps those fractions onto the landmarks it actually
  measured (leg split, neck pinch, wing tip) instead of trusting them blindly.
"""

import argparse
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import bpy
import addon_utils
import numpy as np
from mathutils import Matrix, Vector, kdtree

VRM_MODULE = "bl_ext.blender_org.vrm"
HERE = Path(__file__).resolve().parent
T0 = time.time()


# ── talking to the server ─────────────────────────────────────────────
def emit(step=None, progress=None, log=None):
    payload = {}
    if step is not None:
        payload["step"] = step
    if progress is not None:
        payload["progress"] = progress
    if log is not None:
        payload["log"] = log
    print("RIGEVENT>" + json.dumps(payload), flush=True)


def finish(**payload):
    payload.setdefault("ok", True)
    payload["seconds"] = round(time.time() - T0, 2)
    print("RIGRESULT>" + json.dumps(payload), flush=True)


# ── Blender odds and ends ─────────────────────────────────────────────
def enable_vrm():
    module = addon_utils.enable(VRM_MODULE, default_set=True, persistent=True)
    if module is None:
        raise RuntimeError(
            "Blender's VRM add-on isn't installed. Get it from vrm-addon-for-blender "
            "(Blender → Edit → Preferences → Get Extensions → search 'VRM')."
        )
    from bl_ext.blender_org.vrm.common.version import get_addon_version

    return get_addon_version()


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def activate(obj, *extra):
    bpy.ops.object.select_all(action="DESELECT")
    for other in extra:
        other.select_set(True)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def bake(obj, matrix=None):
    """Fold a transform into the mesh data itself.

    ``object.transform_apply`` looks like the obvious call and reports success,
    but in ``--background`` it silently does nothing to a transform that Python
    just assigned: the operator reads the evaluated object, and nothing has
    re-evaluated the depsgraph yet. Writing the vertices is never wrong."""
    m = matrix if matrix is not None else obj.matrix_world.copy()
    obj.data.transform(m)
    obj.data.update()
    if matrix is None:
        obj.matrix_world = Matrix.Identity(4)


def coords(obj):
    """Every vertex as an (n, 3) array. Local space == world space here: the
    importers' transforms are applied before anything else touches the mesh."""
    mesh = obj.data
    buf = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", buf)
    return buf.reshape(-1, 3)


def tri_count(obj):
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def load_recipes():
    return json.loads((HERE / "archetypes.json").read_text())


def scrub_external_images(source_path):
    """Throw away any texture the importer pulled off this machine's disk.

    A model file can name its textures by path, and Blender resolves those
    against the local filesystem and PACKS whatever it finds — including files
    that aren't images at all. Since the uploader only ever sends one file, any
    texture living outside the upload's own directory came from this host and
    has no business riding out inside the exported VRM."""
    safe = Path(source_path).resolve().parent
    for image in list(bpy.data.images):
        raw = (image.filepath_from_user() or "").strip()
        if not raw:
            continue  # embedded in the model itself, which is what we want
        try:
            resolved = Path(bpy.path.abspath(raw)).resolve()
            inside = resolved == safe or safe in resolved.parents
        except (OSError, ValueError):
            inside = False
        if not inside:
            emit(log=f"dropped texture loaded from outside the upload: {raw[:80]}")
            image.filepath = ""
            if image.packed_file:
                image.unpack(method="REMOVE")
            bpy.data.images.remove(image, do_unlink=True)


# ── import ────────────────────────────────────────────────────────────
def import_source(path):
    suffix = Path(path).suffix.lower()
    if suffix in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        raise RuntimeError(f"can't import '{suffix}' files")

    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("that file has no mesh in it")

    # Any armature that rode along is one we didn't build and can't map — the
    # mesh is what we want. Unparent first so removing it can't drag the mesh.
    for obj in meshes:
        if obj.parent is not None and obj.parent.type == "ARMATURE":
            world = obj.matrix_world.copy()
            obj.parent = None
            obj.matrix_world = world
        for mod in list(obj.modifiers):
            if mod.type == "ARMATURE":
                obj.modifiers.remove(mod)
    for obj in [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]:
        bpy.data.objects.remove(obj, do_unlink=True)

    scrub_external_images(path)
    for obj in meshes:
        bake(obj)
    activate(meshes[0], *meshes[1:])
    if len(meshes) > 1:
        bpy.ops.object.join()
    body = bpy.context.view_layer.objects.active
    body.name = "Body"
    body.data.name = "Body"
    # Shape keys inherited from the source would collide with the ones we add
    # (and block the join we just did if they disagreed).
    if body.data.shape_keys:
        body.shape_key_clear()
    return body


def decimate(body, target_tris):
    before = tri_count(body)
    if target_tris <= 0 or before <= target_tris:
        return before, before
    activate(body)
    mod = body.modifiers.new("Decimate", "DECIMATE")
    mod.ratio = max(0.01, min(1.0, target_tris / float(before)))
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return before, tri_count(body)


# ── orientation: which way is this thing looking ──────────────────────
def _symmetry_error(points, axis, center, samples=5000):
    """Mean distance from a mirrored point to the nearest real one, in model
    units. The plane a creature is symmetric about is the one down its nose."""
    step = max(1, len(points) // samples)
    sample = points[::step]
    tree = kdtree.KDTree(len(sample))
    for i, p in enumerate(sample):
        tree.insert(Vector(p), i)
    tree.balance()
    total = 0.0
    for p in sample:
        q = Vector(p)
        q[axis] = 2.0 * center - q[axis]
        _, _, dist = tree.find(q)
        total += dist
    return total / len(sample)


def orient(body, yaw_deg=0.0, face_front=True):
    """Spin the model until it faces Blender -Y, stand it on z=0, centre it.

    Returns what it decided, so the UI can say "I turned this 90°" and the
    user can argue with it."""
    pts = coords(body)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    mid = (lo + hi) / 2.0
    diag = float(np.linalg.norm(hi - lo)) or 1.0

    err_x = _symmetry_error(pts, 0, float(mid[0]))
    err_y = _symmetry_error(pts, 1, float(mid[1]))
    # Symmetric about x=0 → the nose points along y, and vice versa.
    fb_axis = 1 if err_x <= err_y else 0
    lr_axis = 1 - fb_axis
    confidence = abs(err_x - err_y) / max(err_x, err_y, 1e-9)

    # Front-vs-back: heads lead. A beak, snout or nose puts the head's centre
    # of mass ahead of the body's; toes break the tie the same way.
    height = float(hi[2] - lo[2]) or 1.0
    head = pts[pts[:, 2] > lo[2] + 0.78 * height]
    feet = pts[pts[:, 2] < lo[2] + 0.10 * height]
    lead = 0.0
    if len(head):
        lead += float(head[:, fb_axis].mean() - pts[:, fb_axis].mean())
    if len(feet):
        lead += 0.5 * float(feet[:, fb_axis].mean() - pts[:, fb_axis].mean())
    facing = 1.0 if lead >= 0 else -1.0
    if not face_front:
        facing = -facing

    # Turn `facing` along `fb_axis` into -Y.
    if fb_axis == 0:
        turn = -90.0 if facing > 0 else 90.0
    else:
        turn = 180.0 if facing > 0 else 0.0
    turn += float(yaw_deg)

    activate(body)
    bake(body, Matrix.Rotation(math.radians(turn), 4, "Z"))

    pts = coords(body)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    mid = (lo + hi) / 2.0
    bake(body, Matrix.Translation((-float(mid[0]), -float(mid[1]), -float(lo[2]))))

    return {
        "turned_deg": round(turn, 1),
        "front_back_axis": "x" if fb_axis == 0 else "y",
        "faced": "+x" if (fb_axis == 0 and facing > 0) else ("-x" if fb_axis == 0 else ("+y" if facing > 0 else "-y")),
        "symmetry_error": {"x_plane": round(err_x / diag, 4), "y_plane": round(err_y / diag, 4)},
        "symmetry_confidence": round(confidence, 3),
    }


def rescale(body, height_m):
    pts = coords(body)
    height = float(pts[:, 2].max() - pts[:, 2].min()) or 1.0
    if height_m <= 0:
        return height, 1.0
    factor = height_m / height
    activate(body)
    bake(body, Matrix.Scale(factor, 4))
    pts = coords(body)
    bake(body, Matrix.Translation((0.0, 0.0, -float(pts[:, 2].min()))))
    return height, factor


# ── measuring ─────────────────────────────────────────────────────────
BANDS = 48


def _lobes(xs, span):
    """How many separate blobs this horizontal slice has, side to side. Two
    means legs; three means legs plus something sticking out."""
    if len(xs) < 24 or span <= 0:
        return 1
    counts, _ = np.histogram(xs, bins=32)
    floor = max(2, int(0.006 * len(xs)))
    occupied = counts > floor
    runs, prev_gap = 0, 2
    for cell in occupied:
        if cell:
            if prev_gap >= 2:
                runs += 1
            prev_gap = 0
        else:
            prev_gap += 1
    return max(1, runs)


def measure(body):
    """Everything the fitter needs: the silhouette band by band, then the
    landmarks worth naming (where the legs split, where the neck pinches in,
    how far a wing reaches)."""
    pts = coords(body)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    height = float(hi[2] - lo[2]) or 1.0
    z = pts[:, 2] - lo[2]
    index = np.clip((z / height * BANDS).astype(int), 0, BANDS - 1)

    bands = []
    for b in range(BANDS):
        sel = pts[index == b]
        if not len(sel):
            bands.append({"z": (b + 0.5) / BANDS, "w": 0.0, "d": 0.0, "n": 0, "lobes": 0})
            continue
        width = float(sel[:, 0].max() - sel[:, 0].min())
        bands.append(
            {
                "z": round((b + 0.5) / BANDS, 4),
                "w": round(width / height, 4),
                "d": round(float(sel[:, 1].max() - sel[:, 1].min()) / height, 4),
                "n": int(len(sel)),
                "lobes": _lobes(sel[:, 0], width),
            }
        )

    def band_at(frac):
        return bands[min(BANDS - 1, max(0, int(frac * BANDS)))]

    # A slice has to carry real mesh to count: a dragging tail tip or a wisp
    # of hair touching the floor is not the bottom of the body.
    floor_n = max(12, int(0.006 * len(pts)))
    middle = [b for b in bands if 0.30 <= b["z"] <= 0.70 and b["n"] > floor_n]
    # Median, not max: one slice through a pair of outstretched arms is not
    # how wide the torso is.
    torso_w = float(np.median([b["w"] for b in middle])) if middle else 0.3
    torso_area = float(np.median([b["w"] * b["d"] for b in middle])) if middle else 0.09

    # The hip line — the lowest slice where the body has filled out to most of
    # its width. Below it is legs, or nothing. This survives a fat body that
    # swallows its own legs (every penguin) far better than looking for the
    # slice that splits in two, which also can't tell a leg gap from the gap
    # between a sleeve and a jacket.
    # Cross-section AREA, not width: a pair of feet can be as wide as the
    # body they hold up, but never as deep. Start from the narrowest slice down
    # low — the ankle — and walk up until the body has filled out again. That
    # single line means "legs" whether they're a penguin's stubs, a pig's
    # thick shins, or a person's.
    lower = [i for i, b in enumerate(bands) if b["z"] < 0.45 and b["n"] > floor_n]
    ankle = min(lower, key=lambda i: bands[i]["w"] * bands[i]["d"]) if lower else 0
    hip = None
    for i in range(ankle, int(0.75 * BANDS)):
        band = bands[i]
        following = bands[min(i + 1, BANDS - 1)]
        if (
            band["n"] > floor_n
            and band["w"] * band["d"] >= 0.55 * torso_area
            and following["w"] * following["d"] >= 0.50 * torso_area
        ):
            hip = band["z"]
            break

    # Where the legs visibly separate, when they do at all. Only used to say
    # "yes, those are two legs" — the hip line above decides how long they are.
    crotch = None
    start = next(
        (i for i in range(0, min(5, BANDS)) if bands[i]["n"] > floor_n and bands[i]["lobes"] >= 2),
        None,
    )
    if start is not None:
        top = start
        for i in range(start, int(0.62 * BANDS)):
            if bands[i]["n"] > floor_n and bands[i]["lobes"] >= 2:
                top = i
            else:
                break
        crotch = bands[top]["z"]

    # Where the neck pinches: the narrowest slice between shoulders and crown,
    # and only if it's a real waist rather than a gentle taper.
    neck = None
    window = [b for b in bands if 0.55 <= b["z"] <= 0.93 and b["n"] > floor_n]
    if window:
        pinch = min(window, key=lambda b: b["w"])
        above = max((b["w"] for b in bands if b["z"] > pinch["z"] and b["n"] > floor_n), default=0.0)
        if above > pinch["w"] * 1.12:
            neck = pinch["z"]

    # Shoulders: the last slice below the neck that's still full torso width.
    shoulder = None
    if neck:
        for b in reversed([x for x in bands if x["z"] < neck and x["n"] > floor_n]):
            if b["w"] >= 0.80 * torso_w:
                shoulder = b["z"]
                break

    # The wing/arm tip: the furthest-out vertex above the legs. Only counts as
    # an arm if it reaches meaningfully past the torso.
    arm = None
    upper = pts[(z > (hip or 0.3) * height * 0.85) & (z < (neck or 0.85) * height)]
    if len(upper):
        far = upper[np.argmax(np.abs(upper[:, 0]))]
        reach = abs(float(far[0])) / height
        if reach > 0.55 * torso_w:
            arm = {
                "out": round(reach, 4),
                "z": round(float(far[2] - lo[2]) / height, 4),
                "fb": round(float(far[1]) / height, 4),
                "reach_ratio": round(reach / max(torso_w * 0.5, 1e-6), 3),
            }

    head_band = [b for b in bands if b["z"] >= (neck or 0.82)]
    head_w = max((b["w"] for b in head_band), default=0.2)
    head_pts = pts[z >= (neck or 0.82) * height]
    head_reach = 0.0
    if len(head_pts):
        head_reach = float(pts[:, 1].mean() - head_pts[:, 1].min()) / height
    toe = pts[z < 0.10 * height]
    toe_reach = float(pts[:, 1].mean() - toe[:, 1].min()) / height if len(toe) else 0.0

    return {
        "height": height,
        "width": round(float(hi[0] - lo[0]) / height, 4),
        "depth": round(float(hi[1] - lo[1]) / height, 4),
        "tris": tri_count(body),
        "verts": int(len(pts)),
        "bands": bands,
        "torso_w": round(torso_w, 4),
        "hip_z": hip,
        "crotch_z": crotch,
        "neck_z": neck,
        "shoulder_z": shoulder,
        "arm": arm,
        "head_w": round(head_w, 4),
        "head_reach": round(head_reach, 4),
        "toe_reach": round(toe_reach, 4),
        "bottom_lobes": max((band_at(f)["lobes"] for f in (0.02, 0.05, 0.09)), default=1),
    }


def suggest(m):
    """Guess the archetype, and be able to say why — the UI shows the reasons
    next to the chips, so a wrong guess is obvious rather than mysterious.

    Leg length carries most of the decision: a person is half legs, a penguin
    is a sixth, and cartoon animals live in between."""
    reasons = []
    neck = m["neck_z"] or 0.80
    head_frac = 1.0 - neck
    legs = m["hip_z"] if m["hip_z"] is not None else 0.0
    snout = m["head_reach"] > 0.09

    if m["depth"] > 1.15 and m["depth"] > m["width"] * 1.25:
        reasons.append(f"reaches further front-to-back ({m['depth']:.2f}×height) than it stands tall")
        return "quadruped", 0.7, reasons

    if legs < 0.05:
        if neck < 0.5 or head_frac > 0.45:
            reasons.append("nothing below the head — rigging it as a bust")
            return "bust", 0.62, reasons
        reasons.append("body reaches the floor with no legs under it")
        return "blob", 0.5, reasons

    reasons.append(f"body starts {legs:.0%} up — that's how much of it is legs")
    if legs < 0.24:
        if snout:
            reasons.append(f"face juts {m['head_reach']:.2f}×height forward: a beak or a snout")
            return "bird_biped", 0.74, reasons
        reasons.append("stubby legs under a heavy body")
        return "humanoid_creature", 0.55, reasons

    if legs < 0.40 or head_frac > 0.26:
        reasons.append(f"head is {head_frac:.0%} of the body and the legs are short for a person")
        return "humanoid_creature", 0.62, reasons

    if m["depth"] <= 1.05 and m["width"] <= 1.15:
        reasons.append("human proportions: small head, long limbs")
        return "humanoid", 0.68, reasons
    reasons.append("unusual proportions — falling back to the standing-creature rig")
    return "humanoid_creature", 0.4, reasons


# ── previews ──────────────────────────────────────────────────────────
def setup_render(width=384, height=512):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"       # CPU Cycles is the one renderer that
    scene.cycles.device = "CPU"          # always works headless, on any box
    scene.cycles.samples = 10
    scene.cycles.use_denoising = False
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"

    world = bpy.data.worlds.new("W")
    scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.055, 0.06, 0.07, 1)

    for energy, rot in ((4.0, (55, 0, 25)), (2.0, (70, 0, 210))):
        data = bpy.data.lights.new("Sun", "SUN")
        data.energy = energy
        light = bpy.data.objects.new("Sun", data)
        scene.collection.objects.link(light)
        light.rotation_euler = tuple(math.radians(a) for a in rot)

    data = bpy.data.cameras.new("Cam")
    data.type = "ORTHO"
    data.sensor_fit = "VERTICAL"  # ortho_scale spans the image HEIGHT; the UI
    cam = bpy.data.objects.new("Cam", data)  # relies on that to map clicks back
    scene.collection.objects.link(cam)
    scene.camera = cam
    return cam


def render_views(body, out_dir, prefix, views=("front", "side"), focus=None):
    """Ortho renders, plus the numbers needed to turn a click on the picture
    back into a point in the model. ``focus`` frames the head instead of the
    whole body — that's the only shot in which a blink is visible."""
    pts = coords(body)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    mid = (lo + hi) / 2.0
    height = float(hi[2] - lo[2]) or 1.0
    scale = height * 1.12
    if focus:
        mid = np.array([mid[0], mid[1], focus["z"]], dtype=np.float64)
        scale = focus["scale"]
    cam = bpy.context.scene.camera
    cam.data.ortho_scale = scale
    dist = height * 4.0
    scene = bpy.context.scene
    aspect = scene.render.resolution_x / scene.render.resolution_y

    placements = {
        "front": ((mid[0], mid[1] - dist, mid[2]), (90, 0, 0)),
        "side": ((mid[0] + dist, mid[1], mid[2]), (90, 0, 90)),
        "back": ((mid[0], mid[1] + dist, mid[2]), (90, 0, 180)),
    }
    out = {}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for view in views:
        where, rot = placements[view]
        cam.location = where
        cam.rotation_euler = tuple(math.radians(a) for a in rot)
        path = str(Path(out_dir) / f"{prefix}{view}.png")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        out[view] = path
    return out, {
        "ortho_scale": round(scale, 6),
        "aspect": round(aspect, 6),
        "center_x": round(float(mid[0]), 6),
        "center_z": round(float(mid[2]), 6),
        "height": round(height, 6),
    }


def scale_projection(projection, factor):
    """Re-express a camera the analysis pass recorded in the rig's units."""
    if not projection or factor in (0, 1.0):
        return projection
    scaled = dict(projection)
    for key in ("ortho_scale", "center_x", "center_z", "height"):
        if key in scaled:
            scaled[key] = scaled[key] * factor
    return scaled


def unproject(u, v, projection):
    """A click on the front preview → a point on the model's centre plane.
    Depth comes later, from whatever surface is actually there."""
    span_z = projection["ortho_scale"]
    span_x = span_z * projection["aspect"]
    return (
        projection["center_x"] + (float(u) - 0.5) * span_x,
        projection["center_z"] + (0.5 - float(v)) * span_z,
    )


# ── skeleton ──────────────────────────────────────────────────────────
# The VRM bones we always produce. Blender bone name → the add-on's property.
HUMAN_BONES = {
    "hips": "hips",
    "spine": "spine",
    "chest": "chest",
    "neck": "neck",
    "head": "head",
    "shoulder.L": None,
    "shoulder.R": None,
    "upperArm.L": "left_upper_arm",
    "lowerArm.L": "left_lower_arm",
    "hand.L": "left_hand",
    "upperArm.R": "right_upper_arm",
    "lowerArm.R": "right_lower_arm",
    "hand.R": "right_hand",
    "upperLeg.L": "left_upper_leg",
    "lowerLeg.L": "left_lower_leg",
    "foot.L": "left_foot",
    "upperLeg.R": "right_upper_leg",
    "lowerLeg.R": "right_lower_leg",
    "foot.R": "right_foot",
}
VRM_SHOULDERS = {"shoulder.L": "left_shoulder", "shoulder.R": "right_shoulder"}


def leg_line(m, recipe_legs):
    """How far up the body the legs reach, as the fitter will use it.

    The measured hip is the honest answer but it reads the *outside* of the
    creature: a penguin's belly hides the top of its legs, so the silhouette
    pinch sits lower than the joint does. Trusting it outright would drag the
    whole spine down with it, so it's allowed to move the recipe by 40% and no
    further."""
    measured = m.get("hip_z")
    if not measured:
        return recipe_legs
    return float(min(max(measured, 0.6 * recipe_legs), 1.4 * recipe_legs))


def _warp(recipe_legs, recipe_neck, m, leg_z):
    """Map recipe fractions onto the creature we actually have.

    The recipe says "hips at 22% of the height"; this creature's legs split at
    19% and its neck pinches at 58%. Rather than argue, stretch the recipe's
    own scale so its landmarks land on the measured ones and everything
    between rides along."""
    control = [(0.0, 0.0)]
    if leg_z:
        control.append((recipe_legs, leg_z))
    if m.get("neck_z"):
        control.append((recipe_neck, m["neck_z"]))
    control.append((1.0, 1.0))
    control = sorted({round(a, 6): (a, b) for a, b in control}.values())
    xs = [a for a, _ in control]
    ys = [b for _, b in control]
    # Non-monotonic control points would fold the skeleton in on itself.
    for i in range(1, len(xs)):
        if ys[i] <= ys[i - 1] or xs[i] <= xs[i - 1]:
            return lambda f: f

    def warp(frac):
        return float(np.interp(frac, xs, ys))

    return warp


def plan_bones(m, spec, height):
    """Where every bone goes, in metres, for this creature. Upright creatures
    hang off a vertical spine; four-legged ones off a horizontal one."""
    mode = spec.get("mode", "upright")
    S = spec["spine"]
    A = spec["arms"]
    L = spec["legs"]
    leg_z = leg_line(m, L["crotch_z"])
    warp = _warp(L["crotch_z"], S["neck"], m, leg_z)
    H = height

    def P(x, y, z):
        return Vector((x, y, z))

    bones = []          # (name, head, tail, parent, deform)
    if mode == "horizontal":
        # Spine along the body: front is -Y, so "stations" run from the nose
        # backwards. Bone height rides the animal's back.
        back = 0.78 * H
        depth = m["depth"] * H
        front = -depth / 2.0

        def station(frac):
            return front + frac * depth

        hips = P(0, station(S["hips"]), back * 0.92)
        spine = P(0, station(S["spine"]), back * 0.98)
        chest = P(0, station(S["chest"]), back)
        neck = P(0, station(S["neck"]), back * 1.02)
        head = P(0, station(S["head_top"]) - 0.02 * H, back * 1.05)
        head_tip = P(0, front - 0.06 * H, back * 1.02)
        bones += [
            ("hips", hips, spine, None, True),
            ("spine", spine, chest, "hips", True),
            ("chest", chest, neck, "spine", True),
            ("neck", neck, head, "chest", True),
            ("head", head, head_tip, "neck", True),
        ]
        leg_specs = [
            ("upperLeg", "lowerLeg", "foot", "hips", station(S["hips"]), L["hip_out"]),
            ("upperArm", "lowerArm", "hand", "chest", station(S["chest"]), A["shoulder_out"]),
        ]
        for upper, lower, end, parent, y, out in leg_specs:
            for side, sign in (("L", 1.0), ("R", -1.0)):
                top = P(sign * out * H, y, back * 0.85)
                knee = P(sign * out * H, y, back * 0.45)
                ankle = P(sign * out * H, y, 0.07 * H)
                toe = P(sign * out * H, y - L["foot_reach"] * H, 0.02 * H)
                if upper == "upperArm":
                    bones.append((f"shoulder.{side}", P(sign * out * H * 0.5, y, back * 0.92), top, parent, True))
                    parent_name = f"shoulder.{side}"
                else:
                    parent_name = parent
                bones += [
                    (f"{upper}.{side}", top, knee, parent_name, True),
                    (f"{lower}.{side}", knee, ankle, f"{upper}.{side}", True),
                    (f"{end}.{side}", ankle, toe, f"{lower}.{side}", True),
                ]
        return bones, mode

    # ---- upright -----------------------------------------------------
    z = lambda frac: warp(frac) * H  # noqa: E731 — reads better than a def here
    hips = P(0, 0, z(S["hips"]))
    spine = P(0, 0, z(S["spine"]))
    chest = P(0, 0, z(S["chest"]))
    neck = P(0, 0, z(S["neck"]))
    head = P(0, 0, z(S["head_top"]))
    bones += [
        ("hips", hips, spine, None, True),
        ("spine", spine, chest, "hips", True),
        ("chest", chest, neck, "spine", True),
        ("neck", neck, head, "chest", True),
        ("head", head, P(0, -0.01 * H, min(0.999, S["head_top"] + 0.11) * H), "neck", True),
    ]

    arms_on = A.get("present", True)
    tip_out = A["tip_out"] * H
    tip_z = z(A["tip_z"])
    tip_fb = 0.0
    if arms_on and m.get("arm"):
        # Measured beats guessed: this is where the wing or sleeve really ends.
        tip_out = m["arm"]["out"] * H
        tip_z = m["arm"]["z"] * H
        tip_fb = m["arm"]["fb"] * H
    # z() maps RECIPE fractions onto this body. A measured landmark is already
    # in body space, so it goes straight through — warping it again would move
    # it twice.
    shoulder_z = m["shoulder_z"] * H if m.get("shoulder_z") else z(A["shoulder_z"])
    root_out = A["shoulder_out"] * H
    for side, sign in (("L", 1.0), ("R", -1.0)):
        root = P(sign * root_out, 0, shoulder_z)
        tip = P(sign * tip_out, tip_fb, tip_z)

        def along(frac):
            return root.lerp(tip, frac)

        deform = arms_on
        bones += [
            (f"shoulder.{side}", root, along(0.16), "chest", deform),
            (f"upperArm.{side}", along(0.16), along(A["split"][0]), f"shoulder.{side}", deform),
            (f"lowerArm.{side}", along(A["split"][0]), along(A["split"][1]), f"upperArm.{side}", deform),
            (f"hand.{side}", along(A["split"][1]), tip, f"lowerArm.{side}", deform),
        ]

    legs_on = L.get("present", True)
    # By construction warp(recipe crotch) == leg_z, so this IS the measured
    # (clamped) leg line — passing leg_z through z() would warp it twice.
    crotch_z = z(L["crotch_z"])
    ankle_z = L["ankle_z"] * H
    hip_out = L["hip_out"] * H
    for side, sign in (("L", 1.0), ("R", -1.0)):
        top = P(sign * hip_out, 0, crotch_z)
        knee = P(sign * hip_out, 0, ankle_z + (crotch_z - ankle_z) * (1.0 - L["knee"]))
        ankle = P(sign * hip_out, 0, ankle_z)
        toe = P(sign * hip_out, -L["foot_reach"] * H, ankle_z * 0.45)
        bones += [
            (f"upperLeg.{side}", top, knee, "hips", legs_on),
            (f"lowerLeg.{side}", knee, ankle, f"upperLeg.{side}", legs_on),
            (f"foot.{side}", ankle, toe, f"lowerLeg.{side}", legs_on),
        ]
    return bones, mode


def build_armature(plan):
    data = bpy.data.armatures.new("Rig")
    rig = bpy.data.objects.new("Rig", data)
    bpy.context.scene.collection.objects.link(rig)
    activate(rig)
    bpy.ops.object.mode_set(mode="EDIT")
    for name, head, tail, parent, deform in plan:
        bone = data.edit_bones.new(name)
        bone.head = head
        bone.tail = tail
        if (tail - head).length < 1e-4:      # a zero-length bone is dropped on
            bone.tail = head + Vector((0, 0, 1e-3))  # exit from edit mode
        bone.roll = 0.0
        bone.use_connect = False
        # Bones we only keep because VRM insists on them (a bust has no legs)
        # are marked non-deforming, which also keeps automatic weights from
        # handing them half the mesh.
        bone.use_deform = bool(deform)
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    return rig


def _segment_distance(pts, head, tail):
    """Distance from every vertex to a bone, treated as a line segment."""
    a = np.array(head)
    ab = np.array(tail) - a
    denom = float(ab @ ab) or 1e-9
    t = np.clip(((pts - a) @ ab) / denom, 0.0, 1.0)[:, None]
    return np.linalg.norm(pts - (a + t * ab), axis=1)


def skin_by_proximity(body, rig):
    """Weight every vertex by how close it is to each bone.

    The fallback that always works. Heat weighting is better when it runs, but
    it needs watertight geometry and generated meshes rarely have any — a
    jacket modelled as its own floating shell is enough to make it give up and
    weight nothing at all. This can't fail: each vertex takes the two nearest
    bones, weighted by inverse square distance, so limbs still blend at the
    joints instead of snapping."""
    pts = coords(body)
    bones = [b for b in rig.data.bones if b.use_deform]
    dists = np.stack([_segment_distance(pts, b.head_local, b.tail_local) for b in bones], axis=1)
    order = np.argsort(dists, axis=1)[:, :2]
    rows = np.arange(len(pts))
    near, second = dists[rows, order[:, 0]], dists[rows, order[:, 1]]
    eps = max(1e-4, float(np.median(near)) * 0.05)
    w_near = 1.0 / (near + eps) ** 2
    w_second = np.where(second < 2.0 * (near + eps), 1.0 / (second + eps) ** 2, 0.0)
    total = w_near + w_second
    w_near, w_second = w_near / total, w_second / total

    body.vertex_groups.clear()
    groups = {b.name: body.vertex_groups.new(name=b.name) for b in bones}
    # Batch by quantised weight: 34k vertices one call each is slow, a hundred
    # buckets per bone is instant and visually identical.
    for column, weights in ((0, w_near), (1, w_second)):
        buckets = np.round(weights * 100).astype(int)
        for slot, bone in enumerate(bones):
            mine = order[:, column] == slot
            if not mine.any():
                continue
            for bucket in range(1, 101):
                picked = rows[mine & (buckets == bucket)]
                if len(picked):
                    groups[bone.name].add(picked.tolist(), bucket / 100.0, "ADD")

    for mod in list(body.modifiers):
        if mod.type == "ARMATURE":
            body.modifiers.remove(mod)
    body.parent = rig
    body.matrix_parent_inverse = rig.matrix_world.inverted()
    modifier = body.modifiers.new("Armature", "ARMATURE")
    modifier.object = rig
    return len(pts)


def skin(body, rig):
    """Heat-diffusion weights when Blender can solve them, proximity when it
    can't — and it often can't, on meshes nobody modelled by hand."""
    bpy.context.view_layer.update()
    activate(rig, body)
    method = "automatic"
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as exc:  # noqa: BLE001 — whatever went wrong, proximity still works
        emit(log=f"automatic weights failed ({exc})")
        method = "proximity"

    covered = sum(1 for v in body.data.vertices if v.groups)
    if method == "automatic" and covered < 0.85 * len(body.data.vertices):
        emit(log=f"heat weighting only reached {covered:,} of {len(body.data.vertices):,} vertices")
        method = "proximity"

    if method == "proximity":
        covered = skin_by_proximity(body, rig)
    return method, covered


# ── expressions ───────────────────────────────────────────────────────
def _nearest_surface(body, pts, x, z):
    """Where the face's surface actually is at this point on the picture.

    A ray from in front, because "nearest vertex" doesn't mean what it sounds
    like on a bird: everything near an eye is also near the beak, which sticks
    out much further forward, so a nearest-frontmost search puts both eyes on
    the beak. The ray only hits what's genuinely at that spot."""
    depth = float(pts[:, 1].min()) - 1.0
    hit, location, _, _ = body.ray_cast(Vector((x, depth, z)), Vector((0.0, 1.0, 0.0)))
    if hit:
        return np.array([location.x, location.y, location.z])
    d2 = (pts[:, 0] - x) ** 2 + (pts[:, 2] - z) ** 2
    near = np.argsort(d2)[:64]
    picked = pts[near]
    return picked[np.argsort(picked[:, 1])[0]]


def face_points(body, m, spec, opts, projection, height):
    """Where the eyes and mouth are. The user's clicks win; otherwise the
    archetype's rule of thumb, snapped onto the actual surface."""
    pts = coords(body)
    face = spec["face"]
    neck_z = (m.get("neck_z") or 0.82) * height
    head_span = max(height - neck_z, 0.05 * height)
    head_half = max(m["head_w"] * height * 0.5, 0.02 * height)
    chosen = {"source": "estimated"}

    eyes = opts.get("eyes") or []
    mouth = opts.get("mouth")
    if len(eyes) == 2 and projection:
        chosen["source"] = "placed by hand"
        centres = []
        for eye in eyes:
            x, z = unproject(eye["u"], eye["v"], projection)
            centres.append(_nearest_surface(body, pts, x, z))
        # +X is the avatar's own left, so the leftmost click on screen is its
        # RIGHT eye. Sorting here is what keeps blinkLeft winking the eye the
        # tracker means.
        centres.sort(key=lambda c: -c[0])
    else:
        z = neck_z + face["eye_h"] * head_span
        offset = face["eye_out"] * head_half
        # Mirrored aim, but a ray each. Both eyes are looked for at the same
        # height so the blinks stay level, while each lands on its own surface:
        # generated heads are rarely symmetric to the millimetre, and this one
        # is 8 cm deeper on one side than the other.
        centres = [
            _nearest_surface(body, pts, offset, z),
            _nearest_surface(body, pts, -offset, z),
        ]

    if mouth and projection:
        x, z = unproject(mouth["u"], mouth["v"], projection)
        mouth_pt = _nearest_surface(body, pts, x, z)
    else:
        mouth_pt = _nearest_surface(body, pts, 0.0, neck_z + face["mouth_h"] * head_span)

    chosen["eyes"] = [[round(float(c), 5) for c in centre] for centre in centres]
    chosen["mouth"] = [round(float(c), 5) for c in mouth_pt]
    chosen["eye_radius"] = round(float(face["eye_r"] * head_half * 2.0), 5)
    chosen["mouth_radius"] = round(float(face["mouth_r"] * head_half * 2.0), 5)
    return centres, mouth_pt, chosen


def add_shape_keys(body, m, spec, opts, projection, height, defaults):
    """Generate the three shapes the face tracker actually drives: an eye
    closing on each side, and a mouth that opens.

    No mesh arrives with these. Blink squashes the eye and pulls it back into
    the socket — the same move the hand-built penguin uses, which reads as a
    closed eye whether the eye is a painted dot or a glossy ball. The mouth
    hinges the jaw (or the lower beak) down about a line through the corners of
    the mouth, with a soft falloff so the cheeks come along."""
    face = spec["face"]
    centres, mouth_pt, chosen = face_points(body, m, spec, opts, projection, height)
    mesh = body.data
    if not mesh.shape_keys:
        body.shape_key_add(name="Basis", from_mix=False)
    base = coords(body)

    eye_r = face["eye_r"] * max(m["head_w"] * height * 0.5, 0.02 * height) * 2.0
    squash = float(defaults.get("eye_squash", 0.82))
    retract = float(defaults.get("eye_retract", 0.35))
    moved = {}

    for name, centre in (("blink_L", centres[0]), ("blink_R", centres[1])):
        key = body.shape_key_add(name=name, from_mix=False)
        delta = base - np.array(centre)
        dist = np.linalg.norm(delta, axis=1)
        inside = dist < eye_r
        if not inside.any():
            moved[name] = 0
            continue
        fall = np.clip(1.0 - dist[inside] / eye_r, 0.0, 1.0) ** 0.6
        new = base[inside].copy()
        new[:, 2] = centre[2] + (new[:, 2] - centre[2]) * (1.0 - squash * fall)
        new[:, 1] += retract * eye_r * fall          # back into the head
        for local, index in enumerate(np.nonzero(inside)[0]):
            key.data[int(index)].co = Vector(new[local])
        moved[name] = int(inside.sum())

    key = body.shape_key_add(name="mouth_open", from_mix=False)
    mouth_r = face["mouth_r"] * max(m["head_w"] * height * 0.5, 0.02 * height) * 2.0
    angle = math.radians(face["mouth_drop_deg"])
    beak = face["mouth_kind"] == "beak"
    # The hinge sits BEHIND the mouth, where a jaw joint would be. Rotating
    # about the mouth itself gives the lower jaw no lever to swing on, which is
    # how you end up with a shape key that moves 500 vertices by a millimetre
    # and looks like nothing happened.
    hinge = np.array(mouth_pt, dtype=np.float64)
    hinge[1] += (0.85 if beak else 0.55) * mouth_r
    dist = np.linalg.norm(base - np.array(mouth_pt), axis=1)
    # A beak drops its lower half; a jaw takes the whole chin with it.
    inside = (dist < mouth_r) & (base[:, 2] < mouth_pt[2] + 0.10 * mouth_r)
    if beak:
        inside &= base[:, 1] < hinge[1]
    if inside.any():
        fall = np.clip(1.0 - dist[inside] / mouth_r, 0.0, 1.0) ** 0.6
        rel = base[inside] - hinge
        theta = angle * fall
        cos, sin = np.cos(theta), np.sin(theta)
        # Rotate about the left-right axis through the hinge: the chin drops
        # and swings back, exactly as a jaw does.
        out = base[inside].copy()
        out[:, 1] = hinge[1] + rel[:, 1] * cos - rel[:, 2] * sin
        out[:, 2] = hinge[2] + rel[:, 1] * sin + rel[:, 2] * cos
        for local, index in enumerate(np.nonzero(inside)[0]):
            key.data[int(index)].co = Vector(out[local])
    moved["mouth_open"] = int(inside.sum())

    chosen["vertices_moved"] = moved
    # How far the biggest-moving vertex travels, as a share of the model's
    # height. Under about half a percent and nobody will see the expression.
    chosen["travel"] = {}
    for name in ("blink_L", "blink_R", "mouth_open"):
        block = body.data.shape_keys.key_blocks.get(name)
        if block is None:
            continue
        after = np.array([tuple(p.co) for p in block.data], dtype=np.float64)
        chosen["travel"][name] = round(float(np.linalg.norm(after - base, axis=1).max() / height), 4)
    return chosen


# ── VRM ───────────────────────────────────────────────────────────────
def write_vrm_data(rig, body, name, addon_version, with_expressions=True):
    ext = rig.data.vrm_addon_extension
    ext.addon_version = addon_version
    ext.spec_version = "1.0"

    bones = ext.vrm1.humanoid.human_bones
    for blender_name, prop in HUMAN_BONES.items():
        if prop:
            getattr(bones, prop).node.bone_name = blender_name
    for blender_name, prop in VRM_SHOULDERS.items():
        if blender_name in rig.data.bones:
            getattr(bones, prop).node.bone_name = blender_name
    # Let the exporter straighten the rig into a T-pose on the way out: the
    # avatar page drives normalised humanoid bones and poses the arms down
    # itself, which only lines up if the file's rest pose is a T.
    ext.vrm1.humanoid.pose = "autoPose"

    meta = ext.vrm1.meta
    meta.vrm_name = name[:60] or "Avatar"
    meta.version = "1.0"
    meta.authors.clear()
    meta.authors.add().value = "video-stream auto-rig"
    meta.avatar_permission = "onlyAuthor"       # it's the user's own model —
    meta.commercial_usage = "personalNonProfit"  # claim nothing on their behalf
    meta.credit_notation = "unnecessary"
    meta.modification = "allowModificationRedistribution"
    meta.allow_redistribution = False
    meta.allow_excessively_violent_usage = False
    meta.allow_excessively_sexual_usage = False
    meta.allow_political_or_religious_usage = False
    meta.allow_antisocial_or_hate_usage = False

    if not with_expressions:
        return
    keys = body.data.shape_keys.key_blocks if body.data.shape_keys else {}
    preset = ext.vrm1.expressions.preset

    def bind(expression, key_name, weight=1.0):
        if key_name not in keys:
            return
        item = expression.morph_target_binds.add()
        item.node.mesh_object_name = body.name
        item.index = key_name
        item.weight = weight

    bind(preset.blink, "blink_L")
    bind(preset.blink, "blink_R")
    bind(preset.blink_left, "blink_L")
    bind(preset.blink_right, "blink_R")
    # One mouth shape, five vowels: the same weights the hand-built rig uses,
    # so "aa" gapes and "ee" barely parts the lips.
    for vowel, weight in (("aa", 1.0), ("oh", 0.8), ("ou", 0.55), ("ih", 0.4), ("ee", 0.3)):
        bind(getattr(preset, vowel), "mouth_open", weight)


def validate(rig):
    from bl_ext.blender_org.vrm.editor.validation import VrmValidationError, WM_OT_vrm_validator

    bpy.types.Scene.autorig_errors = bpy.props.CollectionProperty(type=VrmValidationError)
    scene = bpy.context.scene
    scene.autorig_errors.clear()
    fatal = WM_OT_vrm_validator.detect_errors(bpy.context, scene.autorig_errors, rig.name)
    messages = [e.message for e in scene.autorig_errors if int(e.severity) < 2]
    return bool(fatal), messages


def export_vrm(rig, body, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    activate(rig, body)
    bpy.ops.export_scene.vrm(
        filepath=path,
        armature_object_name=rig.name,
        export_invisibles=False,
        export_only_selections=False,
        enable_advanced_preferences=False,
        export_all_influences=False,
        export_lights=False,
        export_gltf_animations=False,
        export_try_sparse_sk=False,
        ignore_warning=True,
        check_existing=False,
    )
    return os.path.getsize(path) if os.path.exists(path) else 0


# ── verbs ─────────────────────────────────────────────────────────────
def do_probe():
    version = enable_vrm()
    finish(
        version=bpy.app.version_string,
        vrm_addon=True,
        vrm_addon_version=".".join(str(p) for p in version),
        blender=bpy.app.binary_path,
    )


def do_analyze(opts):
    recipes = load_recipes()
    emit(step="opening the model", progress=0.05)
    clear_scene()
    body = import_source(opts["source"])
    emit(step="simplifying for a quick look", progress=0.25, log=f"{tri_count(body):,} triangles in")
    decimate(body, recipes["defaults"]["target_tris"])
    emit(step="working out which way it faces", progress=0.45)
    facing = orient(body, opts.get("yaw_deg", 0.0), opts.get("face_front", True))
    emit(step="measuring", progress=0.6)
    m = measure(body)
    archetype, confidence, reasons = suggest(m)
    emit(step="rendering previews", progress=0.72)
    setup_render()
    work = Path(opts["work_dir"])
    previews, projection = render_views(body, work, "preview-")
    # A head close-up as well, framed and measured on its own, so the page can
    # offer "click where the eyes are" against a picture big enough to click.
    neck_z = (m.get("neck_z") or 0.80) * m["height"]
    focus = {"z": (neck_z + m["height"]) / 2.0, "scale": max(m["height"] - neck_z, 0.16 * m["height"]) * 1.9}
    head_shot, head_projection = render_views(body, work, "preview-head-", views=("front",), focus=focus)
    previews["head"] = head_shot["front"]
    analysis = {
        "archetype": archetype,
        "confidence": confidence,
        "reasons": reasons,
        "facing": facing,
        "projection": projection,
        "head_projection": head_projection,
        "previews": {k: f"/static/gallery/rig/{work.name}/{Path(v).name}" for k, v in previews.items()},
        "measure": {k: v for k, v in m.items() if k != "bands"},
        "suggested_height_m": recipes["archetypes"][archetype]["height_m"],
    }
    (work / "analysis.json").write_text(json.dumps(analysis, indent=2))
    (work / "bands.json").write_text(json.dumps(m["bands"]))
    emit(step="done", progress=1.0)
    finish(**analysis)


def do_rig(opts):
    recipes = load_recipes()
    work = Path(opts["work_dir"])
    saved = {}
    try:
        saved = json.loads((work / "analysis.json").read_text())
    except (OSError, ValueError):
        pass

    emit(step="starting Blender", progress=0.02)
    addon_version = enable_vrm()
    clear_scene()
    emit(step="opening the model", progress=0.06)
    body = import_source(opts["source"])
    before = tri_count(body)

    target = int(opts.get("target_tris") or 0) or recipes["defaults"]["target_tris"]
    emit(step="simplifying the mesh", progress=0.14, log=f"{before:,} triangles in")
    _, after = decimate(body, target)
    emit(progress=0.24, log=f"{after:,} triangles kept")

    emit(step="facing it the way VRM expects", progress=0.28)
    facing = orient(body, opts.get("yaw_deg", 0.0), opts.get("face_front", True))
    emit(step="measuring the body", progress=0.34)
    m = measure(body)

    archetype = opts.get("archetype") or "auto"
    if archetype == "auto" or archetype not in recipes["archetypes"]:
        archetype = saved.get("archetype") or suggest(m)[0]
    spec = recipes["archetypes"][archetype]
    emit(log=f"rigging as {spec['label']}")

    height_m = float(opts.get("height_m") or 0) or spec["height_m"]
    original_height, scaled_by = rescale(body, height_m)
    m = measure(body)
    height = m["height"]

    emit(step="building the skeleton", progress=0.44)
    plan, mode = plan_bones(m, spec, height)
    rig = build_armature(plan)

    emit(step="binding the mesh to the bones", progress=0.56)
    method, covered = skin(body, rig)
    emit(log=f"{method} weights — {covered:,}/{len(body.data.vertices):,} vertices bound")

    face_info = {}
    if opts.get("expressions", True):
        emit(step="generating blink and mouth shapes", progress=0.68)
        # The head close-up is what the user clicked on, so unproject through
        # ITS camera — the full-body projection would put the eyes somewhere
        # around the navel. And scale it: the analysis measured the model at
        # the size it arrived, while the rig has since resized it to a height
        # in metres, so an unscaled click lands short by exactly that ratio.
        projection = saved.get("head_projection") or saved.get("projection")
        face_info = add_shape_keys(
            body, m, spec, opts, scale_projection(projection, scaled_by), height, recipes["defaults"]
        )
        emit(log="face shapes: " + ", ".join(f"{k} {v}v" for k, v in face_info["vertices_moved"].items()))

    emit(step="writing the VRM humanoid map", progress=0.76)
    write_vrm_data(rig, body, opts.get("name") or "Avatar", addon_version, opts.get("expressions", True))
    fatal, problems = validate(rig)
    if fatal:
        raise RuntimeError("the VRM add-on rejected this rig: " + "; ".join(problems[:4]))

    emit(step="exporting the VRM", progress=0.84)
    out = opts["vrm_out"]
    size = export_vrm(rig, body, out)
    if not size:
        raise RuntimeError("Blender finished but wrote no VRM file")

    emit(step="rendering the result", progress=0.92)
    setup_render()
    shots = {}
    keys = body.data.shape_keys.key_blocks if body.data.shape_keys else {}
    previews, _ = render_views(body, work, "rigged-", views=("front", "side"))
    shots.update(previews)

    # Head close-ups: a blink is a few hundred vertices moving a centimetre, so
    # a full-body shot can't show whether it worked. These three are what the
    # user actually judges the rig by.
    neck_z = (m.get("neck_z") or 0.80) * height
    focus = {"z": (neck_z + height) / 2.0, "scale": max(height - neck_z, 0.16 * height) * 1.9}
    for label, active in (("face", ()), ("blink", ("blink_L", "blink_R")), ("mouth", ("mouth_open",))):
        if label != "face" and not any(k in keys for k in active):
            continue
        for block in keys:
            block.value = 1.0 if block.name in active else 0.0
        bpy.context.view_layer.update()
        more, _ = render_views(body, work, f"rigged-{label}-", views=("front",), focus=focus)
        shots[label] = more["front"]
    for block in keys:
        block.value = 0.0

    emit(step="done", progress=1.0)
    finish(
        archetype=archetype,
        archetype_label=spec["label"],
        tracking=spec.get("tracking", "face"),
        vrm=f"/static/gallery/vrm/{Path(out).name}",
        vrm_path=out,
        bytes=size,
        height_m=round(height, 3),
        source_height=round(original_height, 4),
        tris={"before": before, "after": after},
        bones=len(rig.data.bones),
        weights={"method": method, "covered": covered, "total": len(body.data.vertices)},
        face=face_info,
        facing=facing,
        warnings=problems,
        previews={k: f"/static/gallery/rig/{work.name}/{Path(v).name}" for k, v in shots.items()},
    )


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--verb", required=True, choices=("probe", "analyze", "rig"))
    parser.add_argument("--args", default="")
    args = parser.parse_args(argv)
    try:
        if args.verb == "probe":
            do_probe()
            return
        opts = json.loads(Path(args.args).read_text())
        if args.verb == "analyze":
            do_analyze(opts)
        else:
            do_rig(opts)
    except Exception as exc:  # noqa: BLE001 — the server needs the reason, not a stack trace on stderr
        traceback.print_exc()
        finish(ok=False, error=f"{type(exc).__name__}: {exc}")


main()
