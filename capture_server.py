"""Web UI for capturing camera views (M1): live camera, guided steps, one click per view.

    python capture_server.py                    # then open http://localhost:8890
    python capture_server.py --bus 5B79016329   # pick the follower explicitly

Pose the follower (via the leader arm, or by hand), check the live picture, click Save.
Writes the same views.json + views/<name>.jpg as capture_view.py.
"""

import argparse
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from camera import Camera, list_camera_queues
from capture_view import GUIDED_VIEWS, NAME_RE, SNAPSHOT_DIR, capture, load_views, save_views
from station_arm import Arm, format_joints, logger

ROOT = Path(__file__).parent
PAGE = ROOT / "capture_ui.html"


class State:
    """Lives on the asyncio loop thread; HTTP threads call in via `call`."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.arm: Arm | None = None
        self.cams: dict[str, Camera] = {}
        self.cam_queue: str | None = None
        self.capturing = False

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

    def snapshot(self) -> dict:
        data = load_views()
        arm = self.arm
        joints = {}
        try:
            joints = arm.joints()
        except RuntimeError as e:
            logger.warning("joints: %s", e)
        return {
            "bus": arm.bus_serial,
            "buses": {s: ("torque on" if arm._torque_on(s) else "limp") for s in arm.all_buses()},
            "joints": format_joints(joints) if joints else "no data",
            "cameras": list(self.cams),
            "camera": self.cam_queue,
            "capturing": self.capturing,
            "guided": [{"name": n, "description": d} for n, d in GUIDED_VIEWS],
            "views": data["views"],
        }

    async def do_capture(self, name: str, description: str) -> dict:
        if self.capturing:
            raise RuntimeError("already capturing")
        self.capturing = True
        try:
            data = load_views()
            cam = self.cams.get(self.cam_queue)
            ok = await capture(self.arm, data, name, cam, description, confirm_overwrite=False)
            if not ok:
                raise RuntimeError(f"bad name {name!r}")
            return data["views"][name]
        finally:
            self.capturing = False


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                self._json(state.snapshot())
            elif self.path.startswith("/stream"):
                self._stream()
            elif self.path.startswith("/views/"):
                f = SNAPSHOT_DIR / Path(self.path.split("?")[0]).name
                if f.suffix == ".jpg" and f.exists():
                    body = f.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def _stream(self):
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
            try:
                req = self._body()
                if self.path == "/api/capture":
                    name = req.get("name", "").strip()
                    if not NAME_RE.match(name):
                        return self._json({"error": "name: lowercase letters, digits, _"}, 400)
                    view = state.call(state.do_capture(name, req.get("description", "")))
                    self._json({"ok": True, "view": view})
                elif self.path == "/api/delete":
                    data = load_views()
                    data["views"].pop(req.get("name"), None)
                    save_views(data)
                    self._json({"ok": True})
                elif self.path == "/api/camera":
                    if req.get("queue") in state.cams:
                        state.cam_queue = req["queue"]
                    self._json({"ok": True})
                elif self.path == "/api/bus":
                    if req.get("bus") in state.arm.all_buses():
                        state.arm.bus_serial = req["bus"]
                    self._json({"ok": True})
                else:
                    self.send_error(404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="localhost", help="Station host")
    p.add_argument("--bus", default="auto", help="follower bus serial (auto: the arm with torque on)")
    p.add_argument("--port", type=int, default=8890)
    args = p.parse_args()

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    state = State(loop)
    try:
        state.call(state.start(args.server, args.bus), timeout=30)
    except Exception as e:
        raise SystemExit(f"error: {e}")
    print(f"follower bus {state.arm.bus_serial}, camera {state.cam_queue}")
    print(f"open http://localhost:{args.port}")
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
