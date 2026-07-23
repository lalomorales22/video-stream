"""Rig Link: fold remote instances' motion signals into the local director.

The multi-machine rig runs one video-stream per camera machine, but the
director lives on the OBS box — which can't see motion happening on cameras
plugged into OTHER machines. Each instance exports its scores at
``GET /api/signals``; PeerLink threads on the director's instance poll their
peers at the director's cadence and merge the results into the signal map as
``motion:<peer>:<index>``, so rules can cut on remote movement:

    { "source": "motion:studio-laptop:1", "scene": "Wide", "threshold": 0.05 }

Polling (not the bus) is deliberate: one tiny JSON GET at 4 Hz per peer is
nothing on a LAN, returns scores + camera states atomically, survives peer
restarts with zero session state, and ``?enable=1`` doubles as the remote
"please score motion" switch. A dead peer's last signals simply go stale and
fall out of the director's freshness window — no special-casing needed.
"""

from __future__ import annotations

import re
import threading
import time

POLL_INTERVAL = 0.25  # matches DirectorConfig.interval
ERROR_BACKOFF = 3.0


def parse_peers(raw: str) -> list[tuple[str, str]]:
    """Parse '--peers' input into [(name, base_url)].

    Accepts comma-separated entries of ``name=host[:port]`` or bare
    ``host[:port]`` (named after the host). Scheme defaults to http.
    """
    peers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, url = part.partition("=")
            name = name.strip()
        else:
            name, url = "", part
        url = url.strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        url = url.rstrip("/")
        if not name:
            name = re.sub(r"^https?://", "", url).partition(":")[0]
        # Lowercase so signal keys match Rule.key normalization — the same
        # treatment AudioMeterListener gives audio input names.
        name = re.sub(r"[^\w.-]", "-", name.lower()) or "peer"
        base = name
        n = 2
        while name in seen:  # duplicate names would collide in the signal map
            name = f"{base}{n}"
            n += 1
        seen.add(name)
        peers.append((name, url))
    return peers


class PeerLink(threading.Thread):
    """Polls one peer instance for motion signals. Retries forever."""

    def __init__(self, name: str, base_url: str) -> None:
        super().__init__(name=f"peer-{name}", daemon=True)
        self.peer_name = name
        self.base_url = base_url
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._signals: dict[str, tuple[float, float]] = {}
        self._cameras: list[dict] = []
        self.ok = False
        self.error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import httpx

        with httpx.Client(timeout=2.0) as client:
            while not self._stop.is_set():
                try:
                    resp = client.get(
                        f"{self.base_url}/api/signals", params={"enable": 1}
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    now = time.monotonic()
                    signals = {
                        f"motion:{self.peer_name}:{index}": (float(score), now)
                        for index, score in (data.get("motion") or {}).items()
                    }
                    with self._lock:
                        self._signals = signals
                        self._cameras = data.get("cameras") or []
                    self.ok = True
                    self.error = None
                    self._stop.wait(POLL_INTERVAL)
                except Exception as exc:
                    # Old signals stay put and simply go stale — the director's
                    # freshness window drops them within ~2s.
                    self.ok = False
                    self.error = str(exc)
                    self._stop.wait(ERROR_BACKOFF)

    def signals(self) -> dict[str, tuple[float, float]]:
        with self._lock:
            return dict(self._signals)

    def status(self) -> dict:
        with self._lock:
            cameras = list(self._cameras)
        return {
            "name": self.peer_name,
            "url": self.base_url,
            "ok": self.ok,
            "error": self.error,
            "cameras": cameras,
        }


class PeerManager:
    """One PeerLink per configured peer; merged view for the director."""

    def __init__(self, peers: list[tuple[str, str]]) -> None:
        self.links = [PeerLink(name, url) for name, url in peers]

    def start(self) -> None:
        for link in self.links:
            link.start()

    def stop(self) -> None:
        for link in self.links:
            link.stop()

    def signals(self) -> dict[str, tuple[float, float]]:
        merged: dict[str, tuple[float, float]] = {}
        for link in self.links:
            merged.update(link.signals())
        return merged

    def status(self) -> list[dict]:
        return [link.status() for link in self.links]
