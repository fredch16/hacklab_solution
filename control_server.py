"""Control service: the only process that writes to Station. REST + SSE on localhost.

    python control_server.py --sim                  # simulated arm, no hardware
    python control_server.py --bus 5B79016329       # real follower (set it to "(Self-controlled)" in Station)
    python control_server.py --bus 5B79016329 --camera   # also serve stills from Station's USB camera

Moves never block: POST /motions returns a motion_id at once, progress comes as
Server-Sent Events on GET /events (try: curl -N localhost:8100/events).
CLI client: ctl.py. API: docs/CONTROL.md. Standard library only.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import motion
from motion_manager import ManagerError, MotionManager

log = logging.getLogger("control")

MAX_HEADER = 16 * 1024
MAX_BODY = 64 * 1024
HEARTBEAT_S = 15.0
ROOT = Path(__file__).parent

REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error",
           501: "Not Implemented", 503: "Service Unavailable"}


class HttpError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


# -- camera sources for /still.jpg ---------------------------------------------

class StationCamera:
    """Latest frame from Station's usb-video queue (variant A, today's hardware)."""

    def __init__(self, cam):
        self.cam = cam

    @classmethod
    async def connect(cls, client, queue: str | None):
        from camera import Camera
        return cls(await Camera.connect(client=client, queue=queue))

    async def still(self) -> bytes:
        return await self.cam.get_frame(max_age_s=1.0, timeout_s=3.0)


class GatewayCamera:
    """Full-resolution still from the iPhone camera gateway (variant B)."""

    def __init__(self, url: str):
        self.url = url.rstrip("/") + "/still.jpg"

    async def still(self) -> bytes:
        import urllib.request

        def fetch():
            with urllib.request.urlopen(self.url, timeout=5) as r:
                return r.read()
        return await asyncio.to_thread(fetch)


# -- HTTP ----------------------------------------------------------------------

class ControlServer:
    def __init__(self, mgr: MotionManager, camera=None):
        self.mgr = mgr
        self.camera = camera
        self.started = time.time()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        method = path = "?"
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return
            if len(head) > MAX_HEADER:
                raise HttpError(413, "header_too_large", "request header too large")
            lines = head.decode("latin-1").split("\r\n")
            try:
                method, target, _ = lines[0].split(" ", 2)
            except ValueError:
                raise HttpError(400, "invalid_request", "malformed request line")
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0)
            if length > MAX_BODY:
                raise HttpError(413, "body_too_large", f"body over {MAX_BODY} bytes")
            body = await reader.readexactly(length) if length else b""
            path = urlsplit(target).path
            await self.route(method, path, body, writer)
        except HttpError as e:
            await self.send_json(writer, e.status, {"error": e.code, "detail": e.detail})
        except ManagerError as e:
            await self.send_json(writer, e.status, e.body())
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("%s %s failed", method, path)
            try:
                await self.send_json(writer, 500, {"error": "internal", "detail": f"{type(e).__name__}: {e}"})
            except ConnectionError:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def send(self, writer, status: int, body: bytes, ctype: str, extra: dict | None = None):
        hdr = [f"HTTP/1.1 {status} {REASONS.get(status, '')}", f"Content-Type: {ctype}",
               f"Content-Length: {len(body)}", "Cache-Control: no-store", "Connection: close"]
        hdr += [f"{k}: {v}" for k, v in (extra or {}).items()]
        writer.write(("\r\n".join(hdr) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()

    async def send_json(self, writer, status: int, data, extra: dict | None = None):
        await self.send(writer, status, (json.dumps(data) + "\n").encode(), "application/json", extra)

    @staticmethod
    def parse_json(body: bytes) -> dict:
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError as e:
            raise HttpError(400, "invalid_json", str(e))
        if not isinstance(data, dict):
            raise HttpError(400, "invalid_request", "body must be a JSON object")
        return data

    async def route(self, method: str, path: str, body: bytes, writer):
        parts = [p for p in path.split("/") if p]
        key = (method, tuple(parts[:1]))
        if key == ("GET", ("health",)):
            return await self.send_json(writer, 200, self.health())
        if key == ("GET", ("status",)):
            return await self.send_json(writer, 200, self.mgr.status())
        if key == ("GET", ("views",)):
            return await self.send_json(writer, 200, {"views": self.mgr.views_public()})
        if key == ("POST", ("motions",)) and len(parts) == 1:
            data = self.parse_json(body)
            unknown = set(data) - {"view", "frame"}
            if unknown:
                raise HttpError(400, "invalid_request", f"unknown fields {sorted(unknown)}; allowed: view")
            if "frame" in data:
                raise HttpError(501, "not_implemented", "frame targets come with stage 2")
            if "view" not in data:
                raise HttpError(400, "invalid_request", 'body must be {"view": "<name>"}')
            return await self.send_json(writer, 202, await self.mgr.start(data["view"]))
        if key == ("GET", ("motions",)) and len(parts) == 2:
            return await self.send_json(writer, 200, self.mgr.get_motion(parts[1]))
        if key == ("POST", ("stop",)):
            log.info("STOP requested")
            return await self.send_json(writer, 200, await self.mgr.stop())
        if key == ("GET", ("still.jpg",)):
            return await self.still(writer)
        if key == ("GET", ("events",)):
            return await self.events(writer)
        if method == "GET" and not parts:
            html = (ROOT / "control_ui.html").read_bytes()
            return await self.send(writer, 200, html, "text/html; charset=utf-8")
        if key == ("GET", ("ui",)) and parts == ["ui", "state"]:
            return await self.send_json(writer, 200, self.mgr.ui_state())
        if key == ("GET", ("snapshots",)) and len(parts) == 2:
            return await self.snapshot(writer, parts[1])
        if parts and parts[0] in ("health", "status", "views", "motions", "stop", "still.jpg", "events"):
            raise HttpError(405, "method_not_allowed", f"{method} not allowed on {path}")
        raise HttpError(404, "not_found", f"no route {path}")

    def health(self) -> dict:
        try:
            self.mgr.arm.joints(self.mgr.stale_s)
            station_ok, err = True, None
        except RuntimeError as e:
            station_ok, err = False, str(e)
        return {
            "station_ok": station_ok, "station_error": err,
            "joint_age_s": self.mgr.joint_age_s(),
            "bus": getattr(self.mgr.arm, "bus_serial", None),
            "camera": type(self.camera).__name__ if self.camera else None,
            "uptime_s": round(time.time() - self.started, 1),
            "views": len(self.mgr.views()),
            "control_authority": "unchecked: set the follower to (Self-controlled) in Station",
        }

    async def still(self, writer):
        if self.camera is None:
            raise HttpError(503, "no_camera", "no camera source; start with --camera or --gateway")
        if self.mgr.moving:
            raise HttpError(409, "moving", "stills are only taken while the arm is holding")
        joints = self.mgr.arm.joints(self.mgr.stale_s)
        try:
            jpeg = await self.camera.still()
        except Exception as e:  # noqa: BLE001
            raise HttpError(503, "camera_error", f"{type(e).__name__}: {e}")
        near = self.mgr.nearest_view(joints)
        await self.send(writer, 200, jpeg, "image/jpeg", {
            "X-Joints": json.dumps(joints, separators=(",", ":")),
            "X-Nearest-View": (near or {}).get("view", ""),
            "X-Taken-At": f"{time.time():.3f}",
        })

    async def snapshot(self, writer, name: str):
        """Snapshot of a saved view (views.json "snapshot"), only for known views."""
        view = name[:-4] if name.endswith(".jpg") else name
        rec = self.mgr.views().get(view)
        snap = rec and rec.get("snapshot")
        if not snap:
            raise HttpError(404, "no_snapshot", f"no snapshot for view {view!r}")
        base = self.mgr.views_path.resolve().parent
        path = (base / snap).resolve()
        if base not in path.parents or not path.is_file():
            raise HttpError(404, "no_snapshot", f"snapshot for {view!r} not found")
        await self.send(writer, 200, path.read_bytes(), "image/jpeg")

    async def events(self, writer):
        q = self.mgr.subscribe()
        try:
            writer.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                          "Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n"
                          "retry: 2000\n\n").encode())
            first = {"t": round(time.time(), 3), "kind": "status", **self.mgr.status()}
            writer.write(f"data: {json.dumps(first)}\n\n".encode())
            await writer.drain()
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), HEARTBEAT_S)
                    writer.write(f"data: {json.dumps(ev)}\n\n".encode())
                except asyncio.TimeoutError:
                    writer.write(b": keep-alive\n\n")
                await writer.drain()
        finally:
            self.mgr.unsubscribe(q)


# -- main ----------------------------------------------------------------------

async def build_arm(args):
    if args.sim:
        from sim_arm import SimArm
        views = MotionManager(None, args.views).views()
        start = views.get("home", next(iter(views.values()), None))
        arm = SimArm(start=start["joints"] if start else None)
        # the simulator only knows ranges for motors 1-6; give the ElRobot's extra motors a full arc
        base = arm.ranges()
        extra = {m: (0, 4095) for m in arm.joints() if m not in base}
        arm.ranges = lambda: {**base, **extra}
        return arm
    from station_arm import Arm
    return await Arm.connect(args.server, args.bus)


async def main(args):
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("listening on %s: anyone on that network can move the arm", args.host)
    arm = await build_arm(args)
    move_kwargs = {"max_speed": args.speed}
    mgr = MotionManager(arm, views_path=args.views, events_path=ROOT / "events.jsonl",
                        move_kwargs=move_kwargs)
    camera = None
    if args.gateway:
        camera = GatewayCamera(args.gateway)
    elif args.camera:
        if args.sim:
            log.warning("--camera ignored with --sim")
        else:
            camera = await StationCamera.connect(arm.client, args.camera_queue)
    srv = ControlServer(mgr, camera)
    server = await asyncio.start_server(srv.handle, args.host, args.port, limit=MAX_HEADER)
    near = mgr.nearest_view()
    log.info("control service on http://%s:%d  bus=%s  views=%d  nearest=%s  camera=%s  max_speed=%s",
             args.host, args.port, getattr(arm, "bus_serial", "?"), len(mgr.views()),
             near and f"{near['view']} ({near['distance']} steps)", type(camera).__name__ if camera else None,
             args.speed)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with server:
        await stop.wait()
    log.info("shutting down: holding the arm")
    await mgr.shutdown()
    if hasattr(arm, "close"):
        arm.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sim", action="store_true", help="simulated arm, no Station needed")
    p.add_argument("--server", default="localhost", help="Station host")
    p.add_argument("--bus", default="auto", help="follower bus serial (default: auto)")
    p.add_argument("--host", default="127.0.0.1", help="listen address (default: localhost only)")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--views", type=Path, default=motion.VIEWS_PATH)
    p.add_argument("--speed", type=float, default=motion.MAX_SPEED,
                   help=f"max steps/s of the furthest joint (default {motion.MAX_SPEED}; 250 = half speed)")
    p.add_argument("--camera", action="store_true", help="serve stills from Station's USB camera")
    p.add_argument("--camera-queue", default=None, help="usbvideo queue (default: first camera)")
    p.add_argument("--gateway", default=None, help="iPhone camera gateway base URL, e.g. http://127.0.0.1:8400")
    return p.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    try:
        asyncio.run(main(parse_args()))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
