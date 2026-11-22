"""Web UI for the camera arm: capture views (M1) and move between them (M2).

    python capture_server.py                    # then open http://localhost:8890
    python capture_server.py --bus 5B79016329   # pick the follower explicitly
    python capture_server.py --sim --port 8891  # simulated arm: no Station, no camera

/         capture: Go limp, pose the follower by hand, Hold here, check the live picture, click Save.
          Writes the same views.json + views/<name>.jpg as capture_view.py.
/control  control: click a saved view and the arm moves there (motion.go_to_view); STOP holds it where it is.
/api/still.jpg  one camera JPEG for the MCP server (mcp_server.py), only while the arm holds still; read-only.
/api/frame      preview of framing a box from the current image (framing.py); dry_run only, never moves.

Read-only extras for the control page: /api/telemetry (per-joint table), /api/pose (stick-figure points from
fk.py), /api/history (last moves), /api/settings (GET), /api/extras (is the MCP server up).
Commands beyond moving: POST /api/unlink (Station stops mirroring a leader onto the arm), POST /api/settings
(runtime toggles of the motion layer: BLEND, CLEAR_HEIGHT_M, SPEED_TURBO; reset when this server restarts).

All arm motion and torque goes through motion.py (go_to_view, hold, engage, release, describe).

Who may command the arm: everything that actuates (move start, Hold here, Go limp, arm selection) runs on the
asyncio loop as ONE command at a time (State.command). A running move also counts as the owner. STOP is the
exception: it never queues. It bumps the stop generation, cancels the move, waits for it to unwind, then holds
and confirms the hold. There is only ever ONE such cleanup: a STOP that arrives while one is running joins it,
and no command is admitted until it has finished. Every command carries two generations, the one the page saw
when the user clicked and the one from when its HTTP request arrived, and is refused if a STOP came in since
either, so a click from before a STOP can never start afterwards.
"""

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import framing
import motion
from camera import Camera, list_camera_queues
from capture_view import GUIDED_VIEWS, NAME_RE, SNAPSHOT_DIR, VIEWS_PATH, capture, load_views, save_views
from station_arm import Arm, format_joints, logger

ROOT = Path(__file__).parent
PAGES = {"/": ROOT / "capture_ui.html", "/index.html": ROOT / "capture_ui.html", "/control": ROOT / "control_ui.html"}

# max_speed for go_to_view, steps per second
SPEEDS = {"slow": motion.SPEED_SLOW, "normal": motion.SPEED_NORMAL, "fast": motion.SPEED_FAST,
          "turbo": motion.SPEED_TURBO}
DEFAULT_SPEED = "slow"
STOP_WAIT_S = 1.0   # how long STOP waits for the cancelled move / command to unwind before it holds anyway
TORQUE_WAIT_S = 1.5 # ...and for a Hold here / Go limp in progress to finish (short, and nothing is moving then)
STOP_ANSWER_S = 6.0 # /api/stop answers within this, with "hold": "unconfirmed" if the cleanup has not finished
HOLD_SETTLE_S = 0.2 # a hold counts as confirmed once the goal registers have not changed for this long
HOLD_CONFIRM_S = 1.5  # ...and we give up waiting for that after this long
STALE_CLICK = "the arm was stopped since this page last updated; check the arm and click again"
DRAIN_S = 3.0       # shutdown: how long to wait for the stopped move task to end
WHERE_TTL_S = 1.0   # motion.describe() is asked at most this often, and never while a move is running
FRAME_POSE_TOL = 20    # /api/frame: steps a joint may differ from the image's pose before the image counts as stale
STILL_MAX_AGE_S = 1.0  # /api/still.jpg: a camera frame older than this counts as no picture
CAMERA_LIVE_S = 2.0    # the status strip calls the camera live while its newest frame is younger than this
WATCH_S = 0.5          # how often the server itself checks whether arm data is still arriving
BACK_NOTE_S = 120      # "the arm is back" is said for this long after its data returned
HISTORY_ROWS = 15
MCP_URL = "http://127.0.0.1:8200/mcp"

# Runtime toggles: attributes of the motion module (docs/MOTION.md, "Speeds and blending"). Changed only
# through State.change_settings (command owner, never while moving); back to these defaults on restart.
SETTINGS = {
    "DIRECT": {"type": "bool", "label": "Go straight from view to view",
               "help": "Between two checked views all joints move together in one straight move, without going "
                       "up to the transit pose, whenever the geometry model shows that path never dips below "
                       "its two end views. Off: every move goes up through transit. Views marked "
                       "\"direct\": false (home) always go through transit."},
    "ARC": {"type": "bool", "locked": True, "label": "Lift over when straight would dip",
            "help": "For pairs whose straight path would dip (left and right, for example): take the same "
                    "path lifted in the middle when it passes the geometry check. Otherwise use transit. "
                    "Watch the first run of each pair."},
    "BLEND": {"type": "bool", "label": "Blend the turn into rise and descent",
              "help": "At Fast and Turbo the arm starts turning while it is still rising, and starts coming down "
                      "while it is still turning, but only high above the desk. Off: strictly rise, then turn, "
                      "then descend."},
    "CLEAR_HEIGHT_M": {"type": "number", "min": 0.15, "max": 0.40, "step": 0.01, "unit": "m",
                       "label": "Clear height for blending",
                       "help": "How high above the desk the forearm, wrist, gripper and camera must be (by the "
                               "geometry model) before turning may overlap. Higher is more careful."},
    "SPEED_TURBO": {"type": "int", "min": 1000, "max": 3400, "step": 100, "unit": "steps/s",
                    "label": "Turbo speed",
                    "help": "Top speed of rise and turn at Turbo. 2000 is about half a turn per second; the "
                            "servos' own limit is about 3400."},
}
SETTINGS = {k: v for k, v in SETTINGS.items() if hasattr(motion, k)}
SETTING_DEFAULTS = {k: getattr(motion, k) for k in SETTINGS}
SPEED_ABOUT = {  # one plain line per speed for the page; {n} is the current steps/s
    "slow": "Slow: about 18 degrees a second. Rise, then turn, then descend, one after the other.",
    "normal": "Normal: about 35 degrees a second, same order: rise, then turn, then descend.",
    "fast": "Fast: rise and turn at about 70 degrees a second, the descent at Normal speed. "
            "With blending on, the turn overlaps while the arm is high above the desk.",
    "turbo": "Turbo: rise and turn as fast as the servos go ({n} steps a second), the descent at Fast speed. "
             "Keep the desk clear and a hand near STOP.",
}
JOINT_NAMES = {1: "base turn", 2: "shoulder", 3: "elbow", 4: "pitch", 5: "forearm roll", 6: "wrist pitch",
               7: "wrist roll", 8: "gripper (camera)"}
WARN_AT = {"temp": 55, "load": 600, "current": 200}  # soft colour warnings in the joint table


def settings_view() -> dict:
    return {k: {**spec, "value": getattr(motion, k), "default": SETTING_DEFAULTS[k]} for k, spec in SETTINGS.items()}


def check_settings(req: dict) -> dict:
    """Validated {name: value} from a POST /api/settings body. Raises Refused(400) on anything odd."""
    if not isinstance(req, dict):
        raise Refused("send a JSON object", 400)
    changes = dict(SETTING_DEFAULTS) if req.get("defaults") is True else {}
    for name, value in req.items():
        if name == "defaults":
            continue
        spec = SETTINGS.get(name)
        if spec is None:
            raise Refused(f"unknown setting {name!r}; known: {list(SETTINGS)}", 400)
        if spec["type"] == "bool":
            if not isinstance(value, bool):
                raise Refused(f"{name} must be true or false", 400)
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                raise Refused(f"{name} must be a number", 400)
            if not spec["min"] <= value <= spec["max"]:
                raise Refused(f"{name} must be between {spec['min']} and {spec['max']} (got {value})", 400)
            if spec["type"] == "int":
                if value != int(value):
                    raise Refused(f"{name} must be a whole number", 400)
                value = int(value)
            else:
                value = round(float(value), 3)
        if spec.get("locked") and value != SETTING_DEFAULTS[name]:
            raise Refused(f"{spec['label']} is always on", 400)
        changes[name] = value
    if not changes:
        raise Refused(f"nothing to change; send some of {list(SETTINGS)} or {{\"defaults\": true}}", 400)
    return changes


def clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"

NO_CAMERA_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360"><rect width="640" height="360"/>'
                 b'<text x="320" y="187" fill="#8a92a3" font-family="system-ui, sans-serif" font-size="20" '
                 b'text-anchor="middle">no camera</text></svg>')


class Refused(Exception):
    """A request we won't carry out right now; the message goes to the page."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def pick_bus(requested: str, buses: list[str], torqued: list[str], views: dict) -> str:
    """Which arm we drive. Never guesses: with several arms and no single answer it refuses.

    requested: --bus (a serial, or "auto").  buses: serials Station sees.  torqued: those holding.
    views: views.json's "views"; the arm they were captured on is the follower.
    """
    if not buses:
        raise RuntimeError("Station sees no arm; check USB and the Station web UI")
    if requested != "auto":
        if requested not in buses:
            raise RuntimeError(f"arm {requested} is not connected; Station sees {', '.join(buses)}")
        return requested
    if len(buses) == 1:
        return buses[0]
    recorded = views.get(motion.TRANSIT, {}).get("bus")
    if recorded not in buses:
        known = {v.get("bus") for v in views.values()} & set(buses)
        recorded = known.pop() if len(known) == 1 else None
    holding = torqued[0] if len(torqued) == 1 else None
    if recorded and holding and recorded != holding:
        raise RuntimeError(f"cannot tell which arm is the follower: the saved views were captured on {recorded}, "
                           f"but {holding} is the one holding torque. Pass --bus <serial>")
    if recorded or holding:
        return recorded or holding
    raise RuntimeError(f"several arms are connected ({', '.join(buses)}) and none of them is the one the saved "
                       "views were captured on. Pass --bus <serial>")


class State:
    """Lives on the asyncio loop thread; HTTP threads only submit coroutines via `call`."""

    def __init__(self, loop: asyncio.AbstractEventLoop, sim: bool = False):
        self.loop = loop
        self.sim = sim
        self.arm: Arm | None = None
        self.cams: dict[str, Camera] = {}
        self.cam_queue: str | None = None
        self.capturing = False
        self.views_path = VIEWS_PATH
        # Command ownership (see the module docstring).
        self.owner = asyncio.Lock()            # held by the one command that may touch the arm or views.json
        self.command_task: asyncio.Task | None = None  # the task holding `owner`, so STOP can cancel it
        self.command_atomic = False            # ...unless it is a torque command, which STOP lets finish
        self.preempted: set[asyncio.Task] = set()      # command tasks STOP has cancelled
        self.stop_task: asyncio.Task | None = None     # the one Stop cleanup; later STOPs join it
        self.torque_doubt: str | None = None   # a Go limp that could not be confirmed: no move until it is cleared
        self.gen = 0                           # stop generation: every STOP bumps it
        self._gen_lock = threading.Lock()
        self.closing = False                   # shutting down: no new work
        self.move_task: asyncio.Task | None = None
        self.move_view: str | None = None
        self.move_outcome: dict | None = None  # how the last move started here ended (result, stopped or crashed)
        self.hold_state: str | None = None     # last stop/abort: "confirmed" | "unconfirmed" | "limp" (nothing to hold)
        self.where: dict | None = None         # last motion.describe()
        self.where_at = 0.0
        self.where_gen = 0                     # bumped whenever the cached answer stops being true
        self.where_task: asyncio.Task | None = None
        # For the status strip: is arm data arriving (wall-clock times), and what the last moves did.
        self.data_seen_at: float | None = None   # last time the joints could be read
        self.data_lost_at: float | None = None   # set while they cannot: when they last could
        self.data_back_at: float | None = None   # when data last returned after being lost
        self.data_reason: str | None = None      # why not, in the arm's words
        self.move_started = 0.0
        self.move_speed: tuple[str, float] | None = None
        self.history: deque = deque(maxlen=2 * HISTORY_ROWS)  # results of moves started here (the sim's history)
        self._fk = None                          # the geometry model, imported on first use (False: unavailable)
        self._tips: tuple | None = None          # (key, {view: jaw tip xyz}) landmarks for the arm picture
        self._mcp: tuple[float, bool] = (0.0, False)
        self.last_link = {"linked": None, "leader": None, "text": None}
        self.director = None                     # camera_director.jobs.Director with --director, else off

    def call(self, coro, timeout: float = 15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def start(self, server: str, bus: str):
        self.arm = await Arm.connect(server, bus="any")
        buses = list(self.arm.all_buses())
        try:
            self.arm.bus_serial = pick_bus(bus, buses, [s for s in buses if self.arm._torque_on(s)],
                                           load_views(self.views_path)["views"])
        except RuntimeError:
            self.arm.close()
            raise
        for q in await list_camera_queues(self.arm.client):
            self.cams[q] = Camera(self.arm.client, q)
        await asyncio.sleep(1.5)
        # Default to the camera with the most detailed picture (a covered lens gives tiny JPEGs).
        if self.cams:
            self.cam_queue = max(self.cams, key=lambda q: len(self.cams[q].jpeg or b""))

    async def start_sim(self):
        from sim_arm import SimArm
        if not motion.view_names():
            self.views_path = motion.VIEWS_PATH = ROOT / "views.example.json"
        motion.LOG_PATH = Path(os.devnull)  # motion_log.jsonl is for tuning limits from real-arm runs only
        self.arm = SimArm()  # starts at the saved home view

    # ---- command ownership ----

    @property
    def moving(self) -> bool:
        return self.move_task is not None and not self.move_task.done()

    def bump(self) -> int:
        """A STOP arrived (any thread): from now on, commands received before it must not start."""
        with self._gen_lock:
            self.gen += 1
            return self.gen

    @property
    def stopping(self) -> bool:
        """A Stop cleanup is running (cancel, unwind, hold, confirm): nothing else is admitted."""
        return self.stop_task is not None and not self.stop_task.done()

    async def command(self, body, gen: int | None = None, click: int | None = None, idle: bool = True,
                      atomic: bool = False):
        """Run `body()` as the single owner of the arm and views.json.

        gen:    the stop generation when the HTTP request arrived; pass it for anything that actuates.
        click:  the stop generation the page had seen when the user clicked.
        idle:   refuse while a move is running.
        atomic: a torque command. STOP does not interrupt it half-way (it would no longer know whether
                the arm is energised); it waits for it instead. Everything else is cancelled by STOP.
        """
        def admit():
            if self.closing:
                raise Refused("the server is shutting down", 503)
            if self.stopping or (gen is not None and gen != self.gen):
                raise Refused("stopped before it started (STOP was pressed)")
            if click is not None and click != self.gen:
                raise Refused(STALE_CLICK)

        admit()  # on arrival: a command that meets a STOP in progress is refused, not queued behind it
        if self.director is not None:  # a person's command outranks a running director job
            self.director.preempt_unless_own("manual control took over")
        async with self.owner:
            admit()  # and again once it owns the arm: a STOP may have come while it waited
            if idle and self.moving:
                raise Refused(f"moving to {self.move_view}: press STOP first")
            self.command_task, self.command_atomic = asyncio.current_task(), atomic
            try:
                return await body()
            except asyncio.CancelledError:
                if asyncio.current_task() in self.preempted:  # STOP took the arm away from this command
                    raise Refused("interrupted by STOP") from None
                raise
            finally:
                self.preempted.discard(asyncio.current_task())
                self.command_task = None

    def motion_state(self) -> dict:
        """motion.status, completed with what this server knows about the move it started."""
        m = dict(motion.status)
        m.setdefault("hold", None)
        if self.moving:
            if not m.get("moving"):  # task created, go_to_view not running yet
                m.update(moving=True, view=self.move_view, phase="check", phase_index=0, phase_count=0,
                         progress=0.0, error=None, route=[], hold=None)
        elif self.move_outcome is not None:
            m.update(moving=False, phase=None, error=self.move_outcome.get("error"), last_result=self.move_outcome,
                     hold=self.move_outcome.get("hold"))
        return m

    # ---- where is the arm (motion.describe, cached) ----

    def _forget_where(self):
        self.where, self.where_at = None, 0.0
        self.where_gen += 1

    def refresh_where(self):
        """Loop thread: ask motion where the arm is, unless a move is running or we are already asking."""
        if not self.moving and (self.where_task is None or self.where_task.done()):
            self.where_task = asyncio.create_task(self._describe())

    async def _describe(self):
        while not self.moving:
            gen = self.where_gen
            try:
                answer = await motion.describe(self.arm)
            except Exception as e:  # stale data, arm unplugged, Station not answering
                answer = {"ok": False, "error": str(e) or type(e).__name__}
            if gen == self.where_gen:  # else the arm was moved or its torque changed meanwhile: ask again
                self.where, self.where_at = answer, time.monotonic()
                return

    def cards(self, data: dict, where: dict | None = None) -> list[dict]:
        """The views a user can click, in motion's order (the transit pose is not one of them)."""
        try:
            limit_notes = motion.check_views(self.arm.ranges())
        except Exception:  # no calibration / no arm data: describe() reports that
            limit_notes = {}
        descriptions = motion.view_descriptions()
        return [{"name": n, "description": descriptions.get(n, ""),
                 "snapshot": bool(data["views"].get(n, {}).get("snapshot")),
                 "captured_at": data["views"].get(n, {}).get("captured_at"),
                 "checked": motion.view_checked(n), "limit_notes": limit_notes.get(n, []),
                 "first_run": self.first_run_reason(n, where)}
                for n in motion.view_names()]

    @staticmethod
    def first_run_reason(target: str, where: dict | None) -> str | None:
        """Why a move to `target` would be forced to first-run speed whatever speed is picked, or None.
        Mirrors the rule in motion._go_to_view: every view involved must be marked checked."""
        start = (where or {}).get("view")
        if target != motion.TRANSIT and not motion.view_checked(target):
            return "this view is not marked checked yet"
        if start and start != motion.TRANSIT and start != target and not motion.view_checked(start):
            return f'the arm starts from "{start}", which is not marked checked yet'
        if target == motion.TRANSIT and not start:
            return "no checked view is involved"
        return None

    # ---- what the status strip says ----

    def note_data(self) -> tuple[dict, str | None]:
        """Read the joints and keep track of when arm data was last there. Any thread."""
        now = time.time()
        try:
            joints = self.arm.joints()
        except Exception as e:  # stale data, arm unplugged, Station gone: never fatal, never guessed around
            if self.data_lost_at is None:
                self.data_lost_at = self.data_seen_at or now
            self.data_reason = str(e) or type(e).__name__
            return {}, self.data_reason
        if self.data_lost_at is not None:
            self.data_lost_at, self.data_back_at = None, now
            self.loop.call_soon_threadsafe(self._forget_where)  # whatever we knew about the arm is old
        self.data_seen_at, self.data_reason = now, None
        return joints, None

    async def watch(self):
        """Keeps the freshness times right while no page is polling (local data only, no Station traffic)."""
        while not self.closing:
            self.note_data()
            await asyncio.sleep(WATCH_S)

    def data_state(self) -> dict:
        now = time.time()
        lost = self.data_lost_at
        return {"fresh": lost is None and self.data_seen_at is not None,
                "age_s": round(now - lost, 1) if lost is not None else 0.0,
                "since": lost, "since_clock": clock(lost) if lost is not None else None,
                "reason": self.data_reason,
                "back_at": self.data_back_at,
                "back_clock": clock(self.data_back_at) if self.data_back_at else None,
                "back_ago_s": round(now - self.data_back_at, 1) if self.data_back_at else None}

    def camera_state(self) -> dict:
        cam = self.cams.get(self.cam_queue) if self.cam_queue else None
        age = time.monotonic() - cam.stamp if cam is not None and cam.jpeg else None
        return {"present": cam is not None, "live": age is not None and age < CAMERA_LIVE_S,
                "age_s": round(age, 1) if age is not None else None}

    @staticmethod
    def link_state(described: dict, blockers: list) -> dict:
        """Does Station link (mirror) a leader arm onto this one? Taken from motion.describe()'s blockers."""
        text = next((b for b in blockers if "mirroring" in b), None)
        if text is None:
            return {"linked": False if "blockers" in described else None, "leader": None, "text": None}
        m = re.search(r"mirroring leader (\S+) onto", text)
        return {"linked": True if m else None, "leader": m.group(1) if m else None, "text": text}

    def advice(self, data: dict, torque, where, link: dict, blockers: list, has_transit: bool) -> dict:
        """ONE plain next step for the person at the page: {"code", "level", "text", "action"}.
        action names a button the page puts next to it: "hold" | "unlink" | "recover" | "capture" | None."""
        def say(code, level, text, action=None):
            return {"code": code, "level": level, "text": text, "action": action}

        place = (where or {}).get("where")
        if self.moving:
            return say("moving", "info", f"Moving to {self.move_view}. STOP holds the arm where it is.")
        if self.stopping:
            return say("stopping", "info", "Stopping: waiting for the arm to confirm that it holds.")
        unheld = self.hold_state == "unconfirmed"
        if not data["fresh"]:
            what = ("Station no longer lists the arm" if "missing" in (data["reason"] or "") else "Arm data stopped")
            return say("stale", "bad", f"{what} {ago(data['age_s'])} ago (at {data['since_clock']}). Check the arm's USB "
                       "cable and power, and that the laptop did not sleep. Nothing can move without live data; "
                       "this clears by itself when the arm is back."
                       + (" The last stop could not be confirmed either: look at the arm, and pull the follower's "
                          "power plug if it is still moving." if unheld else ""))
        if unheld:
            return say("hold_unconfirmed", "bad", "The last stop could not be confirmed. Check the arm, then press STOP "
                       "again; pull the follower's power plug if it is still moving.")
        back = (f"The arm is back (since {data['back_clock']}). " if data["back_ago_s"] is not None
                and data["back_ago_s"] < BACK_NOTE_S else "")
        if link["linked"]:
            return say("linked", "bad", f"{back}Station still links leader {link['leader']} to this arm, so the leader "
                       "would fight every move.", "unlink")
        if link["text"]:
            return say("link_unknown", "warn", f"{back}Cannot tell whether Station links a leader to this arm. "
                       "Open Station and check that the follower is on \"(Self-controlled)\".")
        if self.torque_doubt:
            return say("limp_unconfirmed", "warn", "Could not confirm that the arm went limp. Keep supporting it, "
                       "then press Hold here.", "hold")
        if torque == "limp":
            spot = {"hub": " It is at the transit pose, a good place to hold.",
                    "view": f" It is at \"{(where or {}).get('view')}\".",
                    "spoke": f" It is between \"{(where or {}).get('view')}\" and the transit pose.",
                    "unknown": " It is not at a known pose: pose it by hand at the transit pose first "
                               "(the picture and \"where\" update live), then hold."}.get(place, "")
            return say("limp", "warn", f"{back}The arm is limp.{spot} Support it, then press Hold here.", "hold")
        if not has_transit:
            return say("no_transit", "warn", "No transit pose is saved yet, and every move passes through it. "
                       "Capture one named \"transit\" on the Capture page.", "capture")
        if place == "unknown":
            return say("unknown_pose", "warn", f"{back}The arm is holding at an unknown pose, so there is no checked "
                       "path from here. Recover lifts it straight up to the transit pose, slowly; or pose it by "
                       "hand (Go limp, move it, Hold here).", "recover")
        if blockers:
            action = "hold" if "Hold here" in blockers[0] else None
            return say("blocked", "warn", f"{back}A move cannot start: {blockers[0]}", action)
        if where is None:
            return say("checking", "info", "Checking where the arm is\u2026")
        return say("ready", "ok", "Ready. Click a view and the camera moves there.")

    def still(self) -> tuple[bytes, dict]:
        """One JPEG for the MCP server (Claude), only while nothing moves, with where the arm is.

        Read-only: never commands the arm. On --sim (no camera) it returns the saved snapshot of
        the view the arm is at, so the image workflow can be tried without hardware."""
        if self.moving or self.stopping:
            raise Refused("the arm is moving: stills are only taken while it holds still")
        try:
            joints = self.arm.joints()
        except RuntimeError as e:
            raise Refused(f"no arm data: {e}", 503)
        w = (self.where or {}).get("where") or {}
        view = w.get("view") if w.get("where") == "view" else None
        cam = self.cams.get(self.cam_queue) if self.cam_queue else None
        if cam is not None:
            if not cam.jpeg or time.monotonic() - cam.stamp > STILL_MAX_AGE_S:
                raise Refused("no fresh camera frame; is the camera in Station?", 503)
            jpeg = cam.jpeg
        elif self.sim:
            snap = load_views(self.views_path)["views"].get(view or "", {}).get("snapshot")
            f = SNAPSHOT_DIR / Path(snap).name if snap else None
            if f is None or not f.exists():
                raise Refused("sim camera: pictures only at saved views that have a snapshot", 503)
            jpeg = f.read_bytes()
        else:
            raise Refused("no camera", 503)
        size = framing.jpeg_size(jpeg)
        return jpeg, {"X-View": view or "", "X-Joints": json.dumps(joints, separators=(",", ":")),
                      "X-Image-Size": f"{size[0]}x{size[1]}" if size else "",
                      "X-Taken-At": f"{time.time():.3f}"}

    def frame_preview(self, req: dict) -> tuple[int, dict]:
        """POST /api/frame: where the camera would go to frame a box from the current image.
        Preview only (stage 2, B): computes and checks, never moves. Runs in the HTTP thread
        because the solver takes a second or two; it only reads the arm, like snapshot()."""
        if req.get("dry_run") is not True:
            return 501, {"ok": False, "error": "not_enabled",
                         "detail": "moves to computed targets are not enabled yet; send dry_run: true for a preview"}
        if self.moving or self.stopping:
            return 409, {"ok": False, "error": "moving", "detail": "the arm is moving; preview from a still pose"}
        try:
            joints = self.arm.joints()
            ranges = self.arm.ranges()
        except RuntimeError as e:
            return 503, {"ok": False, "error": "no_arm_data", "detail": str(e)}
        seen = req.get("still_joints")
        if not isinstance(seen, dict) or not seen:
            return 400, {"ok": False, "error": "invalid_request",
                         "detail": "still_joints (the X-Joints header of the image the box is from) is required"}
        try:
            moved = {int(m): abs(int(joints[int(m)]) - int(p)) for m, p in seen.items() if int(m) in joints}
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "invalid_request", "detail": "still_joints must map motor ids to steps"}
        if not moved or max(moved.values()) > FRAME_POSE_TOL:
            return 409, {"ok": False, "error": "stale_image",
                         "detail": "the arm has moved since this image was taken; take a new image and mark the box again"}
        aspect = framing.DEFAULT_ASPECT
        size = str(req.get("image_size") or "")
        if "x" in size:
            try:
                w, h = (int(v) for v in size.split("x", 1))
                aspect = w / h if w > 0 and h > 0 else aspect
            except ValueError:
                pass
        try:
            out = framing.plan(joints, req.get("bbox") or {}, framing=str(req.get("framing") or "fill"),
                               margin=float(req.get("margin", 0.1)), aspect=aspect, ranges=ranges)
        except framing.FramingError as e:
            return (400 if e.code == "invalid_request" else 422), e.body()
        except (TypeError, ValueError) as e:
            return 400, {"ok": False, "error": "invalid_request", "detail": str(e)}
        return 200, {**out, "dry_run": True, "from_view": ((self.where or {}).get("where") or {}).get("view")}

    def snapshot(self) -> dict:
        data = load_views(self.views_path)
        arm = self.arm
        torque, buses = None, {arm.bus_serial: "no data"}
        joints, _ = self.note_data()
        if joints:
            try:  # stale data or an unplugged arm must not break the state poll
                buses = {s: ("torque on" if arm._torque_on(s) else "limp") for s in arm.all_buses()} or buses
                torque = "holding" if arm._torque_on(arm.bus_serial) else "limp"
            except RuntimeError as e:
                logger.debug("joints: %s", e)
        moving = self.moving
        if not moving and time.monotonic() - self.where_at > WHERE_TTL_S:
            self.loop.call_soon_threadsafe(self.refresh_where)
        described = (None if moving else self.where) or {}
        where = described.get("where")
        blockers = described.get("blockers")
        if blockers is None:
            blockers = [described["error"]] if described.get("error") else []
        if self.torque_doubt and not moving:
            blockers = [self.torque_doubt + "; press Hold here or STOP to find out"] + list(blockers)
        has_transit = motion.TRANSIT in data["views"]
        freshness = self.data_state()
        if moving:  # nothing is asked while a move runs; a move only starts with no leader linked
            link = self.last_link
        else:
            link = self.last_link = self.link_state(described, blockers) if described else self.last_link
        return {
            "bus": arm.bus_serial,
            "buses": buses,
            "joints": format_joints(joints) if joints else "no data",
            "connected": bool(joints),
            "torque": torque,
            "cameras": list(self.cams),
            "camera": self.cam_queue,
            "capturing": self.capturing,
            "guided": [{"name": n, "description": d} for n, d in GUIDED_VIEWS],
            "views": data["views"],
            "cards": self.cards(data, where),
            "transit": motion.TRANSIT,
            "has_transit": has_transit,
            "transit_first_run": self.first_run_reason(motion.TRANSIT, where),
            "sim": self.sim,
            "motion": self.motion_state(),
            "hold": self.hold_state,
            "stopping": bool(self.stopping),
            "generation": self.gen,
            "speed_options": SPEEDS,
            "speed_about": {k: v.format(n=SPEEDS.get(k)) for k, v in SPEED_ABOUT.items() if k in SPEEDS},
            "default_speed": DEFAULT_SPEED,
            "first_speed": getattr(motion, "SPEED_FIRST", None),  # what unchecked views are forced to
            "where": where,
            "can_move": ((bool(described.get("ok")) and (where or {}).get("where") != "unknown" and not blockers)
                         if described else None),
            "blockers": blockers,  # why a move cannot start right now (the first one is where_error)
            "where_error": blockers[0] if blockers else None,
            "current_view": where.get("view") if where and where.get("where") == "view" else None,
            # The status strip: is arm data arriving, is the camera, is a leader linked, and the one next step.
            "data": freshness,
            "camera_state": self.camera_state(),
            "link": link,
            "advice": self.advice(freshness, torque, where, link, blockers, has_transit),
            "settings": {k: getattr(motion, k) for k in SETTINGS},
            "settings_changed": any(getattr(motion, k) != SETTING_DEFAULTS[k] for k in SETTINGS),
            "now": time.time(),
        }

    # ---- read-only extras for the control page (HTTP threads; local data only) ----

    def telemetry(self) -> dict:
        """The per-joint table: position, goal error, load, current, temperature, voltage, holding or not."""
        try:
            tel = self.arm.telemetry()
        except Exception as e:
            return {"ok": False, "error": str(e) or type(e).__name__, "joints": [], "warn_at": WARN_AT}
        rows = []
        for m in sorted(tel):
            t = tel[m]
            warn = [k for k, limit in WARN_AT.items() if isinstance(t.get(k), (int, float)) and t[k] >= limit]
            rows.append({"id": m, "name": JOINT_NAMES.get(m, f"motor {m}"), "pos": t["pos"], "goal": t["goal"],
                         "goal_error": t["goal"] - t["pos"], "load": t.get("load"), "current": t.get("current"),
                         "current_a": round(t["current"] * 0.0065, 2) if t.get("current") is not None else None,
                         "temp": t.get("temp"), "voltage": t.get("voltage"), "holding": bool(t.get("torque")),
                         "status": t.get("status", 0), "read_error": t.get("read_error") or "", "warn": warn})
        return {"ok": True, "t": time.time(), "joints": rows, "warn_at": WARN_AT}

    def pose(self) -> dict:
        """Points of the stick figure from the geometry model (fk.py): metres, z up, z = 0 is the desk."""
        if self._fk is None:
            try:
                import fk
                self._fk = fk
            except Exception as e:  # no model, no picture: the page hides the panel
                logger.warning("arm picture unavailable: %s", e)
                self._fk = False
        if not self._fk:
            return {"ok": False, "available": False, "error": "the geometry model (fk.py) could not be loaded"}
        fk = self._fk
        try:
            ranges = self.arm.ranges() or None
            key = (self.views_path, self.views_path.stat().st_mtime if self.views_path.exists() else 0,
                   json.dumps(ranges, sort_keys=True))
            if self._tips is None or self._tips[0] != key:
                self._tips = (key, {name: [round(c, 4) for c in fk.fk(pose, ranges)["jaw_tip"]]
                                    for name, pose in motion.load_views().items() if len(pose) == 8})
            out = {"ok": False, "available": True, "views": self._tips[1], "transit": motion.TRANSIT,
                   "clear_height_m": getattr(motion, "CLEAR_HEIGHT_M", None)}
            joints = self.arm.joints()
            if len(joints) != 8:
                return {**out, "error": "the arm does not report 8 joints"}
            points = fk.fk(joints, ranges)
            chain = [[round(c, 4) for c in points[k]] for k in points if k != "camera"]
            camera = [round(c, 4) for c in points["camera"]]
            watched = [points[k][2] for k in getattr(motion, "CLEAR_POINTS", ()) if k in points]
            return {**out, "ok": True, "chain": chain, "camera": camera,
                    "lowest_m": round(min(p[2] for p in chain[1:] + [camera]), 3),
                    "clearance_m": round(min(watched), 3) if watched else None}
        except RuntimeError as e:  # no arm data: keep the panel, say why it is not moving
            return {"ok": False, "available": True, "error": str(e), "views": (self._tips or (None, {}))[1],
                    "transit": motion.TRANSIT, "clear_height_m": getattr(motion, "CLEAR_HEIGHT_M", None)}
        except Exception as e:
            logger.exception("pose")
            return {"ok": False, "available": True, "error": f"{type(e).__name__}: {e}"}

    def moves(self, limit: int = HISTORY_ROWS) -> dict:
        """The last moves, newest first. Real arm: motion_log.jsonl (every program's moves, with the measured
        lag, load, current and temperature). Sim: moves are not logged, so what this server remembers."""
        def worst(d):
            d = {k: v for k, v in (d or {}).items() if isinstance(v, (int, float))}
            if not d:
                return None
            k = max(d, key=d.get)
            return {"motor": int(k), "value": d[k]}

        rows, source = [], "memory"
        if not self.sim:
            source = Path(motion.LOG_PATH).name
            try:
                with open(motion.LOG_PATH, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    start = max(0, f.tell() - 400_000)
                    f.seek(start)
                    lines = f.read().splitlines()[(1 if start else 0):]
            except OSError:
                lines = []
            for line in lines[-limit:]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        else:
            rows = list(self.history)[-limit:]
        speeds = {v: k for k, v in SPEEDS.items()}
        speeds[getattr(motion, "SPEED_FIRST", None)] = "first-run"
        out = [{"t": r.get("t"), "clock": clock(r["t"]) if r.get("t") else None,
                "date": time.strftime("%d %b", time.localtime(r["t"])) if r.get("t") else None,
                "view": r.get("view"), "ok": bool(r.get("ok")), "error": r.get("error"), "hold": r.get("hold"),
                "speed": r.get("speed"), "speed_name": speeds.get(r.get("speed")), "asked": r.get("asked"),
                "duration": r.get("duration"), "took": r.get("took"), "route": r.get("route") or [],
                "worst_lag": worst(r.get("max_lag")), "peak_load": worst(r.get("max_load")),
                "peak_current": worst(r.get("max_current")), "max_temp": r.get("max_temp")}
               for r in reversed(rows)]
        return {"ok": True, "source": source, "moves": out,
                "note": ("Simulated arm: moves are not logged, this is what the server remembers since it started."
                         if self.sim else "From motion_log.jsonl: every program's moves on the real arm.")}

    def extras(self) -> dict:
        """Is the MCP server listening? A plain TCP connect, at most every 10 s, never more than 0.3 s."""
        at, up = self._mcp
        if time.monotonic() - at > 10:
            url = urlparse(MCP_URL)
            try:
                with socket.create_connection((url.hostname, url.port), timeout=0.3):
                    up = True
            except OSError:
                up = False
            self._mcp = (time.monotonic(), up)
        return {"ok": True, "mcp": {"url": MCP_URL, "up": up}}

    # ---- torque, arm and camera selection ----

    async def set_torque(self, on: bool, gen: int | None = None, click: int | None = None) -> dict:
        async def body():
            if on and self.note_data()[1]:  # holding blind is not possible: say what to do instead
                d = self.data_state()
                raise Refused(f"no arm data for {ago(d['age_s'])} (since {d['since_clock']}): check the arm's USB cable "
                              "and power, then press Hold here again", 503)
            try:
                await (motion.engage(self.arm) if on else motion.release(self.arm))
            except BaseException as e:
                if not on:  # the torque-off may or may not have taken: no move until we know
                    self.torque_doubt = "could not confirm the arm is limp"
                    if isinstance(e, Exception):
                        raise Refused(f"could not confirm the arm is limp ({e or type(e).__name__}); "
                                      "keep supporting it, then press Hold here or STOP", 500) from None
                raise
            finally:
                self._forget_where()
            self.torque_doubt = None
            if on:
                self.hold_state = "confirmed"
            return {"ok": True}
        result = await self.command(body, gen=self.gen if gen is None else gen, click=click, atomic=True)
        await asyncio.sleep(0.2)  # let the next frames from Station show the new torque state
        self.refresh_where()
        return result

    async def select_bus(self, bus: str, gen: int | None = None) -> dict:
        async def body():
            if bus not in self.arm.all_buses():
                raise Refused(f"arm {bus!r} is not connected", 404)
            self.arm.bus_serial = bus
            self._forget_where()
            return {"ok": True, "bus": self.arm.bus_serial}
        result = await self.command(body, gen=self.gen if gen is None else gen, atomic=True)
        self.refresh_where()
        return result

    async def select_camera(self, queue: str) -> dict:
        async def body():
            if queue in self.cams:
                self.cam_queue = queue
            return {"ok": True, "camera": self.cam_queue}
        return await self.command(body, idle=False)

    async def unlink(self, gen: int | None = None, click: int | None = None) -> dict:
        """Tell Station to stop mirroring a leader onto this arm (what "(Self-controlled)" does), then look
        again. It moves nothing and energises nothing; it only ends the link."""
        async def body():
            stop_mirroring = getattr(self.arm, "stop_mirroring", None)
            if stop_mirroring is not None:
                await asyncio.wait_for(stop_mirroring(), 3)
            elif self.sim:
                self.arm.mirror = None  # SimArm has no stop_mirroring(); its pretend link is this attribute
            else:
                raise Refused("this arm connection cannot unlink; set the follower to \"(Self-controlled)\" in Station", 501)
            await asyncio.sleep(0.5)  # Station appends its new mirroring state
            try:
                leader = await self.arm.mirroring_source()
            except Exception as e:
                raise Refused(f"sent, but cannot read Station's link state to check ({e})", 502) from None
            finally:
                self._forget_where()
            if leader:
                raise Refused(f"Station still links leader {leader} to this arm; set the follower to "
                              "\"(Self-controlled)\" in Station")
            return {"ok": True, "linked": False}
        try:
            return await self.command(body, gen=self.gen if gen is None else gen, click=click, atomic=True)
        finally:
            self.refresh_where()

    async def change_settings(self, changes: dict) -> dict:
        """Set runtime toggles of the motion layer (already validated by check_settings). Never while moving."""
        async def body():
            for name, value in changes.items():
                setattr(motion, name, value)
            if "SPEED_TURBO" in changes and "turbo" in SPEEDS:
                SPEEDS["turbo"] = motion.SPEED_TURBO
            logger.warning("motion settings now: %s", {k: getattr(motion, k) for k in SETTINGS})
            return {"ok": True, "settings": settings_view(), "changed": sorted(changes)}
        return await self.command(body)

    # ---- views ----

    async def do_capture(self, name: str, description: str) -> dict:
        if self.sim:
            raise Refused("views are read-only in --sim", 400)

        async def body():
            self.capturing = True
            try:
                data = load_views()
                cam = self.cams.get(self.cam_queue)
                ok = await capture(self.arm, data, name, cam, description, confirm_overwrite=False)
                if not ok:
                    raise RuntimeError(f"bad name {name!r}")
                return load_views()["views"][name]
            finally:
                self.capturing = False
                self._forget_where()  # a new or re-captured view changes where the arm "is"
        return await self.command(body)

    async def delete_view(self, name: str) -> dict:
        if self.sim:
            raise Refused("views are read-only in --sim", 400)

        async def body():
            data = load_views()
            data["views"].pop(name, None)
            save_views(data)
            self._forget_where()
            return {"ok": True}
        return await self.command(body)

    async def mark_checked(self, view: str, checked) -> dict:
        if self.sim:
            raise Refused("views are read-only in --sim", 400)
        if not isinstance(checked, bool):
            raise Refused('"checked" must be true or false', 400)

        async def body():
            if view not in motion.view_names():
                raise Refused(f"unknown view {view!r}; known: {motion.view_names()}", 404)
            motion.mark_checked(view, checked)
            return {"ok": True, "view": view, "checked": motion.view_checked(view)}
        return await self.command(body)

    # ---- moving ----

    async def start_move(self, view: str, speed: str, recover: bool = False, gen: int | None = None,
                         click: int | None = None, along: tuple[str, float] | None = None) -> dict:
        """Start go_to_view as a task and return at once; progress, refusals and errors show up in state."""
        gen = self.gen if gen is None else gen
        if speed not in SPEEDS:
            raise Refused(f"unknown speed {speed!r}; known: {list(SPEEDS)}", 400)
        if recover and view != motion.TRANSIT:
            raise Refused(f"recover only goes to {motion.TRANSIT!r}", 400)
        if recover:
            speed = "slow"

        async def body():
            if view not in motion.load_views():
                raise Refused(f"unknown view {view!r}; known: {list(motion.load_views())}", 404)
            if self.torque_doubt:
                raise Refused(self.torque_doubt + "; press Hold here or STOP to find out, then try again")
            if self.note_data()[1]:  # no live arm data: say what to do instead of starting a move that cannot run
                d = self.data_state()
                raise Refused(f"no arm data for {ago(d['age_s'])} (since {d['since_clock']}): check the arm's USB cable "
                              "and power. Nothing moves until it is back", 503)
            self.move_view, self.move_outcome = view, None
            self.move_started, self.move_speed = time.time(), (speed, SPEEDS[speed])
            self._forget_where()
            self.move_task = asyncio.create_task(self._run_move(view, SPEEDS[speed], recover, along))
            self.move_task.add_done_callback(lambda _: self.refresh_where())
            return {"ok": True, "started": True, "view": view, "speed": speed, "recover": recover, "generation": gen}
        return await self.command(body, gen=gen, click=click)

    async def start_path(self, poses: list, label: str, gen: int | None = None, load_guard: bool = True) -> dict:
        """Start motion.go_through (a computed, model-checked path from the camera director) like start_move."""
        gen = self.gen if gen is None else gen

        async def body():
            if self.torque_doubt:
                raise Refused(self.torque_doubt + "; press Hold here or STOP to find out, then try again")
            if self.note_data()[1]:
                raise Refused("no arm data: check the arm's USB cable and power. Nothing moves until it is back", 503)
            self.move_view, self.move_outcome = label, None
            # computed poses are new territory: first-run speed, like any view nobody has checked (and the shoulder
            # servo's overload protection tripped twice on the real arm at slow speed)
            self.move_started, self.move_speed = time.time(), ("first-run", motion.SPEED_FIRST)
            self._forget_where()
            self.move_task = asyncio.create_task(self._run_move(label, motion.SPEED_FIRST, False, path=poses,
                                                                load_guard=load_guard))
            self.move_task.add_done_callback(lambda _: self.refresh_where())
            return {"ok": True, "started": True, "view": label, "generation": gen}
        return await self.command(body, gen=gen)

    async def _run_move(self, view: str, max_speed: float, recover: bool, along: tuple[str, float] | None = None,
                        path: list | None = None, load_guard: bool = True):
        try:
            if path is not None:
                result = await motion.go_through(self.arm, path, max_speed=max_speed, label=view, load_guard=load_guard)
            else:
                result = await motion.go_to_view(self.arm, view, max_speed=max_speed, allow_unknown_start=recover,
                                                 along=along)
            self._finish({"view": view, **result})
            if result.get("hold") or result.get("ok"):  # a refusal commanded nothing and says nothing about holding
                self.hold_state = result.get("hold") or "confirmed"
        except asyncio.CancelledError:  # only STOP cancels; it records whether its hold was confirmed
            self.move_outcome = {"ok": False, "view": view, "error": "stopped", "hold": None}
            raise
        except Exception as e:  # a bug in the move must not take the server (and its STOP) down
            logger.exception("move to %s crashed", view)
            self.hold_state, hold_error = await self._hold(active=True)
            error = f"move crashed: {type(e).__name__}: {e}" + (f"; {hold_error}; HOLD NOT CONFIRMED" if hold_error else "")
            self._finish({"ok": False, "view": view, "error": error, "hold": self.hold_state})

    def _finish(self, outcome: dict):
        """Record how the move started here ended: for state (with the time, so the page can tell an old
        result from the current situation) and for the in-memory history."""
        now = time.time()
        name, value = self.move_speed or (None, None)
        self.move_outcome = {**outcome, "at": now, "at_clock": clock(now)}
        self.history.append({"t": self.move_started or now, "took": round(now - (self.move_started or now), 2),
                             "asked": name, "speed": value, "duration": 0.0, "route": [], **outcome})

    def _registers(self) -> dict:
        tel = self.arm.telemetry()
        return {m: (tel[m]["goal"], tel[m]["torque"]) for m in motion.MOVABLE if m in tel}

    async def _hold(self, active: bool = False) -> tuple[str, str | None]:
        """motion.hold(): ("confirmed" | "limp", None), or ("unconfirmed", why) when it could not be confirmed.

        active: a move was running (STOP, or a crashed move). If that move sent goals and its own hold
        did not go through, the joints it was driving are stopped where they are; otherwise, and for an
        idle STOP, resting joints keep their goals.

        On a bus whose read-back lags, commands accepted earlier (the move's last waypoints, a torque
        command) can still land after motion.hold() has compared the registers. So the answer only counts
        once the goal registers and torque flags have stopped changing; if they changed, hold again."""
        try:
            deadline = time.monotonic() + HOLD_CONFIRM_S
            while True:
                # After a move: motion.stop_hold() knows whether that move sent any goal and whether its
                # own abort hold went through, so a Stop during the pre-move checks, or after a hold that
                # is already confirmed, leaves resting joints alone.
                result = (await (motion.stop_hold(self.arm) if active
                                 else motion.hold(self.arm))) or "confirmed"
                before = self._registers()
                await asyncio.sleep(HOLD_SETTLE_S)
                if self._registers() == before:
                    return result, None
                if time.monotonic() > deadline:
                    raise RuntimeError("the goal registers keep changing; is something else commanding the arm?")
        except Exception as e:
            logger.error("hold failed: %s", e)
            return "unconfirmed", f"could not confirm the hold: {e or type(e).__name__}"

    async def stop(self) -> dict:
        """STOP: take the arm away from whatever owns it, then hold. Works any time, never queues.

        Single-flight: while one cleanup runs, every further STOP joins it and returns the same result,
        and nothing else is admitted until it has finished, hold confirmation included."""
        generation = self.bump()  # (the HTTP handler has already bumped once, before even reading the request)
        if not self.stopping:
            self.stop_task = asyncio.create_task(self._stop_cleanup())
        result = await asyncio.shield(self.stop_task)
        return {**result, "generation": max(generation, self.gen)}

    async def _stop_cleanup(self) -> dict:
        owned = False
        if self.director is not None:
            self.director.preempt("STOP pressed")
        try:
            task, view, was_moving = self.move_task, self.move_view, self.moving
            if was_moving:
                task.cancel()
            def preempt():
                if self.command_task is not None:
                    self.preempted.add(self.command_task)
                    self.command_task.cancel()

            waited = TORQUE_WAIT_S if self.command_atomic and self.command_task is not None else STOP_WAIT_S
            if not self.command_atomic:  # e.g. a capture in progress gives way at once
                preempt()
            for patience in (waited, STOP_WAIT_S):  # own the arm through the cleanup...
                try:
                    await asyncio.wait_for(self.owner.acquire(), patience)
                    owned = True
                    break
                except TimeoutError:
                    preempt()  # a torque command that overstayed is cut off after all
            if not owned:  # ...but a command that will not unwind must not keep STOP from holding
                logger.error("STOP: the running command did not give way; holding anyway")
            if was_moving:
                await asyncio.wait({task}, timeout=STOP_WAIT_S)  # go_to_view pulls the goals back, then re-raises
            hold, error = await self._hold(active=was_moving)  # the last word, once the move task is gone
            if was_moving and not task.done():
                hold, error = "unconfirmed", f"move task still running {STOP_WAIT_S:.0f}s after STOP" + (f"; {error}" if error else "")
            self.hold_state = hold  # "limp" = no joint is energised, so there was nothing to hold
            if hold != "unconfirmed":
                self.torque_doubt = None  # we have just read which joints are energised
            if was_moving and (task.cancelled() or not task.done()):
                self._finish({"ok": False, "view": view, "hold": hold,
                              "error": "stopped" + (f"; {error}; HOLD NOT CONFIRMED" if error else "")})
            self._forget_where()
            return {"ok": error is None, "hold": hold, "was_moving": was_moving, "view": view if was_moving else None,
                    "error": error}
        except Exception as e:  # never leave a STOP without an answer
            logger.exception("STOP cleanup failed")
            self.hold_state = "unconfirmed"
            return {"ok": False, "hold": "unconfirmed", "was_moving": False, "view": None,
                    "error": f"STOP failed: {type(e).__name__}: {e}"}
        finally:
            if owned:
                self.owner.release()
            self.loop.call_soon(self.refresh_where)

    async def shutdown(self) -> dict | None:
        """Refuse new work, then stop and drain whatever is running. Returns the stop result, if there was one."""
        self.closing = True
        result = None
        if self.moving or self.command_task is not None or self.stopping:
            result = await self.stop()
            if self.move_task is not None and not self.move_task.done():
                await asyncio.wait({self.move_task}, timeout=DRAIN_S)
        try:
            self.arm.close()  # after the stop-and-hold: nothing of ours talks to the arm any more
        except Exception as e:
            logger.warning("closing the arm connection: %s", e)
        return result

    async def sim_faults(self, req: dict, gen: int | None = None) -> dict:
        """--sim only: SimArm's fault injection, to rehearse refusals and aborts from the browser or curl."""
        arm = self.arm
        if "stuck" in req:
            arm.stuck = {int(m) for m in req["stuck"] or []}
        if "stale" in req:
            arm.stale = bool(req["stale"])
        if "mirror" in req:
            arm.mirror = req["mirror"] or None
        for fault in ("load", "hijack"):  # {"2": 950}: stalled load / another writer forcing this goal
            if fault in req:
                setattr(arm, fault, {int(m): int(v) for m, v in (req[fault] or {}).items()})
        if req.get("offset"):  # {"3": 400}: push joints off their pose, e.g. to an unknown one
            async def body():
                present = arm.joints()
                await arm.write_goals({int(m): present[int(m)] + int(d) for m, d in req["offset"].items()})
            await self.command(body, gen=self.gen if gen is None else gen, atomic=True)
        self._forget_where()
        return {"ok": True, "stuck": sorted(arm.stuck), "stale": arm.stale, "mirror": arm.mirror,
                "load": arm.load, "hijack": arm.hijack}


class Server(ThreadingHTTPServer):
    """Stamps every connection with the stop generation at accept time, in accept order and before any
    worker thread runs, so "this request was already on its way when STOP arrived" is decided fairly."""

    daemon_threads = True

    def __init__(self, address, handler, state: State):
        super().__init__(address, handler)
        self.state = state
        self.accept_gen: dict = {}

    def process_request(self, request, client_address):
        self.accept_gen[client_address] = self.state.gen
        super().process_request(request, client_address)


def make_handler(state: State):
    READERS = {"/api/telemetry": state.telemetry, "/api/pose": state.pose, "/api/history": state.moves,
               "/api/extras": state.extras,
               "/api/settings": lambda: {"ok": True, "settings": settings_view(),
                                         "note": "These reset to the defaults when the server restarts."}}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.gen = self.server.accept_gen.pop(self.client_address, state.gen)  # see Server

        def log_message(self, fmt, *args):
            pass

        def _send(self, body: bytes, content_type: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status=200):
            self._send(json.dumps(obj, default=str).encode(), "application/json", status)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in PAGES:
                self._send(PAGES[path].read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    self._json(state.snapshot())
                except Exception as e:
                    logger.exception("state")
                    self._json({"error": f"state: {e or type(e).__name__}"}, 500)
            elif path in READERS:  # read-only extras: local data, never the command owner, never the arm
                try:
                    answer = READERS[path]()
                except Exception as e:
                    logger.exception(path)
                    answer = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                self._json(answer, 200 if answer.get("ok") or path != "/api/pose" or answer.get("available") else 404)
            elif path.startswith("/api/director/"):
                self._director("GET", self.path, {})   # with the query: frame.jpg?id=N
            elif path == "/stream":
                self._stream()
            elif path == "/api/still.jpg":
                try:
                    jpeg, extra = state.still()
                except Refused as e:
                    return self._json({"ok": False, "error": str(e)}, e.status)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(jpeg)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(jpeg)
            elif path.startswith("/views/"):
                f = SNAPSHOT_DIR / Path(path).name
                if f.suffix == ".jpg" and f.exists():
                    self._send(f.read_bytes(), "image/jpeg")
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def _stream(self):
            sim_frames = state.sim and state.director is not None   # --sim --director: the rendered SIMULATED camera
            if not state.cams and not sim_frames:  # --sim, or Station has no camera: one still picture instead of a stream
                return self._send(NO_CAMERA_SVG, "image/svg+xml")
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while True:
                    if sim_frames:
                        f = state.director.frame()
                        jpeg = f[2] if f else None
                    else:
                        cam = state.cams.get(state.cam_queue)
                        jpeg = cam.jpeg if cam else None
                    if jpeg and jpeg is not last:
                        last = jpeg
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                    time.sleep(1 / 15)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _director(self, method: str, path: str, req: dict):
            """Camera director API (camera_director/jobs.py); 404 unless started with --director."""
            if state.director is None:
                return self._json({"ok": False, "error": "the camera director is off; start capture_server.py "
                                   "with --director"}, 404)
            status, body, ctype = state.director.http(method, path[len("/api/director/"):], req)
            if isinstance(body, bytes):
                return self._send(body, ctype, status)
            self._json(body, status)

        def _click(self, req: dict) -> int:
            """The stop generation the page had seen when the user clicked; required for anything that actuates."""
            click = req.get("generation")
            if not isinstance(click, int) or isinstance(click, bool):
                raise Refused('missing "generation" (the value from /api/state): reload the page', 400)
            if click != state.gen:
                raise Refused(STALE_CLICK)
            return click

        def do_POST(self):
            # These endpoints move a real arm: only our own pages (or curl) may call them, not other websites.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._json({"ok": False, "error": "cross-origin request refused"}, 403)
            gen = self.gen
            try:
                if self.path == "/api/stop":
                    state.bump()  # first thing: commands that arrived before this point may no longer start
                    try:
                        self.rfile.read(int(self.headers.get("Content-Length") or 0))  # the body does not matter
                        result = state.call(state.stop(), timeout=STOP_ANSWER_S)
                    except BaseException as e:  # timeout, cancellation, a bug: never an answer without "hold"
                        late = isinstance(e, TimeoutError)
                        result = {"ok": False, "hold": "unconfirmed", "was_moving": None, "view": None,
                                  "error": (f"no answer from the arm / Station in {STOP_ANSWER_S:.0f}s" if late
                                            else f"STOP failed: {type(e).__name__}: {e}")}
                        return self._json(result, 504 if late else 500)
                    return self._json(result, 200 if result["ok"] else 500)
                req = self._body()
                if self.path.startswith("/api/director/"):
                    return self._director("POST", self.path, req)
                if self.path == "/api/frame":  # preview only, never moves; see State.frame_preview
                    status, body = state.frame_preview(req)
                    return self._json(body, status)
                if self.path == "/api/goto":
                    self._json(state.call(state.start_move(str(req.get("view", "")), req.get("speed") or DEFAULT_SPEED,
                                                           recover=req.get("recover") is True, gen=gen,
                                                           click=self._click(req))))
                elif self.path == "/api/checked":
                    self._json(state.call(state.mark_checked(str(req.get("view", "")), req.get("checked"))))
                elif self.path == "/api/sim" and state.sim:
                    self._json(state.call(state.sim_faults(req, gen=gen)))
                elif self.path == "/api/capture":
                    name = req.get("name", "").strip()
                    if not NAME_RE.match(name):
                        return self._json({"error": "name: lowercase letters, digits, _"}, 400)
                    view = state.call(state.do_capture(name, req.get("description", "")))
                    self._json({"ok": True, "view": view})
                elif self.path == "/api/delete":
                    self._json(state.call(state.delete_view(req.get("name"))))
                elif self.path == "/api/torque":
                    self._json(state.call(state.set_torque(bool(req.get("on")), gen=gen, click=self._click(req))))
                elif self.path == "/api/unlink":
                    click = self._click(req) if "generation" in req else None
                    self._json(state.call(state.unlink(gen=gen, click=click)))
                elif self.path == "/api/settings":
                    self._json(state.call(state.change_settings(check_settings(req))))
                elif self.path == "/api/camera":
                    self._json(state.call(state.select_camera(req.get("queue"))))
                elif self.path == "/api/bus":
                    self._json(state.call(state.select_bus(req.get("bus"), gen=gen)))
                else:
                    self.send_error(404)
            except Refused as e:
                self._json({"ok": False, "error": str(e)}, e.status)
            except motion.MotionError as e:  # refused by the motion layer; the text says what to do
                self._json({"ok": False, "error": str(e)}, 409)
            except TimeoutError:
                self._json({"ok": False, "error": "no answer from the arm / Station in time"}, 504)
            except asyncio.CancelledError:  # the command was cancelled under us (STOP, shutdown)
                self._json({"ok": False, "error": "interrupted"}, 409)
            except Exception as e:
                self._json({"ok": False, "error": str(e) or type(e).__name__}, 500)

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="localhost", help="Station host")
    p.add_argument("--bus", default="auto", help="follower bus serial (auto: the arm with torque on)")
    p.add_argument("--port", type=int, default=8890)
    p.add_argument("--sim", action="store_true", help="simulated arm: no Station, no camera, views read-only")
    p.add_argument("--director", action="store_true",
                   help="enable the camera director (target tracking, view survey, cinematic); off by default")
    args = p.parse_args()

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    state = State(loop, sim=args.sim)
    try:
        state.call(state.start_sim() if args.sim else state.start(args.server, args.bus), timeout=30)
    except Exception as e:
        raise SystemExit(f"error: {e}")
    if args.director:
        from camera_director.jobs import Director
        state.director = Director(state)
    loop.call_soon_threadsafe(state.refresh_where)
    asyncio.run_coroutine_threadsafe(state.watch(), loop)
    print(f"follower bus {state.arm.bus_serial}, camera {state.cam_queue}")
    print(f"open http://localhost:{args.port} (capture) or http://localhost:{args.port}/control (move)", flush=True)
    httpd = Server(("127.0.0.1", args.port), make_handler(state), state)
    for name in ("SIGTERM", "SIGHUP"):  # `kill` and a closed terminal get the same clean exit as Ctrl-C
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.default_int_handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:  # refuse new work first, then don't leave the arm chasing a waypoint when the server goes away
        try:
            stopped = state.call(state.shutdown(), timeout=2 * STOP_WAIT_S + DRAIN_S + 2)
            if stopped:
                print("stopped what was running:", stopped, flush=True)
        except Exception as e:
            print(f"could not stop the move: {e or type(e).__name__}", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
