"""Camera director on the simulated arm and on synthetic images. No hardware, no Station, no Claude.

    .venv/bin/python -m unittest tests/test_camera_director.py -v
"""
import asyncio, json, os, sys, threading, time, unittest, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import motion
import capture_server
from capture_server import State, Server, make_handler

motion.LOG_PATH = Path(os.devnull)
motion.SPEED_FIRST = motion.SPEED_SLOW = motion.SPEED_NORMAL = 1500   # quick simulated moves, this process only
capture_server.SPEEDS = {"slow": 1500, "normal": 1500}


def start_server(director: bool = False):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    state = State(loop, sim=True)
    state.call(state.start_sim(), timeout=30)
    if director:
        from camera_director.jobs import Director
        state.director = Director(state)
    loop.call_soon_threadsafe(state.refresh_where)
    httpd = Server(("127.0.0.1", 0), make_handler(state), state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return state, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def http(base, path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, status, ctype = r.read(), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, status, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    return status, (json.loads(raw) if "json" in ctype else raw)


def wait(cond, timeout=60, what="condition"):
    t = time.monotonic()
    while not cond():
        if time.monotonic() - t > timeout:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


class DirectorDisabled(unittest.TestCase):
    def test_director_disabled_preserves_fixed_presets(self):
        state, httpd, base = start_server(director=False)
        try:
            status, body = http(base, "/api/director/state")
            self.assertEqual(status, 404)
            self.assertIn("--director", body["error"])
            status, body = http(base, "/api/director/job", {"kind": "survey"})
            self.assertEqual(status, 404)
            wait(lambda: http(base, "/api/state")[1].get("motion") is not None, what="state")
            gen = http(base, "/api/state")[1]["generation"]
            status, body = http(base, "/api/goto", {"view": "top", "generation": gen})
            self.assertEqual(status, 200, body)
            wait(lambda: not state.moving, what="move end")
            self.assertTrue(state.move_outcome["ok"], state.move_outcome)
            self.assertIsNone(state.director)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
