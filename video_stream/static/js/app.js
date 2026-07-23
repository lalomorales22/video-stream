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
  const toastEl = document.getElementById("toast");

  let state = { cameras: [], bases: [], port: 8765, primary_ip: "127.0.0.1" };
  let directorState = { enabled: false, active: null, dry_run: false, obs_connected: false };
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
    const card = document.createElement("article");
    card.className = `card${camera.active ? "" : " offline"}${onAir ? " on-air" : ""}`;
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
            await refresh();
          } catch (err) {
            toast(err.message || "Pose unavailable — run ./install-pose.sh", false);
          } finally {
            btn.disabled = false;
          }
        }
      });
    });

    return card;
  }

  function render() {
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

  preferLan?.addEventListener("change", () => render());
  btnRefresh?.addEventListener("click", async () => {
    btnRefresh.disabled = true;
    try {
      await refresh({ discover: true });
      toast("Camera list refreshed");
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

  refresh();
  refreshDirector();
  // Soft poll for status (previews are live MJPEG; this keeps badges/URLs fresh)
  setInterval(() => refresh(), 8000);
  // Director status ticks faster so the "on air" camera stays current
  setInterval(() => {
    if (directorState.enabled) refreshDirector();
  }, 2000);
})();
