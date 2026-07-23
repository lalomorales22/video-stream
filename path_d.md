# Path D — Avatar Studio (scope)

Turn the avatar page into a **studio**: set up, customize, save, and stage avatars, then
push any of them into OBS. Builds on the Path B avatar + tracking.

Status: **started.** The **gallery** (save / load / delete presets, each with its own OBS
URL) is built — backend + UI. The rest below is planned.

---

## The honest scope (read this first)

There's a hard line worth being straight about:

- **Building a VRM from scratch** — sculpting the mesh, rigging a skeleton, painting
  textures, authoring blendshapes — is a *massive* undertaking. VRoid Studio is a whole
  company's product; Blender is decades of work. We are **not** building an in-browser 3D
  modeler. Pretending otherwise would waste your time.
- **What we CAN build is a great studio around VRM *bases*:** import an avatar (made free
  in VRoid, downloaded, or AI-assisted), then **customize, express, pose, stage, save, and
  ship** it. That's where the creative fun actually lives, and it's very doable.

So: "character **studio**", not "character **modeler**." Bases come from VRoid/imports;
everything downstream is ours.

## Progress

- ✅ **Gallery** — save the current avatar + setup as a named preset (VRM is uploaded and
  persisted server-side so it survives and loads from OBS anywhere); load any preset;
  copy a per-preset OBS URL; delete. Stored under `static/gallery/` (gitignored).
- ⬜ **Fold into the app** — an "Avatar" tab/panel in the dashboard instead of a separate
  page, so cameras + avatars live in one place.
- ⬜ **Customize** — recolor/retexture materials, swap outfits/accessories, tweak
  proportions (bone scales), all on a loaded VRM.
- ⬜ **Expression editor** — sliders over the VRM's expressions (happy/angry/blink/visemes);
  save custom expression sets; trigger them live or hotkey them for OBS.
- ⬜ **Pose / staging** — saved poses, position/scale on screen (partly done: drag-move,
  zoom, recenter), multiple avatars, backgrounds.
- ⬜ **AI-assisted base** (stretch) — generate concept art → guide a VRoid/base build. The
  2D→rigged-VRM step is still the hard part; treat as research, not a promise.

## How the gallery works (built)

- `POST /api/avatar/vrm` — upload a VRM (raw body), stored at `/static/gallery/vrm/<id>.vrm`.
- `GET /api/avatar/presets` — list. `POST` — save `{name, vrm, settings}`. `DELETE /{id}` —
  remove (and drop its VRM if unreferenced).
- A preset's `settings` = `{mirror, body, zoom, pan, ox, oy, src}` — everything needed to
  reproduce the shot. The per-preset **Copy OBS URL** encodes it all, so a saved avatar is
  one paste away from an OBS Browser Source on any machine.

## Suggested next steps

1. **Customize (colors/textures)** — the highest fun-per-effort after the gallery. three.js
   material edits on the loaded VRM (tint, swap texture), saved into the preset.
2. **Expression editor** — sliders + saved expression sets; great for OBS reactions.
3. **Fold into the dashboard** — the app-integration you asked for; mostly UI plumbing.

## Decisions to make

1. **Customization depth:** just recolor/expressions (fast, high value), or also
   outfits/accessories (needs modular assets — where do they come from)?
2. **Avatar bases:** rely on VRoid/imports (recommended), or invest in the AI-assisted
   pipeline (research-y)?
3. **App integration:** avatar as a dashboard *tab* (simplest), or avatars as *cards* in
   the same grid as cameras (more work, more unified)?

See [`path_b.md`](path_b.md) (avatar) and [`path_c.md`](path_c.md) (AI persona) for the
adjacent tracks.
