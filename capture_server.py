"""Web UI for the camera arm: capture views (M1) and move between them (M2).

    python capture_server.py                    # then open http://localhost:8890
    python capture_server.py --bus 5B79016329   # pick the follower explicitly
    python capture_server.py --sim --port 8891  # simulated arm: no Station, no camera

/         capture: Go limp, pose the follower by hand, Hold here, check the live picture, click Save.
          Writes the same views.json + views/<name>.jpg as capture_view.py.
/control  control: click a saved view and the arm moves there (motion.go_to_view); STOP holds it where it is.

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
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import motion
from camera import Camera, list_camera_queues
from capture_view import GUIDED_VIEWS, NAME_RE, SNAPSHOT_DIR, VIEWS_PATH, capture, load_views, save_views
from station_arm import Arm, format_joints, logger

ROOT = Path(__file__).parent
PAGES = {"/": ROOT / "capture_ui.html", "/index.html": ROOT / "capture_ui.html", "/control": ROOT / "control_ui.html"}

SPEEDS = {"slow": motion.SPEED_SLOW, "normal": motion.SPEED_NORMAL}  # max_speed for go_to_view, steps per second
DEFAULT_SPEED = "slow"
STOP_WAIT_S = 1.0   # how long STOP waits for the cancelled move / command to unwind before it holds anyway
TORQUE_WAIT_S = 1.5 # ...and for a Hold here / Go limp in progress to finish (short, and nothing is moving then)
STOP_ANSWER_S = 6.0 # /api/stop answers within this, with "hold": "unconfirmed" if the cleanup has not finished
HOLD_SETTLE_S = 0.2 # a hold counts as confirmed once the goal registers have not changed for this long
HOLD_CONFIRM_S = 1.5  # ...and we give up waiting for that after this long
STALE_CLICK = "the arm was stopped since this page last updated; check the arm and click again"
DRAIN_S = 3.0       # shutdown: how long to wait for the stopped move task to end
WHERE_TTL_S = 1.0   # motion.describe() is asked at most this often, and never while a move is running

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

    def cards(self, data: dict) -> list[dict]:
        """The views a user can click, in motion's order (the transit pose is not one of them)."""
        try:
            limit_notes = motion.check_views(self.arm.ranges())
        except Exception:  # no calibration / no arm data: describe() reports that
            limit_notes = {}
        descriptions = motion.view_descriptions()
        return [{"name": n, "description": descriptions.get(n, ""),
                 "snapshot": bool(data["views"].get(n, {}).get("snapshot")),
                 "captured_at": data["views"].get(n, {}).get("captured_at"),
                 "checked": motion.view_checked(n), "limit_notes": limit_notes.get(n, [])}
                for n in motion.view_names()]

    def snapshot(self) -> dict:
        data = load_views(self.views_path)
        arm = self.arm
        joints, torque, buses = {}, None, {arm.bus_serial: "no data"}
        try:  # stale data or an unplugged arm must not break the state poll
            buses = {s: ("torque on" if arm._torque_on(s) else "limp") for s in arm.all_buses()} or buses
            joints = arm.joints()
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
            "cards": self.cards(data),
            "transit": motion.TRANSIT,
            "has_transit": motion.TRANSIT in data["views"],
            "sim": self.sim,
            "motion": self.motion_state(),
            "hold": self.hold_state,
            "stopping": bool(self.stopping),
            "generation": self.gen,
            "speed_options": SPEEDS,
            "default_speed": DEFAULT_SPEED,
            "first_speed": getattr(motion, "SPEED_FIRST", None),  # what unchecked views are forced to
            "where": where,
            "can_move": ((bool(described.get("ok")) and (where or {}).get("where") != "unknown" and not blockers)
                         if described else None),
            "blockers": blockers,  # why a move cannot start right now (the first one is where_error)
            "where_error": blockers[0] if blockers else None,
            "current_view": where.get("view") if where and where.get("where") == "view" else None,
        }

    # ---- torque, arm and camera selection ----

    async def set_torque(self, on: bool, gen: int | None = None, click: int | None = None) -> dict:
        async def body():
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
                         click: int | None = None) -> dict:
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
            self.move_view, self.move_outcome = view, None
            self._forget_where()
            self.move_task = asyncio.create_task(self._run_move(view, SPEEDS[speed], recover))
            self.move_task.add_done_callback(lambda _: self.refresh_where())
            return {"ok": True, "started": True, "view": view, "speed": speed, "recover": recover, "generation": gen}
        return await self.command(body, gen=gen, click=click)

    async def _run_move(self, view: str, max_speed: float, recover: bool):
        try:
            result = await motion.go_to_view(self.arm, view, max_speed=max_speed, allow_unknown_start=recover)
            self.move_outcome = {"view": view, **result}
            if result.get("hold") or result.get("ok"):  # a refusal commanded nothing and says nothing about holding
                self.hold_state = result.get("hold") or "confirmed"
        except asyncio.CancelledError:  # only STOP cancels; it records whether its hold was confirmed
            self.move_outcome = {"ok": False, "view": view, "error": "stopped", "hold": None}
            raise
        except Exception as e:  # a bug in the move must not take the server (and its STOP) down
            logger.exception("move to %s crashed", view)
            self.hold_state, hold_error = await self._hold(active=True)
            error = f"move crashed: {type(e).__name__}: {e}" + (f"; {hold_error}; HOLD NOT CONFIRMED" if hold_error else "")
            self.move_outcome = {"ok": False, "view": view, "error": error, "hold": self.hold_state}

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
                self.move_outcome = {"ok": False, "view": view, "hold": hold,
                                     "error": "stopped" + (f"; {error}; HOLD NOT CONFIRMED" if error else "")}
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
            elif path == "/stream":
                self._stream()
            elif path.startswith("/views/"):
                f = SNAPSHOT_DIR / Path(path).name
                if f.suffix == ".jpg" and f.exists():
                    self._send(f.read_bytes(), "image/jpeg")
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def _stream(self):
            if not state.cams:  # --sim, or Station has no camera: one still picture instead of a stream
                return self._send(NO_CAMERA_SVG, "image/svg+xml")
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while True:
                    cam = state.cams.get(state.cam_queue)
                    jpeg = cam.jpeg if cam else None
                    if jpeg and jpeg is not last:
                        last = jpeg
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                    time.sleep(1 / 15)
            except (BrokenPipeError, ConnectionResetError):
                pass

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
    args = p.parse_args()

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    state = State(loop, sim=args.sim)
    try:
        state.call(state.start_sim() if args.sim else state.start(args.server, args.bus), timeout=30)
    except Exception as e:
        raise SystemExit(f"error: {e}")
    loop.call_soon_threadsafe(state.refresh_where)
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
