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
      renderOverlayChips();
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
        const who = cam
          ? cam.name
          : directorState.active != null
          ? `cam ${directorState.active}`
          : directorState.active_rule || "…";
        const mode = directorState.dry_run
          ? "dry-run"
          : directorState.obs_connected
          ? "→ OBS"
          : "no OBS";
        const why = directorState.last_decision ? ` · ${directorState.last_decision}` : "";
        directorActive.textContent = `on air: ${who} · ${mode}${why}`;
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
        active_rule: d.active_rule ?? null,
        dry_run: !!d.dry_run,
        obs_connected: !!d.obs_connected,
        last_decision: d.last_decision ?? null,
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

  // ── View tabs: Streams | Avatar (avatar mounts lazily in an iframe;
  //    hiding it throttles its rAF loop, so tracking pauses off-tab and
  //    resumes with all state intact when you come back) ────────────────
  const tabStreams = document.getElementById("tab-streams");
  const tabAvatar = document.getElementById("tab-avatar");
  const viewStreams = document.getElementById("view-streams");
  const viewAvatar = document.getElementById("view-avatar");
  const avatarFrame = document.getElementById("avatar-frame");

  function showView(which) {
    const avatar = which === "avatar";
    if (avatar && avatarFrame && !avatarFrame.getAttribute("src")) {
      avatarFrame.src = "/avatar"; // first open only — dashboard stays light
    }
    viewStreams?.classList.toggle("hidden", avatar);
    viewAvatar?.classList.toggle("hidden", !avatar);
    tabStreams?.classList.toggle("on", !avatar);
    tabStreams?.setAttribute("aria-selected", String(!avatar));
    tabAvatar?.classList.toggle("on", avatar);
    tabAvatar?.setAttribute("aria-selected", String(avatar));
  }

  tabStreams?.addEventListener("click", () => showView("streams"));
  tabAvatar?.addEventListener("click", () => showView("avatar"));

  // ── Live captions: Web Speech in THIS tab → subtitles overlay ───────
  const btnSttStart = document.getElementById("btn-stt-start");
  const btnSttStop = document.getElementById("btn-stt-stop");
  const sttStatus = document.getElementById("stt-status");
  const subtitleInput = document.getElementById("subtitle-text");
  let sttRecognition = null;
  let sttActive = false;
  let sttDenied = false;
  let interimTimer = null;
  let pendingInterim = null;

  function pushSubtitle(text, final) {
    return fetch("/api/subtitles/push", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, final }),
    }).catch(() => {});
  }

  function sttUi(listening) {
    btnSttStart?.classList.toggle("hidden", listening);
    btnSttStop?.classList.toggle("hidden", !listening);
    if (sttStatus) sttStatus.textContent = listening ? "🔴 listening" : "";
  }

  function startSTT() {
    if (sttActive) return;
    sttDenied = false;
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      toast("Speech recognition needs Chrome (and localhost or HTTPS)", false);
      return;
    }
    sttRecognition = new Recognition();
    sttRecognition.continuous = true;
    sttRecognition.interimResults = true;
    sttRecognition.lang = "en-US";
    sttRecognition.onstart = () => {
      sttActive = true;
      sttUi(true);
    };
    sttRecognition.onend = () => {
      if (sttActive) {
        // Chrome ends recognition after silence — restart quietly (small
        // delay so error loops can't spin hot).
        setTimeout(() => {
          if (sttActive) {
            try { sttRecognition.start(); } catch {}
          }
        }, 300);
        return;
      }
      sttUi(false);
      // The trailing onend after a not-allowed error must not eat the message.
      if (sttDenied && sttStatus) sttStatus.textContent = "mic denied";
    };
    sttRecognition.onerror = (e) => {
      if (e.error === "not-allowed") {
        sttDenied = true;
        sttActive = false;
        sttUi(false);
        if (sttStatus) sttStatus.textContent = "mic denied";
      }
      // Everything else (no-speech, network, aborted) rides the onend restart.
    };
    sttRecognition.onresult = (event) => {
      if (!sttActive) return; // stopped — never caption after the clear
      let interimText = "";
      let finalText = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript.trim();
        if (!transcript) continue;
        if (event.results[i].isFinal) finalText += (finalText ? " " : "") + transcript;
        else interimText = transcript; // overwrite — last interim wins
      }
      if (finalText) {
        clearTimeout(interimTimer);
        pendingInterim = null;
        pushSubtitle(finalText, true);
      } else if (interimText) {
        // Trailing-edge ~200ms throttle: Chrome fires several results/sec.
        pendingInterim = interimText;
        if (!interimTimer) {
          interimTimer = setTimeout(() => {
            interimTimer = null;
            if (pendingInterim) pushSubtitle(pendingInterim, false);
            pendingInterim = null;
          }, 200);
        }
      }
    };
    try {
      sttRecognition.start();
    } catch {
      toast("Could not start the microphone", false);
    }
  }

  function stopSTT() {
    sttActive = false; // FIRST — so the trailing onend doesn't restart
    clearTimeout(interimTimer); // a queued interim must not caption after the clear
    interimTimer = null;
    pendingInterim = null;
    try { sttRecognition?.stop(); } catch {}
    sttUi(false);
    fetch("/api/subtitles/clear", { method: "POST" }).catch(() => {});
  }

  btnSttStart?.addEventListener("click", startSTT);
  btnSttStop?.addEventListener("click", stopSTT);
  document.getElementById("btn-subtitle-push")?.addEventListener("click", () => {
    const text = subtitleInput?.value.trim();
    if (!text) return;
    pushSubtitle(text, true);
    subtitleInput.value = "";
  });
  subtitleInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("btn-subtitle-push")?.click();
  });

  // ── Overlay URL chips + test alert ──────────────────────────────────
  const overlayChips = document.getElementById("overlay-chips");
  function renderOverlayChips() {
    if (!overlayChips) return;
    const base =
      (state.bases || []).find((b) => b.ip && b.ip !== "127.0.0.1")?.base ||
      (state.bases || [])[0]?.base ||
      "";
    overlayChips.innerHTML = "";
    ["subtitles", "alerts", "hud", "stinger", "chat", "fx"].forEach((name) => {
      const chip = document.createElement("span");
      chip.className = "overlay-chip";
      chip.textContent = name;
      chip.title = `Copy OBS browser-source URL: ${base}/overlay/${name}`;
      chip.addEventListener("click", async () => {
        const ok = await copyText(`${base}/overlay/${name}`);
        toast(ok ? `${name} overlay URL copied` : "Could not copy", ok);
      });
      overlayChips.appendChild(chip);
    });
  }

  document.getElementById("btn-alert-test")?.addEventListener("click", async () => {
    const types = ["follow", "sub", "raid", "bits", "donation"];
    const type = types[Math.floor(Math.random() * types.length)];
    const res = await fetch("/api/alerts/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, amount: type === "bits" ? 500 : type === "donation" ? "$5.00" : null }),
    }).catch(() => null);
    toast(res && res.ok ? `Test ${type} alert sent` : "Alert failed", !!(res && res.ok));
  });

  // ── FX + chaos presets: overlay effects and OBS choreography ─────────
  const fxButtons = document.getElementById("fx-buttons");
  const chaosSelect = document.getElementById("chaos-select");
  let chaosPresets = [];

  async function loadChaos() {
    try {
      const res = await fetch("/api/chaos");
      if (!res.ok) return;
      const d = await res.json();
      chaosPresets = d.presets || [];
      if (fxButtons && !fxButtons.childElementCount) {
        (d.effects || []).forEach((effect) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "btn btn-sm";
          b.textContent = effect;
          b.title = `Play the ${effect} effect on the FX overlay`;
          b.addEventListener("click", async () => {
            const r = await fetch("/api/chaos/fx", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ effect }),
            }).catch(() => null);
            toast(r && r.ok ? `${effect}!` : "FX failed — is the fx overlay open?", !!(r && r.ok));
          });
          fxButtons.appendChild(b);
        });
      }
      if (chaosSelect) {
        chaosSelect.innerHTML = "";
        chaosPresets.forEach((p) => {
          const opt = document.createElement("option");
          opt.value = p.id;
          opt.textContent = p.name;
          chaosSelect.appendChild(opt);
        });
        chaosSelect.parentElement
          ?.querySelectorAll(".chaos-select, #btn-chaos-run")
          .forEach((el) => el.classList.toggle("hidden", chaosPresets.length === 0));
      }
      (d.load_errors || []).forEach((e) => console.warn("[chaos]", e));
    } catch {}
  }

  document.getElementById("btn-chaos-run")?.addEventListener("click", async () => {
    const id = chaosSelect?.value;
    if (!id) return;
    const preset = chaosPresets.find((p) => p.id === id);
    if (preset?.confirm && !window.confirm(`Run '${preset.name}'? It drives OBS directly.`)) return;
    try {
      const res = await fetch("/api/chaos/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "Chaos failed");
      toast(`Running '${preset?.name || id}' (${body.steps} steps)`);
    } catch (err) {
      toast(err.message || "Chaos failed", false);
    }
  });

  loadChaos();

  // ── Phone-as-camera: mint a session, show the QR, watch for the phone ─
  const phonePanel = document.getElementById("phone-panel");
  const phoneQr = document.getElementById("phone-qr");
  const phoneTitle = document.getElementById("phone-title");
  const phoneHint = document.getElementById("phone-hint");
  const phoneViewUrl = document.getElementById("phone-view-url");
  const phoneStatus = document.getElementById("phone-status");
  let phoneSession = null;
  let phoneData = null;
  let phonePollTimer = null;

  document.getElementById("btn-phone")?.addEventListener("click", async () => {
    if (!phonePanel.classList.contains("hidden")) {
      phonePanel.classList.add("hidden");
      clearInterval(phonePollTimer);
      return;
    }
    try {
      // Reuse the session across open/close — re-minting would orphan a
      // phone that's mid-scan on the previous QR.
      if (!phoneData) {
        const res = await fetch("/api/phone/session");
        if (!res.ok) throw new Error("Could not create a phone session");
        phoneData = await res.json();
      }
      const d = phoneData;
      phoneSession = d.session;
      phonePanel.classList.remove("hidden");
      if (!d.https) {
        phoneQr.classList.add("hidden");
        phoneTitle.textContent = "One-time setup needed";
        phoneHint.textContent =
          "Phones only allow camera access on secure pages. In the project folder run ./install-phone.sh once, restart video-stream, and this panel becomes a QR code.";
        phoneViewUrl.textContent = "";
        phoneStatus.textContent = "";
        return;
      }
      phoneQr.classList.remove("hidden");
      phoneQr.src = d.qr_url;
      phoneTitle.textContent = "Scan with your phone (same Wi-Fi)";
      phoneViewUrl.textContent = d.view_url;
      phoneViewUrl.onclick = async () => {
        const ok = await copyText(d.view_url);
        toast(ok ? "View URL copied — paste into OBS as a Browser Source" : "Could not copy", ok);
      };
      document.getElementById("btn-phone-copy").onclick = phoneViewUrl.onclick;
      phoneStatus.textContent = "waiting for the phone…";
      clearInterval(phonePollTimer);
      phonePollTimer = setInterval(async () => {
        try {
          const s = await fetch("/api/phone/status").then((r) => r.json());
          const members = (s.sessions || {})[phoneSession] || 0;
          phoneStatus.textContent =
            members >= 2
              ? "✓ phone connected — the view URL is live"
              : members === 1
              ? "receiver open — waiting for the phone…"
              : "waiting for the phone…";
        } catch {}
      }, 2500);
    } catch (err) {
      toast(err.message || "Phone setup failed", false);
    }
  });

  // ── Unified chat: connect Twitch/Kick, show live status ─────────────
  const chatTwitch = document.getElementById("chat-twitch");
  const chatKick = document.getElementById("chat-kick");
  const chatStatusEl = document.getElementById("chat-status");
  const chatState = { twitch: null, kick: null };

  function renderChatStatus() {
    if (!chatStatusEl) return;
    const parts = [];
    ["twitch", "kick"].forEach((p) => {
      const s = chatState[p];
      if (s && s.status !== "off") {
        parts.push(`${p}: ${s.status}${s.detail ? ` (${s.detail})` : ""}`);
      }
    });
    chatStatusEl.textContent = parts.join(" · ");
  }

  ["twitch", "kick"].forEach((p) => {
    Bus.on(`chat_status_${p}`, (s) => {
      chatState[p] = s;
      // Retained status is the only memory of the channel after a reload —
      // refill a blank input so Connect's "empty field = disconnect" rule
      // can't silently drop a platform the operator never typed this session.
      const input = p === "twitch" ? chatTwitch : chatKick;
      if (input && s.status !== "off" && s.channel && !input.value.trim() && document.activeElement !== input) {
        input.value = s.channel;
      }
      renderChatStatus();
    });
  });

  document.getElementById("btn-chat-connect")?.addEventListener("click", async () => {
    const wanted = { twitch: chatTwitch?.value.trim(), kick: chatKick?.value.trim() };
    let acted = false;
    let failed = false;
    for (const platform of ["twitch", "kick"]) {
      try {
        if (wanted[platform]) {
          const res = await fetch("/api/chat/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform, channel: wanted[platform] }),
          });
          if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.detail || `${platform} connect failed`);
          }
          acted = true;
        } else if (chatState[platform] && chatState[platform].status !== "off") {
          await fetch("/api/chat/disconnect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform }),
          });
          acted = true;
        }
      } catch (err) {
        failed = true;
        toast(err.message || `${platform} connect failed`, false);
      }
    }
    if (acted && !failed) {
      toast("Chat updated — drop the chat overlay into OBS to show it on stream");
    }
  });

  // ── Setup panel: scan · propose · verify · settings ─────────────────
  const btnSetup = document.getElementById("btn-setup");
  const setupPanel = document.getElementById("setup-panel");
  const setupChecklist = document.getElementById("setup-checklist");
  const setupProposal = document.getElementById("setup-proposal");
  const setupSettings = document.getElementById("setup-settings");
  // A form whose only field is a lone text/password input implicit-submits on
  // Enter (full page reload) — the setup form must never navigate.
  setupSettings?.addEventListener("submit", (e) => e.preventDefault());
  let settingsLoaded = false;

  function authHeaders() {
    const token = localStorage.getItem("vs-token");
    const headers = { "Content-Type": "application/json" };
    if (token) headers["X-Auth-Token"] = token;
    return headers;
  }

  btnSetup?.addEventListener("click", () => {
    const open = setupPanel.classList.toggle("hidden") === false;
    btnSetup.setAttribute("aria-pressed", open ? "true" : "false");
    btnSetup.classList.toggle("on", open);
    if (open && !settingsLoaded) loadSettings();
  });

  async function loadSettings() {
    try {
      const res = await fetch("/api/settings", { headers: authHeaders() });
      if (res.status === 401) {
        renderTokenPrompt();
        return;
      }
      if (!res.ok) throw new Error("Could not load settings");
      const data = await res.json();
      renderSettingsForm(data.fields);
      settingsLoaded = true;
    } catch (err) {
      toast(err.message || "Settings unavailable", false);
    }
  }

  function renderTokenPrompt() {
    setupSettings.innerHTML = `
      <div class="settings-row">
        <label for="vs-token-input">Auth token</label>
        <input type="password" id="vs-token-input" placeholder="X-Auth-Token" />
        <button type="button" class="btn btn-sm" id="vs-token-save">Unlock</button>
      </div>`;
    document.getElementById("vs-token-save").addEventListener("click", () => {
      localStorage.setItem(
        "vs-token",
        document.getElementById("vs-token-input").value.trim()
      );
      loadSettings();
    });
    document.getElementById("vs-token-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") document.getElementById("vs-token-save").click();
    });
  }

  function renderSettingsForm(fields) {
    setupSettings.innerHTML = "";
    fields.forEach((f) => {
      const row = document.createElement("div");
      row.className = "settings-row";
      const id = `set-${f.key}`;
      const input =
        f.kind === "bool"
          ? `<input type="checkbox" id="${id}" data-key="${f.key}" data-kind="bool" ${f.value ? "checked" : ""} />`
          : `<input type="${f.secret ? "password" : "text"}" id="${id}" data-key="${f.key}" data-kind="${f.kind}"
               value="${esc(String(f.value ?? ""))}" placeholder="${esc(String(f.default ?? ""))}" />`;
      row.innerHTML = `<label for="${id}" title="${esc(f.help)}">${esc(f.key)}</label>${input}`;
      setupSettings.appendChild(row);
    });
    const actions = document.createElement("div");
    actions.className = "settings-row settings-save";
    actions.innerHTML = `<span class="muted mono">changes apply live · director restarts if running</span>
      <button type="button" class="btn btn-sm btn-signal" id="btn-save-settings">Save settings</button>`;
    setupSettings.appendChild(actions);
    document.getElementById("btn-save-settings").addEventListener("click", saveSettings);
  }

  async function saveSettings() {
    const updates = {};
    for (const el of setupSettings.querySelectorAll("input[data-key]")) {
      const kind = el.getAttribute("data-kind");
      const key = el.getAttribute("data-key");
      let value = kind === "bool" ? el.checked : el.value;
      if (kind === "int" || kind === "float") {
        // Cleared field = "use the default" — that's what the placeholder shows.
        const raw = el.value.trim() === "" ? el.placeholder : el.value;
        value = kind === "int" ? parseInt(raw, 10) : parseFloat(raw);
        if (Number.isNaN(value)) {
          toast(`${key}: not a number`, false);
          return;
        }
      }
      updates[key] = value;
    }
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(updates),
      });
      const body = await res.json().catch(() => ({}));
      if (res.status === 401) {
        renderTokenPrompt();
        throw new Error("Auth token required");
      }
      if (!res.ok) throw new Error(body.detail || "Save failed");
      renderSettingsForm(body.fields);
      toast("Settings saved");
    } catch (err) {
      toast(err.message || "Save failed", false);
    }
  }

  document.getElementById("btn-verify")?.addEventListener("click", async () => {
    setupChecklist.innerHTML = `<div class="muted mono">checking the rig…</div>`;
    try {
      const res = await fetch("/api/setup/verify");
      if (!res.ok) throw new Error("Verify failed");
      const data = await res.json();
      setupChecklist.innerHTML = "";
      data.checks.forEach((c) => {
        const row = document.createElement("div");
        row.className = `check-row ${c.ok ? "ok" : "bad"}`;
        row.innerHTML = `<span class="check-mark">${c.ok ? "✓" : "✗"}</span>
          <span class="check-name">${esc(c.name)}</span>
          <span class="check-details mono">${esc(c.details)}</span>`;
        setupChecklist.appendChild(row);
      });
      toast(`Rig check: ${data.passed}/${data.total} passed`, data.passed === data.total);
    } catch (err) {
      setupChecklist.innerHTML = "";
      toast(err.message || "Verify failed", false);
    }
  });

  document.getElementById("btn-generate")?.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/setup/generate", { method: "POST" });
      if (!res.ok) throw new Error("Generate failed");
      const data = await res.json();
      setupProposal.classList.remove("hidden");
      const pairs = Object.entries(data.proposal);
      if (!pairs.length) {
        setupProposal.innerHTML = `<div class="muted mono">${
          data.obs_reachable
            ? "No matching scenes found — name OBS scenes like “Cam 0”, “Cam 1”…"
            : "OBS unreachable — start OBS and enable its WebSocket server first"
        }</div>`;
        return;
      }
      setupProposal.innerHTML = `
        <span class="eyebrow">Proposed scene map</span>
        <code class="mono">${esc(data.scene_map_string)}</code>
        <button type="button" class="btn btn-sm btn-signal" id="btn-apply-map">Apply</button>`;
      document.getElementById("btn-apply-map").addEventListener("click", async () => {
        const res2 = await fetch("/api/settings", {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ obs_scene_map: data.scene_map_string }),
        });
        if (res2.status === 401) {
          renderTokenPrompt();
          toast("Auth token required", false);
          return;
        }
        toast(res2.ok ? "Scene map applied" : "Apply failed", res2.ok);
      });
    } catch (err) {
      toast(err.message || "Generate failed", false);
    }
  });

  // ── Studio Bus: live push replaces polling while connected ──────────
  Bus.on("director", (d) => {
    directorState = {
      enabled: !!d.enabled,
      active: d.active ?? null,
      active_rule: d.active_rule ?? null,
      dry_run: !!d.dry_run,
      obs_connected: !!d.obs_connected,
      last_decision: d.last_decision ?? null,
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
    loadChaos(); // retries a failed first load; guarded against duplicates
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
