(() => {
  const grid = document.getElementById("camera-grid");
  const empty = document.getElementById("empty-state");
  const liveCount = document.getElementById("live-count");
  const cameraCount = document.getElementById("camera-count");
  const hostPill = document.getElementById("host-pill");
  const preferLan = document.getElementById("prefer-lan");
  const btnRefresh = document.getElementById("btn-refresh");
  const btnDirector = document.getElementById("btn-director");
  const directorLabel = document.getElementById("director-label");
  const directorActive = document.getElementById("director-active");
  const btnKill = document.getElementById("btn-kill");
  const killLabel = document.getElementById("kill-label");
  const safetyBudget = document.getElementById("safety-budget");
  const btnHighlight = document.getElementById("btn-highlight");
  const autoHighlight = document.getElementById("auto-highlight");
  const toastEl = document.getElementById("toast");

  let state = { cameras: [], bases: [], port: 8765, primary_ip: "127.0.0.1" };
  let directorState = { enabled: false, active: null, dry_run: false, obs_connected: false };
  let safetyState = null;
  let toastTimer = null;

  function toast(message, ok = true) {
    toastEl.textContent = message;
    toastEl.classList.toggle("ok", ok);
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 1800);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    }
  }

  function pickUrls(camera) {
    const urls = camera.urls || [];
    if (!urls.length) {
      return {
        stream: camera.stream_url,
        view: camera.view_url,
      };
    }

    if (preferLan?.checked) {
      const lan = urls.find((u) => u.ip && u.ip !== "127.0.0.1" && !String(u.ip).includes("localhost"));
      if (lan) return { stream: lan.stream, view: lan.view, label: lan.label, ip: lan.ip };
    }

    // Prefer non-loopback always for shareability when available
    const shared = urls.find((u) => u.ip && u.ip !== "127.0.0.1");
    const chosen = shared || urls[0];
    return { stream: chosen.stream, view: chosen.view, label: chosen.label, ip: chosen.ip };
  }

  // Map a click on an object-fit:cover <img> to normalized source-frame coords.
  function coverPoint(img, e) {
    const rect = img.getBoundingClientRect();
    const nw = img.naturalWidth || rect.width;
    const nh = img.naturalHeight || rect.height;
    const scale = Math.max(rect.width / nw, rect.height / nh);
    const offX = (nw * scale - rect.width) / 2;
    const offY = (nh * scale - rect.height) / 2;
    const x = (e.clientX - rect.left + offX) / (nw * scale);
    const y = (e.clientY - rect.top + offY) / (nh * scale);
    return { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) };
  }

  function esc(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function renderCard(camera) {
    const picked = pickUrls(camera);
    const res =
      camera.width && camera.height
        ? `${camera.width}×${camera.height}${camera.fps ? ` · ${camera.fps}fps` : ""}`
        : "—";
    const status = camera.active ? "LIVE" : camera.error ? "ERROR" : "OFF";
    const previewSrc = camera.active ? `/stream/${camera.index}?t=${Date.now()}` : "";

    const onAir = directorState.enabled && directorState.active === camera.index;
    const zoomed = (camera.zoom || 1) > 1.05;
    const card = document.createElement("article");
    card.className = `card${camera.active ? "" : " offline"}${onAir ? " on-air" : ""}${zoomed ? " zoomed" : ""}`;
    card.dataset.index = String(camera.index);
    card.innerHTML = `
      <div class="preview-wrap">
        ${
          camera.active
            ? `<img src="${previewSrc}" alt="${esc(camera.name)} preview" loading="lazy" />`
            : `<div class="placeholder">${camera.error ? esc(camera.error) : "stream offline"}</div>`
        }
        <div class="preview-chrome"></div>
        <div class="preview-top">
          <span class="live-badge${camera.active ? "" : " off"}">
            <span class="dot"></span>${status}
          </span>
          <span class="res-badge">${esc(res)}</span>
        </div>
      </div>
      <div class="card-body">
        <div class="card-head">
          <div>
            <h3>${esc(camera.name)}</h3>
            <p class="card-sub">id ${camera.index}${picked.ip ? ` · ${esc(picked.ip)}` : ""}</p>
          </div>
        </div>

        <div class="url-list">
          <div class="url-row">
            <div class="url-label">
              <span>OBS · Browser Source</span>
            </div>
            <div class="url-box">
              <code title="Click to copy" data-copy="${esc(picked.view)}">${esc(picked.view)}</code>
              <button type="button" class="btn btn-copy" data-copy="${esc(picked.view)}">Copy</button>
            </div>
          </div>
          <div class="url-row">
            <div class="url-label">
              <span>MJPEG · stream</span>
            </div>
            <div class="url-box">
              <code title="Click to copy" data-copy="${esc(picked.stream)}">${esc(picked.stream)}</code>
              <button type="button" class="btn btn-copy" data-copy="${esc(picked.stream)}">Copy</button>
            </div>
          </div>
        </div>

        <div class="card-actions">
          <a class="btn btn-sm" href="/view/${camera.index}" target="_blank" rel="noopener">Open view</a>
          <button type="button" class="btn btn-sm" data-action="toggle" data-index="${camera.index}">
            ${camera.active ? "Stop" : "Start"}
          </button>
          <button type="button" class="btn btn-sm btn-pose${camera.pose ? " on" : ""}" data-action="pose" data-index="${camera.index}" ${camera.active ? "" : "disabled"}>
            ${camera.pose ? "Skeleton ·on" : "Skeleton"}
          </button>
          <button type="button" class="btn btn-sm btn-signal" data-action="copy-view" data-index="${camera.index}">
            Copy OBS URL
          </button>
        </div>
      </div>
    `;

    card.querySelectorAll("[data-copy]").forEach((el) => {
      el.addEventListener("click", async () => {
        const text = el.getAttribute("data-copy");
        if (!text) return;
        const ok = await copyText(text);
        if (ok) {
          const btn = el.classList.contains("btn-copy")
            ? el
            : el.parentElement?.querySelector(".btn-copy");
          if (btn) {
            btn.classList.add("copied");
            btn.textContent = "Copied";
            setTimeout(() => {
              btn.classList.remove("copied");
              btn.textContent = "Copy";
            }, 1400);
          }
          toast("URL copied");
        } else {
          toast("Could not copy", false);
        }
      });
    });

    // Smart Zoom: double-click punches in 2x on that spot (baked into the
    // stream server-side, so OBS sees it too); double-click again to go wide.
    const previewImg = card.querySelector(".preview-wrap img");
    if (previewImg) {
      previewImg.addEventListener("dblclick", async (e) => {
        e.preventDefault();
        const cam = state.cameras.find((c) => c.index === camera.index);
        const isZoomed = (cam?.zoom || 1) > 1.05;
        const body = isZoomed
          ? { level: 1 }
          : { ...coverPoint(previewImg, e), level: 2 };
        try {
          const res = await fetch(`/api/cameras/${camera.index}/zoom`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          if (!res.ok) throw new Error("Zoom failed");
          const d = await res.json();
          if (cam) cam.zoom = d.zoom;
          card.classList.toggle("zoomed", d.zoom > 1.05);
          toast(d.zoom > 1.05 ? "Punch in 2×" : "Wide shot");
        } catch (err) {
          toast(err.message || "Zoom failed", false);
        }
      });
    }

    card.querySelectorAll("[data-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const action = btn.getAttribute("data-action");
        const index = Number(btn.getAttribute("data-index"));
        if (action === "copy-view") {
          const cam = state.cameras.find((c) => c.index === index);
          if (!cam) return;
          const { view } = pickUrls(cam);
          const ok = await copyText(view);
          toast(ok ? "OBS view URL copied" : "Could not copy", ok);
          return;
        }
        if (action === "toggle") {
          btn.disabled = true;
          try {
            const cam = state.cameras.find((c) => c.index === index);
            const path = cam?.active
              ? `/api/cameras/${index}/stop`
              : `/api/cameras/${index}/start`;
            const res = await fetch(path, { method: "POST" });
            if (!res.ok) {
              const body = await res.json().catch(() => ({}));
              throw new Error(body.detail || "Request failed");
            }
            await refresh();
          } catch (err) {
            toast(err.message || "Action failed", false);
          } finally {
            btn.disabled = false;
          }
        }
        if (action === "pose") {
          const cam = state.cameras.find((c) => c.index === index);
          const turnOn = !cam?.pose;
          // Optimistic: flip the button now so it feels instant, revert on failure.
          if (cam) cam.pose = turnOn;
          btn.classList.toggle("on", turnOn);
          btn.textContent = turnOn ? "Skeleton ·on" : "Skeleton";
          btn.disabled = true;
          try {
            const res = await fetch(`/api/cameras/${index}/pose`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ enabled: turnOn }),
            });
            if (!res.ok) {
              const body = await res.json().catch(() => ({}));
              // MediaPipe missing → show the first line of the install hint
              throw new Error(String(body.detail || "Pose toggle failed").split("\n")[0]);
            }
            toast(turnOn ? "Skeleton on" : "Skeleton off");
          } catch (err) {
            // Revert the optimistic flip.
            if (cam) cam.pose = !turnOn;
            btn.classList.toggle("on", !turnOn);
            btn.textContent = !turnOn ? "Skeleton ·on" : "Skeleton";
            toast(err.message || "Pose unavailable — run ./install-pose.sh", false);
          } finally {
            btn.disabled = false;
          }
        }
      });
    });

    return card;
  }

  let lastGridSig = "";

  function render(force = false) {
    const cameras = state.cameras || [];
    const active = cameras.filter((c) => c.active).length;

    liveCount.textContent = active ? `${active} live` : "idle";
    cameraCount.textContent =
      cameras.length === 0
        ? "no cameras"
        : `${cameras.length} camera${cameras.length === 1 ? "" : "s"}`;

    if (state.primary_ip && state.port) {
      hostPill.textContent = `${state.primary_ip}:${state.port}`;
    }

    // Rebuilding cards restarts MJPEG previews and detaches in-flight
    // buttons, so skip it when nothing the cards show actually changed
    // (fps is rounded: it jitters by fractions on every poll).
    const sig = JSON.stringify(
      cameras.map((c) => ({ ...c, fps: Math.round(c.fps || 0) }))
    );
    if (!force && sig === lastGridSig) return;
    lastGridSig = sig;

    // Keep empty state node, rebuild the rest
    [...grid.querySelectorAll(".card")].forEach((n) => n.remove());

    if (cameras.length === 0) {
      empty.classList.remove("hidden");
      return;
    }

    empty.classList.add("hidden");
    cameras.forEach((cam) => grid.appendChild(renderCard(cam)));
  }

  async function refresh({ discover = false } = {}) {
    try {
      if (discover) {
        await fetch("/api/discover", { method: "POST" });
      }
      const res = await fetch("/api/status");
      if (!res.ok) throw new Error("status failed");
      state = await res.json();
      render();
    } catch (err) {
      console.error(err);
      cameraCount.textContent = "offline";
    }
  }

  function renderDirector() {
    const on = directorState.enabled;
    btnDirector?.setAttribute("aria-pressed", on ? "true" : "false");
    btnDirector?.classList.toggle("on", on);
    if (directorLabel) directorLabel.textContent = on ? "Auto-director: On" : "Auto-director: Off";
    // Update the "on air" ring on existing cards WITHOUT rebuilding them
    // (rebuilding would detach buttons mid-click and flicker the previews).
    grid.querySelectorAll(".card").forEach((card) => {
      const idx = Number(card.dataset.index);
      card.classList.toggle("on-air", on && directorState.active === idx);
    });
    if (directorActive) {
      if (!on) {
        directorActive.textContent = "";
      } else {
        const cam =
          directorState.active != null
            ? state.cameras.find((c) => c.index === directorState.active)
            : null;
        const who = cam ? cam.name : directorState.active != null ? `cam ${directorState.active}` : "…";
        const mode = directorState.dry_run
          ? "dry-run"
          : directorState.obs_connected
          ? "→ OBS"
          : "no OBS";
        directorActive.textContent = `on air: ${who} · ${mode}`;
      }
    }
  }

  async function refreshDirector() {
    try {
      const res = await fetch("/api/director");
      if (!res.ok) return;
      const d = await res.json();
      directorState = {
        enabled: !!d.enabled,
        active: d.active ?? null,
        dry_run: !!d.dry_run,
        obs_connected: !!d.obs_connected,
      };
      renderDirector(); // updates on-air ring in place, no card rebuild
    } catch {
      /* ignore */
    }
  }

  preferLan?.addEventListener("change", () => render(true));
  btnRefresh?.addEventListener("click", async () => {
    btnRefresh.disabled = true;
    try {
      await refresh({ discover: true }); // kicks off a background rescan
      toast("Rescanning…");
      // Discovery runs in the background now — poll a few times to pick up new cams.
      let n = 0;
      const t = setInterval(async () => {
        await refresh();
        if (++n >= 6) clearInterval(t);
      }, 1500);
    } finally {
      btnRefresh.disabled = false;
    }
  });

  btnDirector?.addEventListener("click", async () => {
    btnDirector.disabled = true;
    try {
      const res = await fetch("/api/director", { method: "POST" });
      if (!res.ok) throw new Error("Director toggle failed");
      const d = await res.json();
      toast(d.enabled ? "Auto-director on" : "Auto-director off");
      await refreshDirector();
    } catch (err) {
      toast(err.message || "Director toggle failed", false);
    } finally {
      btnDirector.disabled = false;
    }
  });

  // ── Safety: kill switch + automation budget ─────────────────────────
  function renderSafety() {
    const s = safetyState;
    if (!s) return;
    const killed = !!s.kill_switch;
    btnKill?.classList.toggle("on", killed);
    btnKill?.setAttribute("aria-pressed", killed ? "true" : "false");
    if (killLabel) killLabel.textContent = killed ? "KILLED" : "KILL";
    if (safetyBudget) {
      safetyBudget.textContent = killed
        ? "automations frozen"
        : `${s.remaining}/${s.max_actions} auto`;
      safetyBudget.classList.toggle("warn", !killed && s.remaining <= 5);
      safetyBudget.classList.toggle("killed", killed);
    }
  }

  // Deliberately no confirm dialog: a panic button that asks questions
  // isn't a panic button. Engaging is loud and one click reverses it.
  async function toggleKill() {
    const on = !safetyState?.kill_switch;
    try {
      const res = await fetch("/api/safety/kill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on, reason: "dashboard" }),
      });
      if (!res.ok) throw new Error("Kill switch failed");
      safetyState = await res.json();
      renderSafety();
      toast(on ? "KILL — automations frozen" : "Kill switch released", !on);
    } catch (err) {
      toast(err.message || "Kill switch failed", false);
    }
  }

  btnKill?.addEventListener("click", toggleKill);

  // ── Replay highlights ───────────────────────────────────────────────
  async function captureHighlight() {
    if (btnHighlight?.disabled) return;
    if (btnHighlight) btnHighlight.disabled = true;
    try {
      const res = await fetch("/api/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "Replay capture failed");
      toast(`Highlight saved${body.label ? " · " + body.label : ""}`);
    } catch (err) {
      toast(err.message || "Replay capture failed", false);
    } finally {
      if (btnHighlight) btnHighlight.disabled = false;
    }
  }

  btnHighlight?.addEventListener("click", captureHighlight);

  autoHighlight?.addEventListener("change", async () => {
    try {
      const res = await fetch("/api/replay/auto", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: autoHighlight.checked }),
      });
      if (!res.ok) throw new Error("Toggle failed");
      const d = await res.json();
      autoHighlight.checked = !!d.auto_enabled;
      toast(d.auto_enabled ? "Auto highlights on — watching for spikes" : "Auto highlights off");
    } catch (err) {
      // Re-sync from the server instead of blindly inverting: a concurrent
      // bus update may already have corrected the checkbox.
      try {
        const s = await fetch("/api/replay").then((r) => (r.ok ? r.json() : null));
        if (s) autoHighlight.checked = !!s.auto_enabled;
      } catch {}
      toast(err.message || "Auto highlights failed", false);
    }
  });

  // ── Hotkeys: Ctrl/Cmd+Shift+K = kill switch, Ctrl/Cmd+Shift+H = highlight
  // (H, not R — Cmd+Shift+R is the browser's hard reload and can't be trusted
  // to preventDefault everywhere.)
  document.addEventListener("keydown", (e) => {
    if (e.repeat) return; // holding the combo must not strobe the kill switch
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (!(e.ctrlKey || e.metaKey) || !e.shiftKey) return;
    if (e.code === "KeyK") {
      e.preventDefault();
      toggleKill();
    } else if (e.code === "KeyH") {
      e.preventDefault();
      captureHighlight();
    }
  });

  // ── Studio Bus: live push replaces polling while connected ──────────
  Bus.on("director", (d) => {
    directorState = {
      enabled: !!d.enabled,
      active: d.active ?? null,
      dry_run: !!d.dry_run,
      obs_connected: !!d.obs_connected,
    };
    renderDirector();
  });
  // Camera pushes trigger a (debounced) status re-fetch instead of rendering
  // the payload directly: /api/status sees this page's Host header, so the
  // displayed OBS URLs never flap between bases chosen by someone else.
  let camerasRefresh = null;
  Bus.on("cameras", () => {
    clearTimeout(camerasRefresh);
    camerasRefresh = setTimeout(() => refresh(), 250);
  });
  Bus.on("safety", (s) => {
    safetyState = s;
    renderSafety();
  });
  Bus.on("replay", (r) => {
    // Server truth wins — a click mid-flight will be re-synced by its own
    // request handler right after.
    if (autoHighlight) autoHighlight.checked = !!r.auto_enabled;
  });
  Bus.on("replay_saved", (r) => {
    toast(`Highlight saved${r.label ? " · " + r.label : ""}`);
  });
  Bus.on("_open", () => {
    // Re-sync once on (re)connect; retained events fill in the rest.
    refresh();
    refreshDirector();
  });

  refresh();
  refreshDirector();
  fetch("/api/safety")
    .then((r) => (r.ok ? r.json() : null))
    .then((s) => {
      if (s) {
        safetyState = s;
        renderSafety();
      }
    })
    .catch(() => {});
  // Cameras open in the background at startup — poll quickly until they appear,
  // then settle into the slow refresh.
  let warmup = 0;
  const warmupTimer = setInterval(() => {
    if ((state.cameras || []).length > 0 || warmup++ > 20) {
      clearInterval(warmupTimer);
      return;
    }
    refresh();
  }, 1500);
  // Soft poll for status (previews are live MJPEG; this keeps badges/URLs fresh)
  setInterval(() => refresh(), 8000);
  // Degraded fallback: only poll the director when the bus is down —
  // while connected, switches arrive as push events instead.
  setInterval(() => {
    if (!Bus.connected && directorState.enabled) refreshDirector();
  }, 2000);
})();
