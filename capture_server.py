"""Web UI for the camera arm: capture views (M1) and move between them (M2).

    python capture_server.py                    # then open http://localhost:8890
    python capture_server.py --bus 5B79016329   # pick the follower explicitly
    python capture_server.py --sim --port 8891  # simulated arm: no Station, no camera

/         capture: Go limp, pose the follower by hand, Hold here, check the live picture, click Save.
          Writes the same views.json + views/<name>.jpg as capture_view.py.
/control  control: click a saved view and the arm moves there (motion.go_to_view); STOP holds it where it is.

All arm motion and torque goes through motion.py (go_to_view, hold, engage, release, describe).
"""

import argparse
import asyncio
import json
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
STOP_WAIT_S = 1.0   # how long STOP waits for the cancelled move to finish before holding again
WHERE_TTL_S = 1.0   # motion.describe() is asked at most this often, and never while a move is running

NO_CAMERA_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360"><rect width="640" height="360"/>'
                 b'<text x="320" y="187" fill="#8a92a3" font-family="system-ui, sans-serif" font-size="20" '
                 b'text-anchor="middle">no camera</text></svg>')


class Refused(Exception):
    """A request we won't carry out right now; the message goes to the page."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class State:
    """Lives on the asyncio loop thread; HTTP threads call in via `call`."""

    def __init__(self, loop: asyncio.AbstractEventLoop, sim: bool = False):
        self.loop = loop
        self.sim = sim
        self.arm: Arm | None = None
        self.cams: dict[str, Camera] = {}
        self.cam_queue: str | None = None
        self.capturing = False
        self.views_path = VIEWS_PATH
        self.move_task: asyncio.Task | None = None
        self.move_view: str | None = None
        self.move_outcome: dict | None = None  # how the last move started here ended (result, stopped or crashed)
        self.where: dict | None = None         # last motion.describe(): {"ok", "error", "where": {...}}
        self.where_at = 0.0
        self.where_gen = 0                     # bumped whenever the cached answer stops being true
        self.where_task: asyncio.Task | None = None

    def call(self, coro, timeout: float = 15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def start(self, server: str, bus: str):
        self.arm = await Arm.connect(server, bus="any")
        buses = list(self.arm.all_buses())
        if bus != "auto":
            self.arm.bus_serial = bus
        else:
            torqued = [s for s in buses if self.arm._torque_on(s)]
            self.arm.bus_serial = torqued[0] if len(torqued) == 1 else buses[0]
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
        self.arm = SimArm()  # starts at the saved home view

    @property
    def moving(self) -> bool:
        return self.move_task is not None and not self.move_task.done()

    def motion_state(self) -> dict:
        """motion.status, completed with what this server knows about the move it started."""
        m = dict(motion.status)
        if self.moving:
            if not m.get("moving"):  # task created, go_to_view not running yet
                m.update(moving=True, view=self.move_view, phase="check", phase_index=0, phase_count=0,
                         progress=0.0, error=None, route=[])
        elif self.move_outcome is not None:
            m.update(moving=False, phase=None, error=self.move_outcome.get("error"), last_result=self.move_outcome)
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
            "speed_options": SPEEDS,
            "default_speed": DEFAULT_SPEED,
            "where": where,
            "where_error": described.get("error"),
            "current_view": where.get("view") if where and where.get("where") == "view" else None,
        }

    # ---- torque ----

    async def set_torque(self, on: bool) -> dict:
        if self.moving:
            if not on:
                raise Refused(f"moving to {self.move_view}: press STOP before going limp")
            return await self.stop()  # "Hold here" during a move means stop
        try:
            await (motion.engage(self.arm) if on else motion.release(self.arm))
        finally:
            self._forget_where()
        await asyncio.sleep(0.2)  # let the next frames from Station show the new torque state
        self.refresh_where()
        return {"ok": True}

    # ---- views ----

    async def do_capture(self, name: str, description: str) -> dict:
        if self.sim:
            raise Refused("views are read-only in --sim", 400)
        if self.moving:
            raise Refused(f"moving to {self.move_view}: wait or press STOP")
        if self.capturing:
            raise RuntimeError("already capturing")
        self.capturing = True
        try:
            data = load_views()
            cam = self.cams.get(self.cam_queue)
            ok = await capture(self.arm, data, name, cam, description, confirm_overwrite=False)
            if not ok:
                raise RuntimeError(f"bad name {name!r}")
            self._forget_where()  # a new or re-captured view changes where the arm "is"
            return data["views"][name]
        finally:
            self.capturing = False

    async def delete_view(self, name: str) -> dict:
        if self.sim:
            raise Refused("views are read-only in --sim", 400)
        data = load_views()
        data["views"].pop(name, None)
        save_views(data)
        self._forget_where()
        return {"ok": True}

    async def mark_checked(self, view: str, checked) -> dict:
        if self.sim:
            raise Refused("views are read-only in --sim", 400)
        if not isinstance(checked, bool):
            raise Refused('"checked" must be true or false', 400)
        if view not in motion.view_names():
            raise Refused(f"unknown view {view!r}; known: {motion.view_names()}", 404)
        motion.mark_checked(view, checked)
        return {"ok": True, "view": view, "checked": motion.view_checked(view)}

    # ---- moving ----

    async def start_move(self, view: str, speed: str, recover: bool = False) -> dict:
        """Start go_to_view as a task and return at once; progress, refusals and errors show up in state."""
        if self.moving:
            raise Refused(f"already moving to {self.move_view}")
        if speed not in SPEEDS:
            raise Refused(f"unknown speed {speed!r}; known: {list(SPEEDS)}", 400)
        if recover and view != motion.TRANSIT:
            raise Refused(f"recover only goes to {motion.TRANSIT!r}", 400)
        if view not in motion.load_views():
            raise Refused(f"unknown view {view!r}; known: {list(motion.load_views())}", 404)
        if recover:
            speed = "slow"
        self.move_view, self.move_outcome = view, None
        self._forget_where()
        self.move_task = asyncio.create_task(self._run_move(view, SPEEDS[speed], recover))
        self.move_task.add_done_callback(lambda _: self.refresh_where())
        return {"ok": True, "started": True, "view": view, "speed": speed, "recover": recover}

    async def _run_move(self, view: str, max_speed: float, recover: bool):
        try:
            result = await motion.go_to_view(self.arm, view, max_speed=max_speed, allow_unknown_start=recover)
            self.move_outcome = {"view": view, **result}
        except asyncio.CancelledError:
            self.move_outcome = {"ok": False, "view": view, "error": "stopped"}
            raise
        except Exception as e:  # a bug in the move must not take the server (and its STOP) down
            logger.exception("move to %s crashed", view)
            error = f"move crashed: {type(e).__name__}: {e}"
            if hold_error := await self._hold():
                error += f"; then {hold_error}"
            self.move_outcome = {"ok": False, "view": view, "error": error}

    async def _hold(self) -> str | None:
        try:
            await motion.hold(self.arm)
        except Exception as e:
            logger.error("hold failed: %s", e)
            return f"could not confirm the hold: {e or type(e).__name__}"

    async def stop(self) -> dict:
        """STOP: cancel the move (if any) and hold. Works any time, also when nothing is moving."""
        task, view, was_moving = self.move_task, self.move_view, self.moving
        if was_moving:
            task.cancel()
        error = await self._hold()  # right away, without waiting for the move to unwind
        if was_moving:
            await asyncio.wait({task}, timeout=STOP_WAIT_S)  # go_to_view holds too, then re-raises
            if task.cancelled():
                self.move_outcome = {"ok": False, "view": view, "error": "stopped"}
            error = await self._hold()  # the last word, after the move task is gone
            if not task.done():
                error = f"move task still running {STOP_WAIT_S:.0f}s after STOP" + (f"; {error}" if error else "")
        self._forget_where()
        self.refresh_where()
        return {"ok": error is None, "was_moving": was_moving, "view": view if was_moving else None, "error": error}

    async def sim_faults(self, req: dict) -> dict:
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
            if self.moving:
                raise Refused("moving: press STOP first")
            present = arm.joints()
            await arm.write_goals({int(m): present[int(m)] + int(d) for m, d in req["offset"].items()})
        self._forget_where()
        return {"ok": True, "stuck": sorted(arm.stuck), "stale": arm.stale, "mirror": arm.mirror,
                "load": arm.load, "hijack": arm.hijack}


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
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

        def do_POST(self):
            # These endpoints move a real arm: only our own pages (or curl) may call them, not other websites.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._json({"ok": False, "error": "cross-origin request refused"}, 403)
            try:
                req = self._body()
                if self.path == "/api/stop":
                    result = state.call(state.stop(), timeout=5)
                    self._json(result, 200 if result["ok"] else 500)
                elif self.path == "/api/goto":
                    self._json(state.call(state.start_move(str(req.get("view", "")), req.get("speed") or DEFAULT_SPEED,
                                                           recover=req.get("recover") is True)))
                elif self.path == "/api/checked":
                    self._json(state.call(state.mark_checked(str(req.get("view", "")), req.get("checked"))))
                elif self.path == "/api/sim" and state.sim:
                    self._json(state.call(state.sim_faults(req)))
                elif self.path == "/api/capture":
                    name = req.get("name", "").strip()
                    if not NAME_RE.match(name):
                        return self._json({"error": "name: lowercase letters, digits, _"}, 400)
                    view = state.call(state.do_capture(name, req.get("description", "")))
                    self._json({"ok": True, "view": view})
                elif self.path == "/api/delete":
                    self._json(state.call(state.delete_view(req.get("name"))))
                elif self.path == "/api/torque":
                    self._json(state.call(state.set_torque(bool(req.get("on")))))
                elif self.path == "/api/camera":
                    if req.get("queue") in state.cams:
                        state.cam_queue = req["queue"]
                    self._json({"ok": True})
                elif self.path == "/api/bus":
                    if state.moving:
                        raise Refused(f"moving to {state.move_view}: press STOP before switching arms")
                    if req.get("bus") in state.arm.all_buses():
                        state.arm.bus_serial = req["bus"]
                    self._json({"ok": True})
                else:
                    self.send_error(404)
            except Refused as e:
                self._json({"ok": False, "error": str(e)}, e.status)
            except motion.MotionError as e:  # refused by the motion layer; the text says what to do
                self._json({"ok": False, "error": str(e)}, 409)
            except TimeoutError:
                self._json({"ok": False, "error": "no answer from the arm / Station in time"}, 504)
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
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # `kill` gets the same clean exit as Ctrl-C
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if state.moving:  # don't leave the arm chasing a waypoint when the server goes away
            try:
                print("stopping the running move:", state.call(state.stop(), timeout=5))
            except Exception as e:
                print(f"could not stop the move: {e}")


if __name__ == "__main__":
    main()
