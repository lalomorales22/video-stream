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
             build the archetype's skeleton fitted to the measured landmarks
             (or keep the skeleton and weights the model arrived with, when
             it's a recognisable human one), skin it, cut hanging arms free of
             a body they were welded to, generate blink/mouth/emotion shape
             keys, write the VRM humanoid mapping and expressions, export.
             ``target: "code27"`` adds what a companion device wants on top:
             MToon materials and finger bones.

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
    elif obj.name == "Body" and SOURCE_RIG.get("joints") is not None:
        # The joints of the skeleton the model arrived with ride along with
        # every turn, slide and resize the body gets, so they still sit inside
        # the right limbs when it's time to build bones on them.
        joints = SOURCE_RIG["joints"]
        ones = np.ones((len(joints), 1))
        SOURCE_RIG["joints"] = (np.hstack([joints, ones]) @ np.array(m).T)[:, :3]


# The skeleton a model arrived with, if it had one: joint names, where they
# are (kept in step with the body by bake()), and who each one hangs off.
SOURCE_RIG = {"names": [], "parents": [], "joints": None}


def coords(obj):
    """Every vertex as an (n, 3) array. Local space == world space here: the
    importers' transforms are applied before anything else touches the mesh."""
    mesh = obj.data
    buf = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", buf)
    return buf.reshape(-1, 3)


def surface_points(obj, count=240000, seed=7):
    """Points scattered evenly over the surface, as an (n, 3) array.

    Anything that asks "is there mesh here?" has to ask the surface, not the
    vertices. After simplifying, a flat stretch of jacket is a few big
    triangles with no vertices in the middle, and a thin slice through it
    looks like empty air."""
    mesh = obj.data
    mesh.calc_loop_triangles()
    tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", tris)
    corners = coords(obj)[tris.reshape(-1, 3)]
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    total = float(area.sum())
    if total <= 0:
        return coords(obj)
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(area), size=count, p=area / total)
    r1 = np.sqrt(rng.random(count))[:, None]
    r2 = rng.random(count)[:, None]
    return (1 - r1) * a[pick] + r1 * (1 - r2) * b[pick] + r1 * r2 * c[pick]


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

    # An armature that rode along can't be exported as-is — its bones aren't
    # named or laid out the way VRM wants — but if it's a recognisable human
    # skeleton, its joints and its skin weights are far better than anything
    # we could guess from the silhouette. Note where the joints are, then take
    # it apart; adopt_source_rig() decides later whether they're usable.
    rigs = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    if len(rigs) == 1 and len(rigs[0].data.bones) >= 15:
        source = rigs[0]
        world = source.matrix_world
        SOURCE_RIG["names"] = [b.name for b in source.data.bones]
        SOURCE_RIG["parents"] = [b.parent.name if b.parent else "" for b in source.data.bones]
        SOURCE_RIG["joints"] = np.array(
            [tuple(world @ b.head_local) for b in source.data.bones], dtype=np.float64
        )
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


def face_by_skeleton(body, facing):
    """Let the skeleton a model arrived with settle which way is front.

    The silhouette guess in orient() is a guess: long hair or a backpack looks
    a lot like a face from the outside. A skeleton doesn't guess — its bones
    are labelled left and right. If the left arm has ended up on the avatar's
    right, the model is facing away, so turn it round."""
    joints = SOURCE_RIG.get("joints")
    if joints is None:
        return facing
    sides = {}
    for name, point in zip(SOURCE_RIG["names"], joints):
        ours, side = _classify(name)
        if ours == "upperArm" and side:
            sides.setdefault(side, float(point[0]))
    if len(sides) == 2 and sides["L"] < sides["R"]:
        bake(body, Matrix.Rotation(math.pi, 4, "Z"))
        pts = coords(body)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        mid = (lo + hi) / 2.0
        bake(body, Matrix.Translation((-float(mid[0]), -float(mid[1]), -float(lo[2]))))
        facing = dict(facing)
        facing["turned_deg"] = round((facing.get("turned_deg", 0.0) + 180.0) % 360.0, 1)
        facing["by_skeleton"] = True
    return facing


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


def _x_runs(xs, lo, hi, bins=56):
    """Left-to-right blobs in one horizontal slice: (start, end) in x."""
    counts, edges = np.histogram(xs, bins=bins, range=(lo, hi))
    floor = max(1, int(0.004 * len(xs)))
    runs, start = [], None
    for i, n in enumerate(counts):
        if n > floor:
            if start is None:
                start = i
        elif start is not None:
            runs.append((float(edges[start]), float(edges[i])))
            start = None
    if start is not None:
        runs.append((float(edges[start]), float(edges[-1])))
    return runs


def _hanging_arms(pts, z, height, hip, neck):
    """Find arms that hang beside the body, one side at a time.

    Slice the body between the knees and the shoulders. Wherever a slice shows
    a separate blob outboard of the torso, that blob is a forearm or a hand;
    the lowest such slice is the fingertips. Each side is measured on its own
    because nobody generates a model standing to attention — one hand is
    usually a little further forward than the other.

    Returns ``{"L": {...}, "R": {...}}`` (either side may be missing) with
    everything as a fraction of height, or None when there's nothing to find."""
    lo_x, hi_x = float(pts[:, 0].min()), float(pts[:, 0].max())
    z_lo = max(0.22, (hip or 0.35) * 0.62)
    z_hi = min(0.80, (neck or 0.84) - 0.06)
    slices = 40
    found = {"L": [], "R": []}
    for i in range(slices):
        a = z_lo + (z_hi - z_lo) * i / slices
        b = z_lo + (z_hi - z_lo) * (i + 1) / slices
        sel = pts[(z >= a * height) & (z < b * height)]
        if len(sel) < 40:
            continue
        runs = _x_runs(sel[:, 0], lo_x, hi_x)
        if len(runs) < 2:
            continue
        # The widest blob is the trunk (or the two legs, lower down). Anything
        # clear of it on the outside is arm.
        trunk = max(runs, key=lambda r: r[1] - r[0])
        for side, run in (("L", runs[-1]), ("R", runs[0])):
            if run == trunk:
                continue
            outside = run[0] >= trunk[1] if side == "L" else run[1] <= trunk[0]
            width = (run[1] - run[0]) / height
            # A leg is a blob too. Legs sit under the body; arms sit beside it.
            centre = (run[0] + run[1]) / 2.0
            if not outside or width > 0.14 or abs(centre) / height < 0.085:
                continue
            blob = sel[(sel[:, 0] >= run[0]) & (sel[:, 0] <= run[1])]
            if len(blob) < 12:
                continue
            found[side].append(
                {
                    "z0": a,
                    "z1": b,
                    "x": float(np.abs(blob[:, 0]).mean()) / height,
                    "y": float(blob[:, 1].mean()) / height,
                    "w": width,
                    "d": float(blob[:, 1].max() - blob[:, 1].min()) / height,
                }
            )
    out = {}
    for side, hits in found.items():
        if len(hits) < 3:
            continue
        # Keep the lowest unbroken stack of slices: a pocket flap or a holster
        # higher up is not part of the arm that ends in this hand.
        step = (z_hi - z_lo) / slices
        stack = [hits[0]]
        for hit in hits[1:]:
            if hit["z0"] - stack[-1]["z1"] > 1.5 * step:
                break
            stack.append(hit)
        if len(stack) < 3 or stack[-1]["z1"] - stack[0]["z0"] < 0.06:
            continue
        tip = stack[:2]
        out[side] = {
            "out": round(float(np.mean([h["x"] for h in tip])), 4),
            "fb": round(float(np.mean([h["y"] for h in tip])), 4),
            "z": round(stack[0]["z0"], 4),
            "free_to_z": round(stack[-1]["z1"], 4),
            "w": round(float(np.median([h["w"] for h in stack])), 4),
            "d": round(float(np.median([h["d"] for h in stack])), 4),
        }
    return out or None


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

    # Arms hanging at the sides never reach "meaningfully past the torso" —
    # they ARE the torso's silhouette — so the test above misses the single
    # most common pose a generated person arrives in. Look for them directly.
    arm_hang = None
    if not arm:
        dense = surface_points(body)
        arm_hang = _hanging_arms(dense, dense[:, 2] - lo[2], height, hip, neck)

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
        "arm_hang": arm_hang,
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


def render_views(body, out_dir, prefix, views=("front", "side"), focus=None, posed=False):
    """Ortho renders, plus the numbers needed to turn a click on the picture
    back into a point in the model. ``focus`` frames the head instead of the
    whole body — that's the only shot in which a blink is visible. ``posed``
    frames the mesh as the armature currently holds it rather than at rest."""
    pts = coords(body)
    if posed:
        evaluated = body.evaluated_get(bpy.context.evaluated_depsgraph_get())
        world = np.array(evaluated.matrix_world)
        buf = np.empty(len(evaluated.data.vertices) * 3, dtype=np.float64)
        evaluated.data.vertices.foreach_get("co", buf)
        local = buf.reshape(-1, 3)
        pts = (np.hstack([local, np.ones((len(local), 1))]) @ world.T)[:, :3]
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    mid = (lo + hi) / 2.0
    height = float(hi[2] - lo[2]) or 1.0
    scale = max(height, float(hi[0] - lo[0]) / (bpy.context.scene.render.resolution_x / bpy.context.scene.render.resolution_y)) * 1.12
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
# Bones only some rigs have: an adopted skeleton's extra spine joint, and the
# fingers added for full-body animation targets.
FINGERS = ("thumb", "index", "middle", "ring", "little")
VRM_OPTIONAL = {"upperChest": "upper_chest"}
for _side, _word in (("L", "left"), ("R", "right")):
    for _finger in FINGERS:
        _joints = ("metacarpal", "proximal", "distal") if _finger == "thumb" else ("proximal", "intermediate", "distal")
        for _joint in _joints:
            VRM_OPTIONAL[f"{_finger}_{_joint}.{_side}"] = f"{_word}_{_finger}_{_joint}"


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
    hang = (m.get("arm_hang") or {}) if (arms_on and not m.get("arm")) else {}
    for side, sign in (("L", 1.0), ("R", -1.0)):
        root = P(sign * root_out, 0, shoulder_z)
        tip = P(sign * tip_out, tip_fb, tip_z)
        down = hang.get(side) or hang.get("R" if side == "L" else "L")
        if down and down["z"] * H < shoulder_z - 0.12 * H:
            # An arm that hangs: the collar bone runs OUT to the shoulder joint
            # and the arm drops from there. One straight line from the chest to
            # the fingertips would cut through the ribs and bind half the torso
            # to the forearm.
            tip = P(sign * down["out"] * H, down["fb"] * H, down["z"] * H)
            joint = P(
                sign * max(0.86 * down["out"] * H, 1.6 * root_out),
                0.35 * down["fb"] * H,
                shoulder_z - 0.012 * H,
            )
            lead = 0.16
            first, second = ((f - lead) / (1.0 - lead) for f in A["split"])
            bones += [
                (f"shoulder.{side}", root, joint, "chest", arms_on),
                (f"upperArm.{side}", joint, joint.lerp(tip, first), f"shoulder.{side}", arms_on),
                (f"lowerArm.{side}", joint.lerp(tip, first), joint.lerp(tip, second), f"upperArm.{side}", arms_on),
                (f"hand.{side}", joint.lerp(tip, second), tip, f"lowerArm.{side}", arms_on),
            ]
            continue

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


# Source-skeleton bone names → ours. Matched on the name with the side and
# every non-letter stripped, so "L_Upperarm", "mixamorig:LeftArm" and
# "upper_arm.L" all land in the same place. Twist bones fold into the limb
# segment they ride on, which is all VRM has room for.
_ADOPT = {
    "hips": ("hip", "hips", "pelvis", "root"),
    "spine": ("waist", "spine", "spine00"),
    "chest": ("spine01", "spine1", "chest"),
    "upperChest": ("spine02", "spine2", "spine03", "spine3", "upperchest"),
    "neck": ("neck", "neck01", "necktwist01", "necktwist02"),
    "head": ("head",),
    "shoulder": ("clavicle", "shoulder", "collar"),
    "upperArm": ("upperarm", "arm", "upperarmtwist01", "upperarmtwist02", "armtwist"),
    "lowerArm": ("forearm", "lowerarm", "forearmtwist01", "forearmtwist02", "forearmtwist"),
    "hand": ("hand",),
    "upperLeg": ("thigh", "upleg", "upperleg", "thightwist01", "thightwist02"),
    "lowerLeg": ("calf", "leg", "lowerleg", "shin", "calftwist01", "calftwist02"),
    "foot": ("foot", "toe", "toebase", "ball"),
}
_ADOPT_LOOKUP = {alias: ours for ours, aliases in _ADOPT.items() for alias in aliases}
_SIDED = ("shoulder", "upperArm", "lowerArm", "hand", "upperLeg", "lowerLeg", "foot")
_REQUIRED_TO_ADOPT = (
    ["hips", "spine", "head"]
    + [f"{part}.{side}" for part in _SIDED if part != "shoulder" for side in "LR"]
)


_FINGER_WORDS = {"thumb": "thumb", "index": "index", "middle": "middle", "ring": "ring", "pinky": "little", "little": "little"}
_FINGER_JOINTS = {"metacarpal": 0, "proximal": 1, "intermediate": 2, "distal": 3}


def _classify_finger(key):
    """"handindex2" / "indexintermediate" → "index_intermediate"; a fingertip
    end bone ("handindex4") → "index_tip". None if it isn't a finger."""
    for word, finger in _FINGER_WORDS.items():
        if word not in key:
            continue
        rest = key.split(word, 1)[1]
        order = ("metacarpal", "proximal", "distal") if finger == "thumb" else ("proximal", "intermediate", "distal")
        for joint in order:
            if rest.startswith(joint):
                return f"{finger}_{joint}"
        digits = "".join(c for c in rest if c.isdigit())
        if digits:
            number = int(digits[:2])
            if 1 <= number <= 3:
                return f"{finger}_{order[number - 1]}"
            if number == 4:
                return f"{finger}_tip"
        return None
    return None


def _classify(name):
    """A source bone name → (our bone, side) or (None, "")."""
    raw = name.split(":")[-1]
    low = raw.lower()
    side = ""
    for marker, which in (("left", "L"), ("right", "R")):
        if marker in low:
            side, low = which, low.replace(marker, "")
    if not side:
        parts = [t for t in raw.replace(".", "_").replace("-", "_").split("_") if t]
        if parts and parts[0].lower() in ("l", "r"):
            side, low = parts[0].upper(), "".join(parts[1:]).lower()
        elif parts and parts[-1].lower() in ("l", "r"):
            side, low = parts[-1].upper(), "".join(parts[:-1]).lower()
    key = "".join(c for c in low if c.isalnum())
    ours = _ADOPT_LOOKUP.get(key)
    if ours is None and side:
        ours = _classify_finger(key)
        if ours:
            return ours, side
    if ours is None:
        return None, ""
    if ours in _SIDED:
        return (ours, side) if side else (None, "")
    return ours, ""


def source_rig_is_human():
    """Did the model arrive with a skeleton we can map onto VRM's?"""
    if SOURCE_RIG.get("joints") is None:
        return False
    found = set()
    for name in SOURCE_RIG["names"]:
        ours, side = _classify(name)
        if ours:
            found.add(f"{ours}.{side}" if side else ours)
    return all(key in found for key in _REQUIRED_TO_ADOPT)


def adopt_source_rig(body, height, keep_fingers=False):
    """Turn the skeleton the model arrived with into a VRM-shaped plan.

    Returns ``(plan, weight_map)`` or ``None`` when there was no skeleton, it
    isn't recognisably human, or the mesh isn't really bound to it. The plan
    has the same shape plan_bones() returns; ``weight_map`` says which of the
    mesh's existing vertex groups feed each of our bones."""
    joints = SOURCE_RIG.get("joints")
    if joints is None:
        return None
    where, weight_map, keyed = {}, {}, {}
    for name, point in zip(SOURCE_RIG["names"], joints):
        ours, side = _classify(name)
        if ours is None:
            continue
        key = f"{ours}.{side}" if side else ours
        keyed[name] = key
        # First hit wins: skeletons list a limb before its twist bones, and
        # "Hip" before "Pelvis" — the first one is the joint itself.
        where.setdefault(key, Vector(point))
    if any(key not in where for key in _REQUIRED_TO_ADOPT):
        return None

    # Fingers come across only as complete sets, and only when asked for: three
    # joints each, both hands. Anything less folds back into the hand.
    finger_keys = [
        f"{finger}_{joint}.{side}"
        for side in "LR"
        for finger in FINGERS
        for joint in (("metacarpal", "proximal", "distal") if finger == "thumb" else ("proximal", "intermediate", "distal"))
    ]
    fingers_ok = keep_fingers and all(key in where for key in finger_keys)

    def is_finger(key):
        return key.split(".")[0].split("_")[0] in FINGERS

    parent_of = dict(zip(SOURCE_RIG["names"], SOURCE_RIG["parents"]))
    for name in SOURCE_RIG["names"]:
        # Every source bone's weight has to land somewhere. One we have no
        # name for (a jaw, an eyelid, a toe) gives it to the nearest ancestor
        # we do — otherwise that part of the mesh would be left behind.
        probe, key = name, None
        for _ in range(64):
            key = keyed.get(probe)
            if key and is_finger(key) and not (fingers_ok and not key.split(".")[0].endswith("_tip")):
                # A finger we're not keeping, or a fingertip end bone: carry
                # on up (tip → distal, or the whole finger → hand).
                key = None
            if key or not parent_of.get(probe):
                break
            probe = parent_of[probe]
        if key:
            weight_map.setdefault(key, []).append(name)

    # Which side is which comes from where the bones ARE, not what they're
    # called: +X is the avatar's left once the body has been turned to face
    # front, and a skeleton authored facing the other way has them swapped.
    if where["upperArm.L"].x < where["upperArm.R"].x:
        def flip(key):
            return key[:-1] + ("R" if key.endswith("L") else "L") if key[-2:] in (".L", ".R") else key
        where = {flip(k): v for k, v in where.items()}
        weight_map = {flip(k): v for k, v in weight_map.items()}

    # The mesh has to actually be bound to these bones.
    groups = {g.name: g.index for g in body.vertex_groups}
    used = {groups[n] for names in weight_map.values() for n in names if n in groups}
    bound = sum(1 for v in body.data.vertices if any(g.group in used and g.weight > 1e-4 for g in v.groups))
    if bound < 0.85 * len(body.data.vertices):
        return None

    H = height
    up = Vector((0.0, 0.0, 1.0))
    plan = []
    trunk = [k for k in ("hips", "spine", "chest", "upperChest", "neck", "head") if k in where]
    for i, key in enumerate(trunk):
        head = where[key]
        if key == "head":
            tail = head + up * max(0.11 * H, 0.5 * (H - head.z))
        else:
            tail = where[trunk[i + 1]]
        plan.append((key, head, tail, trunk[i - 1] if i else None, True))
    arm_root = next(k for k in ("upperChest", "chest", "spine") if k in where)
    for side in "LR":
        chain = [k for k in (f"shoulder.{side}", f"upperArm.{side}", f"lowerArm.{side}", f"hand.{side}") if k in where]
        for i, key in enumerate(chain):
            head = where[key]
            if key.startswith("hand"):
                fore = head - where[f"lowerArm.{side}"]
                tail = head + fore.normalized() * max(0.045 * H, 0.38 * fore.length)
            else:
                tail = where[chain[i + 1]]
            plan.append((key, head, tail, chain[i - 1] if i else arm_root, True))
        if fingers_ok:
            for finger in FINGERS:
                order = ("metacarpal", "proximal", "distal") if finger == "thumb" else ("proximal", "intermediate", "distal")
                chain = [f"{finger}_{joint}.{side}" for joint in order]
                for i, key in enumerate(chain):
                    head = where[key]
                    if i + 1 < len(chain):
                        tail = where[chain[i + 1]]
                    elif f"{finger}_tip.{side}" in where:
                        tail = where[f"{finger}_tip.{side}"]
                    else:
                        tail = head + (head - where[chain[i - 1]]) * 0.8
                    plan.append((key, head, tail, chain[i - 1] if i else f"hand.{side}", True))
        chain = [f"upperLeg.{side}", f"lowerLeg.{side}", f"foot.{side}"]
        for i, key in enumerate(chain):
            head = where[key]
            if key.startswith("foot"):
                tail = Vector((head.x, head.y - 0.09 * H, min(head.z, 0.02 * H)))
            else:
                tail = where[chain[i + 1]]
            plan.append((key, head, tail, chain[i - 1] if i else "hips", True))
    return plan, weight_map


def skin_from_source(body, rig, weight_map):
    """Re-label the weights the model arrived with onto our bones.

    Every source bone that feeds one of ours (a forearm and its two twist
    bones, say) has its weight summed into that bone's group. Nothing is
    re-solved, so the deformation is exactly what the model's author — or the
    generator's own rigger — painted."""
    old_index = {g.index: g.name for g in body.vertex_groups}
    target = {src: ours for ours, sources in weight_map.items() for src in sources}
    merged = {}          # our bone → {vertex: weight}
    for v in body.data.vertices:
        for g in v.groups:
            ours = target.get(old_index.get(g.group))
            if ours and g.weight > 0.0:
                slot = merged.setdefault(ours, {})
                slot[v.index] = slot.get(v.index, 0.0) + g.weight
    body.vertex_groups.clear()
    covered = set()
    for ours, weights in merged.items():
        if ours not in rig.data.bones:
            continue
        group = body.vertex_groups.new(name=ours)
        buckets = {}
        for index, weight in weights.items():
            buckets.setdefault(round(min(weight, 1.0), 3), []).append(index)
        for weight, indices in buckets.items():
            if weight > 0.0:
                group.add(indices, weight, "REPLACE")
        covered.update(weights)
    # Anything the source left unbound (stray loose geometry) follows the hips
    # rather than being nailed to the origin.
    loose = [v.index for v in body.data.vertices if v.index not in covered]
    if loose:
        hips = body.vertex_groups.get("hips") or body.vertex_groups.new(name="hips")
        hips.add(loose, 1.0, "REPLACE")
    for mod in list(body.modifiers):
        if mod.type == "ARMATURE":
            body.modifiers.remove(mod)
    body.parent = rig
    body.matrix_parent_inverse = rig.matrix_world.inverted()
    modifier = body.modifiers.new("Armature", "ARMATURE")
    modifier.object = rig
    return len(covered)


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
    # Groups left over from a skeleton we threw away would survive a failed
    # heat solve looking exactly like a successful one.
    body.vertex_groups.clear()
    bpy.context.view_layer.update()
    activate(rig, body)
    method = "automatic"
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as exc:  # noqa: BLE001 — whatever went wrong, proximity still works
        emit(log=f"automatic weights failed ({exc})")
        method = "proximity"

    # "Has a group" is not "is bound". When heat weighting can't solve, Blender
    # still creates every group and assigns every vertex to it — at weight
    # zero. That counts as fully covered, exports as a mesh nailed to a single
    # neutral bone, and looks perfect until something tries to move it.
    covered = sum(1 for v in body.data.vertices if sum(g.weight for g in v.groups) > 1e-4)
    if method == "automatic" and covered < 0.85 * len(body.data.vertices):
        emit(log=f"heat weighting only reached {covered:,} of {len(body.data.vertices):,} vertices")
        method = "proximity"

    if method == "proximity":
        covered = skin_by_proximity(body, rig)
    return method, covered


def _arm_side_split(pts, dense, rig, side, height, trusted=None):
    """Which vertices belong to a hanging arm, and which to the body beside it.

    Walk down the arm in thin slices. Where there's daylight between the arm
    and the trunk, everything outboard of the gap is arm — measured, not
    guessed. Where there isn't (a hand on a hip, a baggy sleeve against the
    ribs) the arm is taken to be what lies within its own radius of the bone,
    and that radius is read off the one side of an arm that's always visible:
    the outside. ``trusted`` marks vertices already known to be forearm or
    hand (the weights a model arrived with say so), which settles the fingers
    that curl away from the bone.

    Returns boolean masks ``(arm, beside)`` over the vertices, or None if this
    arm doesn't hang or was never once clear of the body."""
    bones = rig.data.bones
    try:
        chain = [bones[f"upperArm.{side}"], bones[f"lowerArm.{side}"], bones[f"hand.{side}"]]
    except KeyError:
        return None
    sign = 1.0 if side == "L" else -1.0
    knots = [np.array(chain[0].head_local)] + [np.array(b.tail_local) for b in chain]
    top, wrist, tip = knots[0], knots[2], knots[3]
    length = sum(float(np.linalg.norm(knots[i + 1] - knots[i])) for i in range(3))
    if top[2] - tip[2] < 0.85 * length:
        # Held away from the body (an A-pose, a T-pose, a wave): there's air
        # between arm and trunk already, and slicing a slanted arm level would
        # only shave strips off the jacket.
        return None

    zs = np.array([k[2] for k in knots])[::-1]
    if np.any(np.diff(zs) <= 0):
        return None          # an arm that doubles back on itself: leave it be
    xs = np.array([k[0] * sign for k in knots])[::-1]
    ys = np.array([k[1] for k in knots])[::-1]

    z_top = top[2] - 0.05 * height
    z_bot = tip[2] - 0.03 * height
    step = 0.008 * height
    count = max(4, int((z_top - z_bot) / step))
    sx = pts[:, 0] * sign
    dx = dense[:, 0] * sign            # the same, for the surface samples
    reach = float(dx.max())
    slices = []
    for i in range(count):
        a, b = z_bot + i * step, z_bot + (i + 1) * step
        mid = (a + b) / 2.0
        cx, cy = float(np.interp(mid, zs, xs)), float(np.interp(mid, zs, ys))
        band = (pts[:, 2] >= a) & (pts[:, 2] < b) & (sx > 0)
        seen = (dense[:, 2] >= a) & (dense[:, 2] < b) & (dx > 0)
        edge = None
        if int(seen.sum()) >= 40:
            runs = _x_runs(dx[seen], 0.0, reach, bins=64)
            mine = [r for r in runs if r[0] - 0.01 * height <= cx <= r[1] + 0.01 * height]
            # "Clear of the body" means the blob around the bone is arm-sized.
            # A hand lying on a thigh is one blob with the leg — that's a weld,
            # however much daylight there is between the two legs.
            if mine and mine[0][0] > 0.03 * height and mine[0][1] - mine[0][0] <= 0.12 * height:
                edge = mine[0][0]
        slices.append((band, mid, cx, cy, edge, seen))
    offsets = [cx - edge for _, _, cx, _, edge, _ in slices if edge is not None]
    if len(offsets) < 3:
        return None
    typical = float(np.median(offsets))
    # Only worth cutting if the forearm or hand is actually stuck to something.
    # Above the elbow, arm meeting trunk is just an armpit.
    elbow_z = knots[1][2]
    stuck = [1 for _, mid, _, _, edge, seen in slices if edge is None and mid < elbow_z and int(seen.sum()) >= 40]
    if len(stuck) < 2:
        return None

    arm = np.zeros(len(pts), dtype=bool)
    for band, mid, cx, cy, edge, seen in slices:
        if not band.any():
            continue
        if edge is not None:
            arm |= band & (sx >= edge)
            continue
        # Welded here. The outside of the arm is still its own, so that's
        # where its radius comes from.
        beside_bone = seen & (np.abs(dense[:, 1] - cy) < 1.5 * typical)
        outer = float(dx[beside_bone].max()) if beside_bone.any() else cx + typical
        radius = float(np.clip(1.08 * (outer - cx), 0.75 * typical, 2.2 * typical))
        if mid < wrist[2]:
            radius = max(radius, 1.25 * typical)     # a hand is wider than a wrist
        flat = np.hypot(sx - cx, pts[:, 1] - cy)
        # Outboard of the bone there is nothing but arm, however baggy the
        # sleeve. The radius only has to decide the side facing the body.
        outboard = (sx >= cx) & (np.abs(pts[:, 1] - cy) < 1.6 * radius)
        arm |= band & ((flat <= radius) | outboard)
    in_range = (sx > 0) & (pts[:, 2] < z_top) & (pts[:, 2] >= z_bot)
    if trusted is not None:
        arm |= trusted & in_range
    # Everything else on this side below the armpit is "beside the arm" —
    # including the thigh under the fingertips, or the faces that reach down
    # from a fingertip to the leg below it would survive the cut.
    beside = (sx > 0) & (pts[:, 2] < z_top) & ~arm
    return arm, beside


def _settle(labels, edges, rounds=3):
    """Tidy a two-way labelling of the mesh: a vertex mostly surrounded by the
    other side joins it. Stops the cut from being a ragged line with islands
    either side — every island is a hole once the two halves move apart."""
    a, b = edges[:, 0], edges[:, 1]
    for _ in range(rounds):
        arm_n = np.bincount(a, weights=(labels[b] == 1), minlength=len(labels)) + np.bincount(
            b, weights=(labels[a] == 1), minlength=len(labels)
        )
        rest_n = np.bincount(a, weights=(labels[b] == 2), minlength=len(labels)) + np.bincount(
            b, weights=(labels[a] == 2), minlength=len(labels)
        )
        flip_to_arm = (labels == 2) & (arm_n >= 2.5 * np.maximum(rest_n, 0.4))
        flip_to_rest = (labels == 1) & (rest_n >= 2.5 * np.maximum(arm_n, 0.4))
        if not (flip_to_arm.any() or flip_to_rest.any()):
            break
        labels = labels.copy()
        labels[flip_to_arm] = 1
        labels[flip_to_rest] = 2
    return labels


def separate_fused_arms(body, rig, height, trust_weights=False):
    """Cut a hanging arm free where the mesh has welded it to the body.

    Generators make one watertight skin, so a hand resting on a hip is
    literally part of the hip: faces run straight from palm to pocket, and
    whoever weighted the model split those vertices between the two. Nothing
    shows while the arms hang. Lift them — which every VRM does on export,
    because the format's rest pose is a T — and that skin stretches into a web
    from wrist to thigh.

    So each arm is found geometrically (see _arm_side_split), its vertices are
    given to the arm's bones alone and the body's to the body alone, the faces
    left spanning the two are removed and the holes capped. Returns
    ``(faces_cut, vertices_reassigned)``."""
    import bmesh

    mesh = body.data
    pts = coords(body)
    owner = np.zeros(len(pts), dtype=np.int8)    # 0 untouched, 1/3 an arm, 2 beside one
    moved = 0
    dense = surface_points(body)
    edge_buf = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", edge_buf)
    edge_pairs = edge_buf.reshape(-1, 2)
    for side, tag in (("L", 1), ("R", 3)):
        # Vertices the existing weights already call forearm or hand. Only
        # meaningful for a skeleton the model arrived with — weights we just
        # solved ourselves are the thing being corrected.
        trusted = None
        if trust_weights:
            trusted = np.zeros(len(pts), dtype=bool)
            mine = {body.vertex_groups[n].index for n in (f"lowerArm.{side}", f"hand.{side}") if n in body.vertex_groups}
            for v in mesh.vertices:
                if sum(g.weight for g in v.groups if g.group in mine) >= 0.5:
                    trusted[v.index] = True
        split = _arm_side_split(pts, dense, rig, side, height, trusted)
        if split is None:
            continue
        arm, beside = split
        labels = np.zeros(len(pts), dtype=np.int8)
        labels[arm] = 1
        labels[beside] = 2
        labels = _settle(labels, edge_pairs)
        arm, beside = labels == 1, labels == 2
        chain = [n for n in (f"upperArm.{side}", f"lowerArm.{side}", f"hand.{side}") if n in rig.data.bones]
        chain_ids = {body.vertex_groups[n].index for n in chain if n in body.vertex_groups}
        for n in chain:
            if n not in body.vertex_groups:
                body.vertex_groups.new(name=n)
        segments = [(n, np.array(rig.data.bones[n].head_local), np.array(rig.data.bones[n].tail_local)) for n in chain]
        dists = np.stack([_segment_distance(pts, h, t) for _, h, t in segments], axis=1)
        fallback = [n for n in ("hips", f"upperLeg.{side}", "spine", "chest", "upperChest") if n in rig.data.bones]
        fb_dists = np.stack(
            [_segment_distance(pts, np.array(rig.data.bones[n].head_local), np.array(rig.data.bones[n].tail_local)) for n in fallback],
            axis=1,
        )
        for index in np.nonzero(arm)[0]:
            v = mesh.vertices[int(index)]
            keep = [(g.group, g.weight) for g in v.groups if g.group in chain_ids and g.weight > 0]
            foreign = [g.group for g in v.groups if g.group not in chain_ids]
            trusted = sum(w for _, w in keep)
            if not foreign and trusted > 0:
                continue
            for group in [g.group for g in v.groups]:
                body.vertex_groups[group].remove([v.index])
            if trusted >= 0.5:
                for group, weight in keep:
                    body.vertex_groups[group].add([v.index], weight / trusted, "REPLACE")
            else:
                # The source gave this to the body. Share it between the two
                # nearest arm bones so the wrist and elbow still bend smoothly.
                order = np.argsort(dists[index])[:2]
                near = dists[index][order] + 1e-4
                share = 1.0 / near ** 2
                if near[1] > 2.5 * near[0]:
                    share[1] = 0.0
                share /= share.sum()
                for slot, weight in zip(order, share):
                    if weight > 0.01:
                        body.vertex_groups[segments[int(slot)][0]].add([v.index], float(weight), "REPLACE")
            moved += 1
        for index in np.nonzero(beside)[0]:
            v = mesh.vertices[int(index)]
            if not any(g.group in chain_ids and g.weight > 0 for g in v.groups):
                continue
            keep = [(g.group, g.weight) for g in v.groups if g.group not in chain_ids and g.weight > 0]
            for group in [g.group for g in v.groups]:
                body.vertex_groups[group].remove([v.index])
            kept = sum(w for _, w in keep)
            if kept > 0.05:
                for group, weight in keep:
                    body.vertex_groups[group].add([v.index], weight / kept, "REPLACE")
            elif fallback:
                body.vertex_groups[fallback[int(np.argmin(fb_dists[index]))]].add([v.index], 1.0, "REPLACE")
            moved += 1
        owner[arm] = tag
        owner[beside & (owner == 0)] = 2

    if not owner.any():
        return 0, 0
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    doomed = [
        face for face in bm.faces
        if len({int(owner[v.index]) for v in face.verts} - {0}) > 1
    ]
    cut = len(doomed)
    if cut:
        rim = {e for f in doomed for e in f.edges}
        bmesh.ops.delete(bm, geom=doomed, context="FACES_ONLY")
        open_edges = [e for e in rim if e.is_valid and e.is_boundary]
        if open_edges:
            filled = bmesh.ops.holes_fill(bm, edges=open_edges, sides=0)
            # A cap must never re-join what was just separated.
            again = [
                f for f in filled.get("faces", [])
                if len({int(owner[v.index]) for v in f.verts} - {0}) > 1
            ]
            if again:
                bmesh.ops.delete(bm, geom=again, context="FACES_ONLY")
            caps = [f for f in filled.get("faces", []) if f.is_valid]
            if caps:
                bmesh.ops.triangulate(bm, faces=caps)
        bm.to_mesh(mesh)
        mesh.update()
    bm.free()
    return cut, moved


# ── fingers ───────────────────────────────────────────────────────────
# How long each finger is next to the middle one, and where it sits across the
# palm (thumb side positive), as a share of the palm's width.
_FINGER_LAYOUT = {
    "index": (0.92, 0.33),
    "middle": (1.00, 0.11),
    "ring": (0.92, -0.11),
    "little": (0.75, -0.33),
}
_PALM = 0.45            # knuckles sit this far from wrist to fingertip
_PHALANGES = (0.45, 0.30, 0.25)


def _hand_frame(body, rig, side, height):
    """Measure one hand: which way it points, which way is across the palm,
    and how big it is. Returns a dict, or a string saying why it can't."""
    name = f"hand.{side}"
    group = body.vertex_groups.get(name)
    if group is None or name not in rig.data.bones:
        return "no hand bone"
    pts = coords(body)
    weight = np.zeros(len(pts))
    for v in body.data.vertices:
        for g in v.groups:
            if g.group == group.index:
                weight[v.index] = g.weight
    mine = weight >= 0.5
    if int(mine.sum()) < 120:
        return f"only {int(mine.sum())} vertices in the hand — too coarse to tell fingers apart"
    bone = rig.data.bones[name]
    wrist = np.array(bone.head_local)
    along = np.array(bone.tail_local) - wrist
    along /= np.linalg.norm(along) or 1.0
    rel = pts[mine] - wrist
    t = rel @ along
    length = float(np.percentile(t, 98))
    if length < 0.085 * height:
        # An open hand is about a tenth of a person's height, wrist to
        # fingertip. Much less and the fingers are curled into a fist or fused
        # into a stub: there's nothing straight to lay finger bones along, and
        # bending them would only crumple the knuckles.
        return (
            f"the hands are closed or stubby ({length / height:.0%} of the height, wrist to tip; "
            "an open hand is about 10%), so there are no straight fingers to put bones in"
        )
    flat = rel - np.outer(t, along)
    # Across the palm is the direction the hand is widest in, once "along the
    # fingers" is taken out. A hand is a slab; if it measures as a tube there's
    # no telling palm from edge and the fingers would be laid out at random.
    values, vectors = np.linalg.eigh(np.cov(flat.T))
    across = vectors[:, int(np.argmax(values))]
    across -= (across @ along) * along
    across /= np.linalg.norm(across) or 1.0
    u = flat @ across
    width = float(np.percentile(u, 97) - np.percentile(u, 3))
    normal = np.cross(along, across)
    thick = flat @ normal
    thickness = float(np.percentile(thick, 97) - np.percentile(thick, 3))
    if width < 0.02 * height or width < 1.25 * max(thickness, 1e-6):
        return "the hand reads as a tube, not a palm — can't tell which way the fingers lie"
    # Thumb side. Palms facing the body or the floor put the thumb in FRONT
    # (-Y); a palm facing forwards or back puts it toward the body's centre
    # line, which is where a resting hand's thumb sits.
    sign = 1.0 if side == "L" else -1.0
    if abs(across[1]) >= 0.4:
        thumb = -1.0 if across[1] > 0 else 1.0
    else:
        thumb = -1.0 if across[0] * sign > 0 else 1.0
    across = across * thumb
    centre = float(np.median(flat @ across))
    return {
        "wrist": wrist, "along": along, "across": across, "length": length,
        "width": width, "centre": centre, "weight": weight, "group": group,
        "vertices": int(mine.sum()),
    }


def add_fingers(body, rig, height):
    """Give each hand a thumb and four fingers, and share the hand's existing
    weight out among them.

    Returns ``(bones_added, note)``. Nothing outside the hands changes: only
    weight that already belonged to a hand bone is moved, so with the fingers
    at rest the mesh sits exactly where it did. Whether they then bend
    *convincingly* depends on the model — fingers sculpted as one mitten curl
    as a mitten."""
    frames, reasons = {}, []
    for side in "LR":
        frame = _hand_frame(body, rig, side, height)
        if isinstance(frame, str):
            reasons.append(frame)
        else:
            frames[side] = frame
    if len(frames) < 2:
        return 0, "No finger bones: " + (reasons[0] if reasons else "couldn't find the hands") + "."

    plan = {}                 # bone name → (head, tail, parent)
    chains = {}               # side → finger → [(bone, start, end)] along the finger
    for side, f in frames.items():
        wrist, along, across = f["wrist"], f["along"], f["across"]
        L, W = f["length"], f["width"]
        origin = wrist + across * f["centre"]
        chains[side] = {}
        for finger, (scale, offset) in _FINGER_LAYOUT.items():
            reach = (1.0 - _PALM) * L * scale
            at = origin + along * (_PALM * L) + across * (offset * W)
            parent, start = f"hand.{side}", 0.0
            chain = []
            for joint, share in zip(("proximal", "intermediate", "distal"), _PHALANGES):
                name = f"{finger}_{joint}.{side}"
                end = start + share
                plan[name] = (Vector(at + along * (start * reach)), Vector(at + along * (end * reach)), parent)
                chain.append((name, start, end))
                parent, start = name, end
            chains[side][finger] = (at, along, reach, chain)
        # The thumb leaves the heel of the hand at an angle.
        aim = 0.6 * along + 0.8 * across
        aim /= np.linalg.norm(aim)
        reach = 0.5 * L
        at = origin + along * (0.10 * L) + across * (0.25 * W)
        parent, start, chain = f"hand.{side}", 0.0, []
        for joint, share in zip(("metacarpal", "proximal", "distal"), (0.40, 0.33, 0.27)):
            name = f"thumb_{joint}.{side}"
            end = start + share
            plan[name] = (Vector(at + aim * (start * reach)), Vector(at + aim * (end * reach)), parent)
            chain.append((name, start, end))
            parent, start = name, end
        chains[side]["thumb"] = (at, aim, reach, chain)

    activate(rig)
    bpy.ops.object.mode_set(mode="EDIT")
    for name, (head, tail, parent) in plan.items():
        bone = rig.data.edit_bones.new(name)
        bone.head, bone.tail = head, tail
        bone.roll = 0.0
        bone.use_connect = False
        bone.use_deform = True
        bone.parent = rig.data.edit_bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")

    pts = coords(body)
    for side, f in frames.items():
        group, weight = f["group"], f["weight"]
        wrist, along, across = f["wrist"], f["along"], f["across"]
        L, W = f["length"], f["width"]
        new_groups = {name: body.vertex_groups.new(name=name) for name in plan if name.endswith(f".{side}")}
        rel = pts - wrist
        t = rel @ along
        u = (rel - np.outer(t, along)) @ across - f["centre"]
        owned = np.nonzero(weight > 0.0)[0]
        fingers = list(_FINGER_LAYOUT.items())
        for index in owned:
            ti, ui, wi = float(t[index]), float(u[index]), float(weight[index])
            # Thumb: the thumb-side edge of the hand, short of the knuckles.
            if ui > 0.40 * W and ti < 0.80 * L:
                finger = "thumb"
            elif ti < 0.40 * L:
                continue                       # palm: stays with the hand
            else:
                finger = min(fingers, key=lambda item: abs(item[1][1] * W - ui))[0]
            at, aim, reach, chain = chains[side][finger]
            s = float((pts[index] - at) @ aim) / max(reach, 1e-6)
            # How much of this vertex has left the palm for the finger: eases
            # in over the knuckle so the crease bends rather than snaps.
            leave = float(np.clip((s + 0.12) / 0.24, 0.0, 1.0))
            if leave <= 0.0:
                continue
            shares = {}
            for bone, start, end in chain:
                span = max(end - start, 1e-6)
                # Blend across each joint over a fifth of a phalanx. The first
                # bone owns everything before its own start (the knuckle), the
                # last everything past its end (the fingertip).
                ramp_in = 1.0 if bone == chain[0][0] else float(np.clip((s - start) / (0.2 * span) + 0.5, 0.0, 1.0))
                ramp_out = 1.0 if bone == chain[-1][0] else float(np.clip((end - s) / (0.2 * span) + 0.5, 0.0, 1.0))
                share = ramp_in * ramp_out
                if share > 0.0:
                    shares[bone] = share
            total = sum(shares.values())
            if total <= 0.0:
                continue
            moved = wi * leave
            for bone, share in shares.items():
                amount = moved * share / total
                if amount > 0.004:
                    new_groups[bone].add([int(index)], amount, "REPLACE")
            if wi - moved > 0.004:
                group.add([int(index)], wi - moved, "REPLACE")
            else:
                group.remove([int(index)])
    return len(plan), ""


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


# The five feelings VRM names, each as a mix of the shapes below. A companion
# device or an AI persona asks for these by name; a face tracker never does.
# Shared pieces rather than five sculpted faces: "happy" and "relaxed" are the
# same smile at different strengths with different eyelids, and that keeps the
# five recognisably one face.
EMOTIONS = {
    "happy": (("mouth_smile", 1.0), ("blink_L", 0.45), ("blink_R", 0.45)),
    "angry": (("brow_angry", 1.0), ("mouth_frown", 0.6)),
    "sad": (("brow_sad", 1.0), ("mouth_frown", 1.0), ("blink_L", 0.2), ("blink_R", 0.2)),
    "relaxed": (("blink_L", 0.55), ("blink_R", 0.55), ("mouth_smile", 0.35)),
    "surprised": (("eye_wide", 1.0), ("mouth_open", 1.0)),
}


def _add_emotion_shapes(body, base, centres, mouth_pt, eye_r, mouth_r):
    """Smile, frown, wide eyes and two brow shapes, from the same three
    landmarks the blink and the jaw use. Returns vertices moved per shape."""
    moved = {}

    def write(name, delta):
        key = body.shape_key_add(name=name, from_mix=False)
        touched = np.nonzero(np.abs(delta).sum(axis=1) > 1e-7)[0]
        for index in touched:
            key.data[int(index)].co = Vector(base[index] + delta[index])
        moved[name] = int(len(touched))

    mouth = np.array(mouth_pt, dtype=np.float64)
    reach = 1.25 * mouth_r
    dist = np.linalg.norm(base - mouth, axis=1)
    near = dist < reach
    fall = np.zeros(len(base))
    fall[near] = np.clip(1.0 - dist[near] / reach, 0.0, 1.0) ** 0.6
    side = np.sign(base[:, 0] - mouth[0])
    # 0 on the centre line, 1 out at the corners: a smile is the corners
    # moving while the middle of the lip stays roughly where it was.
    corner = np.clip(np.abs(base[:, 0] - mouth[0]) / (0.6 * mouth_r), 0.0, 1.0) ** 1.4

    smile = np.zeros_like(base)
    smile[:, 2] = 0.26 * mouth_r * fall * corner              # corners up
    smile[:, 0] = side * 0.10 * mouth_r * fall * corner       # and out
    smile[:, 1] = 0.05 * mouth_r * fall * corner              # and back, round the cheek
    write("mouth_smile", smile)

    frown = np.zeros_like(base)
    frown[:, 2] = -0.22 * mouth_r * fall * corner             # corners down
    frown[:, 0] = -side * 0.04 * mouth_r * fall * corner      # and pinched in
    below = base[:, 2] < mouth[2]
    frown[below, 2] += 0.07 * mouth_r * (fall * (1.0 - corner))[below]   # lower lip pushed up
    write("mouth_frown", frown)

    wide = np.zeros_like(base)
    angry = np.zeros_like(base)
    sad = np.zeros_like(base)
    for centre in centres:
        centre = np.array(centre, dtype=np.float64)
        outward = 1.0 if centre[0] >= 0 else -1.0
        dist = np.linalg.norm(base - centre, axis=1)

        inside = dist < 1.15 * eye_r
        open_up = np.clip(1.0 - dist[inside] / (1.15 * eye_r), 0.0, 1.0) ** 0.6
        height_off = base[inside, 2] - centre[2]
        wide[inside, 2] += 0.60 * height_off * open_up        # the eye opens taller
        wide[inside, 1] -= 0.10 * eye_r * open_up             # and bulges a little

        # The brow: a patch above the eye. t runs -1 (by the nose) to +1 (by
        # the temple), and which end drops is the whole difference between
        # cross and miserable.
        brow = centre + np.array([0.0, 0.0, 0.95 * eye_r])
        dist = np.linalg.norm(base - brow, axis=1)
        patch = dist < 1.45 * eye_r
        lift = np.clip(1.0 - dist[patch] / (1.45 * eye_r), 0.0, 1.0) ** 0.6
        t = np.clip((base[patch, 0] - centre[0]) * outward / eye_r, -1.0, 1.0)
        inner = np.clip(0.5 - 0.75 * t, 0.0, 1.0)
        outer = np.clip(0.5 + 0.75 * t, 0.0, 1.0)
        wide[patch, 2] += 0.36 * eye_r * lift                 # and the brow goes up
        angry[patch, 2] += eye_r * lift * (-0.50 * inner + 0.12 * outer)
        angry[patch, 0] -= outward * 0.10 * eye_r * lift * inner     # knitted together
        sad[patch, 2] += eye_r * lift * (-0.42 * outer + 0.20 * inner)
    write("eye_wide", wide)
    write("brow_angry", angry)
    write("brow_sad", sad)
    return moved


def add_shape_keys(body, m, spec, opts, projection, height, defaults, emotions=True):
    """Generate the three shapes the face tracker actually drives: an eye
    closing on each side, and a mouth that opens — and, unless told not to,
    the handful more that the five named VRM emotions are mixed from.

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

    if emotions:
        moved.update(_add_emotion_shapes(body, base, centres, mouth_pt, eye_r, mouth_r))

    chosen["vertices_moved"] = moved
    # How far the biggest-moving vertex travels, as a share of the model's
    # height. Under about half a percent and nobody will see the expression.
    chosen["travel"] = {}
    for name in moved:
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
    for blender_name, prop in {**VRM_SHOULDERS, **VRM_OPTIONAL}.items():
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
    for emotion, mix in EMOTIONS.items():
        expression = getattr(preset, emotion)
        for key_name, weight in mix:
            bind(expression, key_name, weight)
        # An emotion that already moves the lids or the jaw shouldn't have a
        # blink or a spoken vowel stacked on top at full strength: "blend"
        # tells the player to fade those down as the emotion comes up.
        if len(expression.morph_target_binds):
            if any(k.startswith("blink") or k == "eye_wide" for k, _ in mix):
                expression.override_blink = "blend"
            if any(k == "mouth_open" for k, _ in mix):
                expression.override_mouth = "blend"


def to_mtoon(body):
    """Swap every material for MToon, VRM's own toon shader.

    Players built around VRM (the Code27 among them) expect it, and say that a
    PBR node tree "doesn't survive export". The add-on's converter carries the
    base colour texture across; the lit and shaded colours are then set close
    together, because these textures arrive with their lighting already
    painted in and a hard two-tone band on top of that looks like a mistake."""
    done = []
    for slot in body.material_slots:
        material = slot.material
        if material is None or material.name in done:
            continue
        mtoon = material.vrm_addon_extension.mtoon1
        try:
            # Setting the flag is what the add-on's own checkbox does: it
            # rebuilds the node tree as MToon and carries the textures over.
            mtoon.enabled = True
        except Exception as exc:  # noqa: BLE001 — a material we can't convert stays as it was
            emit(log=f"couldn't convert material '{material.name}' to MToon ({exc})")
            continue
        if not mtoon.enabled:
            continue
        ext = mtoon.extensions.vrmc_materials_mtoon
        base = mtoon.pbr_metallic_roughness.base_color_texture.index.source
        if base is not None:
            ext.shade_multiply_texture.index.source = base
        ext.shade_color_factor = (0.72, 0.70, 0.74)
        ext.shading_toony_factor = 0.55
        ext.shading_shift_factor = -0.1
        ext.gi_equalization_factor = 0.9
        done.append(material.name)
    return done


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
    if opts.get("face_front", True):
        facing = face_by_skeleton(body, facing)
    emit(step="measuring", progress=0.6)
    m = measure(body)
    archetype, confidence, reasons = suggest(m)
    if source_rig_is_human():
        # Proportions are a guess; a skeleton with two arms, two legs and a
        # head is not.
        archetype, confidence = "humanoid", 0.9
        reasons = [
            f"it came with its own human skeleton ({len(SOURCE_RIG['names'])} bones) — "
            "its joints and skin weights will be kept"
        ] + reasons[:1]
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
    if opts.get("face_front", True):
        # An explicit "it's facing away" from the user outranks the skeleton.
        facing = face_by_skeleton(body, facing)
        if facing.get("by_skeleton"):
            emit(log="its own skeleton says it was facing away — turned it round")
    emit(step="measuring the body", progress=0.34)
    m = measure(body)

    # What the avatar is FOR decides the extras. The browser avatar only ever
    # asks for blinks and vowels and looks right with the model's own
    # materials; a companion device plays whole-body animation and emotions,
    # and expects VRM's toon shader.
    target = opts.get("target") or "avatar"
    want_mtoon = opts["mtoon"] if opts.get("mtoon") is not None else target == "code27"
    want_fingers = opts["fingers"] if opts.get("fingers") is not None else target == "code27"

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
    adopted = adopt_source_rig(body, height, keep_fingers=want_fingers) if opts.get("adopt_rig", True) else None
    if adopted:
        plan, weight_map = adopted
        rig = build_armature(plan)
        emit(step="binding the mesh to the bones", progress=0.56)
        emit(log=f"this model came with its own skeleton ({len(SOURCE_RIG['names'])} bones) — keeping its joints and weights")
        method, covered = "source", skin_from_source(body, rig, weight_map)
    else:
        if SOURCE_RIG.get("joints") is not None:
            emit(log="the skeleton this model came with isn't one we can map — building a new one")
        plan, mode = plan_bones(m, spec, height)
        rig = build_armature(plan)
        emit(step="binding the mesh to the bones", progress=0.56)
        method, covered = skin(body, rig)
    emit(log=f"{method} weights — {covered:,}/{len(body.data.vertices):,} vertices bound")

    separated = 0
    if opts.get("separate_arms", bool(adopted) or archetype == "humanoid"):
        separated, torn = separate_fused_arms(body, rig, height, trust_weights=bool(adopted))
        if separated:
            emit(log=f"cut {separated:,} faces that welded an arm to the body ({torn:,} vertices re-assigned)")

    fingers, fingers_note = 0, ""
    came_with = sum(1 for b in rig.data.bones if b.name.split("_")[0] in FINGERS)
    if came_with:
        fingers = came_with
        emit(log=f"kept the {came_with} finger bones the model came with")
    elif want_fingers:
        if adopted or archetype == "humanoid":
            emit(step="adding fingers", progress=0.64)
            fingers, fingers_note = add_fingers(body, rig, height)
            emit(log=f"{fingers} finger bones added" if fingers else fingers_note)
        else:
            fingers_note = f"No finger bones: only people get them, and this was rigged as {spec['label'].lower()}."

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
            body, m, spec, opts, scale_projection(projection, scaled_by), height, recipes["defaults"],
            emotions=opts.get("emotions", True),
        )
        emit(log="face shapes: " + ", ".join(f"{k} {v}v" for k, v in face_info["vertices_moved"].items()))

    toon = []
    if want_mtoon:
        emit(step="converting materials to MToon", progress=0.73)
        toon = to_mtoon(body)
        emit(log=f"MToon materials: {len(toon)} of {len([s for s in body.material_slots if s.material])}")

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
    # And the pose the file actually rests in. VRM straightens every avatar
    # into a T on export, which is the first time the armpits, and anything
    # that was welded to a hip, are asked to move — worth seeing before a
    # device plays a dance on it.
    try:
        from bl_ext.blender_org.vrm.editor.t_pose import set_estimated_humanoid_t_pose

        activate(rig)
        bpy.ops.object.mode_set(mode="POSE")
        set_estimated_humanoid_t_pose(bpy.context, rig)
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.update()
        more, _ = render_views(body, work, "rigged-tpose-", views=("front",), posed=True)
        shots["tpose"] = more["front"]
    except Exception as exc:  # noqa: BLE001 — a missing preview must not fail a finished rig
        emit(log=f"no T-pose preview ({exc})")
    finally:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for pose_bone in rig.pose.bones:
            pose_bone.matrix_basis = Matrix.Identity(4)
        bpy.context.view_layer.update()

    for emotion, mix in EMOTIONS.items():
        if not all(k in keys for k, _ in mix):
            continue
        wanted = dict(mix)
        for block in keys:
            block.value = wanted.get(block.name, 0.0)
        bpy.context.view_layer.update()
        more, _ = render_views(body, work, f"rigged-{emotion}-", views=("front",), focus=focus)
        shots[emotion] = more["front"]
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
        target=target,
        mtoon=len(toon),
        fingers=fingers,
        fingers_note=fingers_note,
        adopted_skeleton=bool(adopted),
        separated_faces=separated,
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
