"""Non-blocking motion for the control service: one move at a time, as a background task.

    mgr = MotionManager(arm)
    m = await mgr.start("top")     # returns at once: {"motion_id": "m-1", "state": "moving", ...}
    await mgr.stop()               # cancels the move; motion.move_to holds where the arm is
    async for ev in mgr.subscribe(): ...   # state changes and progress

Rules (see docs/ARCHITECTURE.md):
  - Only views from views.json can be reached (whitelist). "via" views are passed first.
  - A second start while a move runs is rejected (409 busy). Only stop cancels.
  - Watchdog: joint data older than STALE_S during a move ends it without further writes
    (the servos keep the last waypoint) and reports state "error".
  - Timeout per leg: the move is cancelled, the arm holds, state "timeout".
"""

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import motion
from station_arm import gripper_id

log = logging.getLogger("motion_manager")

STALE_S = 0.5          # joint data older than this stops a move
LEG_TIMEOUT_S = 15.0   # one leg (view to view) must finish within this
AT_VIEW_STEPS = 60     # every joint within this many steps of a view = "at" that view
PROGRESS_EVERY_S = 0.2 # progress events at most this often
MAX_HISTORY = 50

FINAL_STATES = ("reached", "stalled", "timeout", "stopped", "error")


class ManagerError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail

    def body(self) -> dict:
        return {"error": self.code, "detail": self.detail}


@dataclass
class Motion:
    motion_id: str
    view: str
    path: list
    state: str = "moving"
    progress: float = 0.0
    started: float = field(default_factory=time.time)
    ended: float | None = None
    error: str | None = None
    max_error: float | None = None

    def public(self) -> dict:
        return {
            "motion_id": self.motion_id, "view": self.view, "path": self.path,
            "state": self.state, "progress": round(self.progress, 3),
            "started": self.started, "ended": self.ended,
            "duration_s": round((self.ended or time.time()) - self.started, 2),
            "error": self.error,
        }


def load_view_records(path: Path) -> dict[str, dict]:
    """views.json -> {name: {"joints": {id: pos}, "description", "aliases", "via", "snapshot"}}"""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())["views"]
    out = {}
    for name, v in raw.items():
        out[name] = {
            "joints": {int(k): int(p) for k, p in v["joints"].items()},
            "description": v.get("description", ""),
            "aliases": list(v.get("aliases", [])),
            "via": list(v.get("via", [])),
            "snapshot": v.get("snapshot"),
        }
    return out


def _circ(a: int, b: int) -> int:
    d = abs(a - b) % motion.FULL
    return min(d, motion.FULL - d)


class MotionManager:
    def __init__(self, arm, views_path: Path | None = None, events_path: Path | None = None,
                 move_kwargs: dict | None = None, stale_s: float = STALE_S,
                 leg_timeout_s: float = LEG_TIMEOUT_S):
        self.arm = arm
        self.views_path = views_path or motion.VIEWS_PATH
        self.events_path = events_path
        self.move_kwargs = dict(move_kwargs or {})
        self.stale_s = stale_s
        self.leg_timeout_s = leg_timeout_s
        self.current: Motion | None = None
        self.history: dict[str, Motion] = {}
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._subs: set[asyncio.Queue] = set()
        self._last_progress_emit = 0.0

    # -- views --------------------------------------------------------------

    def views(self) -> dict[str, dict]:
        return load_view_records(self.views_path)

    def views_public(self) -> list[dict]:
        """Views without joint values: what MCP, dashboard and Claude may see."""
        return [{"name": n, "description": v["description"], "aliases": v["aliases"],
                 "via": v["via"], "snapshot": v["snapshot"]} for n, v in self.views().items()]

    def nearest_view(self, joints: dict[int, int] | None = None) -> dict | None:
        try:
            joints = joints or self.arm.joints(self.stale_s)
        except RuntimeError:
            return None
        if not joints:
            return None
        grip = gripper_id(joints)
        best = None
        for name, v in self.views().items():
            ids = [m for m in v["joints"] if m in joints and m != grip]
            if not ids:
                continue
            dist = max(_circ(joints[m], v["joints"][m]) for m in ids)
            if best is None or dist < best["distance"]:
                best = {"view": name, "distance": dist, "at": dist <= AT_VIEW_STEPS}
        return best

    # -- state --------------------------------------------------------------

    @property
    def moving(self) -> bool:
        return self._task is not None and not self._task.done()

    def joint_age_s(self) -> float | None:
        stamp = getattr(self.arm, "latest_stamp", None)
        return None if stamp is None else round(time.monotonic() - stamp, 3)

    def status(self) -> dict:
        try:
            joints = self.arm.joints(self.stale_s)
        except RuntimeError as e:
            joints, err = None, str(e)
        else:
            err = None
        if self.moving:
            state = "moving"
        elif self.current is None:
            state = "idle"
        else:
            state = "holding" if getattr(self.arm, "torque", True) else "idle"
        return {
            "state": state,
            "motion": self.current.public() if self.current else None,
            "nearest_view": self.nearest_view(joints) if joints else None,
            "joints": joints,
            "joints_error": err,
            "last_error": self.last_error,
            "bus": getattr(self.arm, "bus_serial", None),
        }

    def get_motion(self, motion_id: str) -> dict:
        m = self.history.get(motion_id)
        if m is None:
            raise ManagerError(404, "unknown_motion", f"no motion {motion_id!r}")
        return m.public()

    # -- events -------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def _emit(self, kind: str, m: Motion | None = None, log_file: bool = True, **extra):
        ev = {"t": round(time.time(), 3), "kind": kind}
        if m is not None:
            ev.update({"motion_id": m.motion_id, "view": m.view, "state": m.state,
                       "progress": round(m.progress, 3)})
            if m.error:
                ev["error"] = m.error
            ev["text"] = f"{m.view}: {m.state}" + (f" ({m.error})" if m.error else "")
        ev.update(extra)
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()   # drop the oldest for a slow reader
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(ev)
        if log_file and self.events_path is not None:
            try:
                with open(self.events_path, "a") as f:
                    f.write(json.dumps(ev) + "\n")
            except OSError:
                log.exception("cannot write %s", self.events_path)

    # -- commands -----------------------------------------------------------

    async def start(self, view: str) -> dict:
        if not isinstance(view, str) or not view or len(view) > 64:
            raise ManagerError(400, "invalid_request", "view must be a non-empty string (max 64 chars)")
        if self.moving:
            raise ManagerError(409, "busy", f"already moving to {self.current.view!r}; stop first")
        views = self.views()
        if view not in views:
            raise ManagerError(404, "unknown_view", f"unknown view {view!r}; known: {sorted(views)}")
        path = [v for v in views[view]["via"] if v != view] + [view]
        missing = [v for v in path if v not in views]
        if missing:
            raise ManagerError(500, "bad_views_file", f"view {view!r} goes via unknown views {missing}")
        try:
            self.arm.joints(self.stale_s)
        except RuntimeError as e:
            raise ManagerError(409, "not_ready", str(e))

        m = Motion(motion_id=f"m-{next(self._ids)}", view=view, path=path)
        self.current = m
        self.history[m.motion_id] = m
        while len(self.history) > MAX_HISTORY:
            self.history.pop(next(iter(self.history)))
        self._emit("motion", m)
        targets = [views[v]["joints"] for v in path]
        self._task = asyncio.create_task(self._run(m, targets))
        return m.public()

    async def stop(self) -> dict:
        """Cancel a running move (motion.move_to holds the arm) or hold where it is."""
        if self.moving:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        else:
            try:
                await motion.hold(self.arm)
            except RuntimeError as e:
                self.last_error = f"stop: cannot hold, {e}"
                log.warning(self.last_error)
        return self.status()

    async def shutdown(self):
        if self.moving:
            await self.stop()

    # -- the move itself ----------------------------------------------------

    def _tick(self, m: Motion, leg: int, legs: int):
        def on_tick(s: float):
            self.arm.joints(self.stale_s)      # watchdog: raises RuntimeError on stale data
            m.progress = (leg + s) / legs
            now = time.monotonic()
            if now - self._last_progress_emit >= PROGRESS_EVERY_S:
                self._last_progress_emit = now
                self._emit("motion", m, log_file=False)
        return on_tick

    async def _run(self, m: Motion, targets: list[dict[int, int]]):
        legs = len(targets)
        try:
            for leg, target in enumerate(targets):
                coro = motion.move_to(self.arm, target, on_tick=self._tick(m, leg, legs), **self.move_kwargs)
                try:
                    result = await asyncio.wait_for(coro, self.leg_timeout_s)
                except asyncio.TimeoutError:
                    # wait_for cancelled move_to, which wrote a hold
                    m.state, m.error = "timeout", f"leg {leg + 1}/{legs} to {m.path[leg]!r} took over {self.leg_timeout_s:g}s"
                    return
                if not result.get("ok"):
                    m.state, m.error = "stalled", result.get("error")
                    return
            m.progress, m.state = 1.0, "reached"
        except asyncio.CancelledError:
            m.state = "stopped"
            raise
        except RuntimeError as e:
            # stale joint data or a lost bus: write nothing more, the servos keep the last waypoint
            m.state, m.error = "error", str(e)
        except Exception as e:  # noqa: BLE001 - report anything else instead of dying silently
            log.exception("move %s failed", m.motion_id)
            m.state, m.error = "error", f"{type(e).__name__}: {e}"
        finally:
            m.ended = time.time()
            if m.error:
                self.last_error = f"{m.motion_id}: {m.error}"
            self._emit("motion", m)
