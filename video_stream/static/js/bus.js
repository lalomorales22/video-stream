/* Studio Bus client: one WebSocket to /ws, an event dispatch table, and a
   linear-backoff reconnect (pattern from february11's dashboard socket).

   Usage:  Bus.on("director", (payload) => { ... });
   Meta events "_open" / "_close" fire on connection changes. The server
   re-sends retained state right after connect, so handlers hydrate instantly.
   Protocol-relative URL so the same file works if HTTPS ever lands. */
window.Bus = (() => {
  const handlers = new Map();
  let delay = 500;
  let connected = false;
  let socket = null;

  function fire(type, payload) {
    (handlers.get(type) || []).forEach((fn) => {
      try {
        fn(payload);
      } catch (err) {
        console.error("[bus]", type, err);
      }
    });
  }

  function reschedule() {
    setTimeout(connect, delay);
    delay = Math.min(5000, delay + 500);
  }

  function connect() {
    let ws;
    try {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
    } catch {
      reschedule();
      return;
    }
    socket = ws;
    ws.onopen = () => {
      connected = true;
      delay = 500;
      fire("_open");
    };
    ws.onmessage = (e) => {
      try {
        const { type, payload } = JSON.parse(e.data);
        fire(type, payload);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      if (connected) fire("_close");
      connected = false;
      reschedule();
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch {}
    };
  }

  connect();

  return {
    on(type, fn) {
      if (!handlers.has(type)) handlers.set(type, []);
      handlers.get(type).push(fn);
    },
    // Publish INTO the bus (server allowlists which events clients may emit;
    // used by Avatar Sync so one tracking page can drive every OBS instance).
    publish(type, payload) {
      if (connected && socket && socket.readyState === 1) {
        try {
          socket.send(JSON.stringify({ publish: { event: type, payload } }));
        } catch {}
      }
    },
    get connected() {
      return connected;
    },
  };
})();
