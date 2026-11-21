"""Control service on the simulated arm: non-blocking moves, busy, stop, via, watchdog,
timeout, stall, and the REST + SSE API end to end. No hardware, no Station.

    python3 tests/test_control_sim.py
"""
import asyncio, json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctl
from motion_manager import ManagerError, MotionManager
from control_server import ControlServer
from sim_arm import SimArm

FAST = {"min_duration": 0.3, "max_speed": 4000}

tmp = Path(tempfile.mkdtemp())
VIEWS = tmp / "views.json"
VIEWS.write_text(json.dumps({"views": {
    "home": {"joints": {"1": 2048, "2": 1000, "3": 3000, "4": 2048, "5": 2048, "6": 2000}, "description": "folded"},
    "left": {"joints": {"1": 1400, "2": 1800, "3": 2200, "4": 2500, "5": 2048, "6": 3000}, "description": "from the left"},
    "top":  {"joints": {"1": 2600, "2": 1600, "3": 2400, "4": 2300, "5": 2048, "6": 2000}, "description": "top-down",
             "via": ["home"], "aliases": ["von oben"]},
}}))
HOME = {1: 2048, 2: 1000, 3: 3000, 4: 2048, 5: 2048, 6: 2000}


def sim(**kw):
    return SimArm(start=dict(HOME), speed=kw.pop("speed", 20000))


def mgr_for(arm, **kw):
    return MotionManager(arm, views_path=VIEWS, events_path=tmp / "events.jsonl",
                         move_kwargs=kw.pop("move", FAST), **kw)


async def wait_end(mgr, timeout=10):
    t = time.monotonic()
    while mgr.moving:
        assert time.monotonic() - t < timeout, "move did not end"
        await asyncio.sleep(0.02)
    return mgr.current.public()


async def expect_error(coro, status, code):
    try:
        await coro
    except ManagerError as e:
        assert (e.status, e.code) == (status, code), (e.status, e.code, e.detail)
        return
    raise AssertionError(f"expected {status} {code}")


class StaleAfter(SimArm):
    """Joint data goes stale after n reads (Station lost)."""
    def __init__(self, n, **kw):
        super().__init__(**kw); self.n = n; self.reads = 0
    def joints(self, max_age_s=0.5):
        self.reads += 1
        if self.reads > self.n:
            raise RuntimeError("joint data is stale (9.9s old)")
        return super().joints(max_age_s)


class Blocked(SimArm):
    """Motor 2 cannot move (blocked or overloaded)."""
    async def write_goals(self, goals, speed=None, accel=None):
        await super().write_goals({m: g for m, g in goals.items() if m != 2}, speed, accel)


async def manager_tests():
    # 1. start returns at once, move finishes in the background
    arm = sim(); mgr = mgr_for(arm)
    q = mgr.subscribe()
    t = time.monotonic(); m = await mgr.start("left"); dt = time.monotonic() - t
    assert dt < 0.05 and m["state"] == "moving", (dt, m)
    assert mgr.status()["state"] == "moving"
    # 2. busy while moving
    await expect_error(mgr.start("home"), 409, "busy")
    end = await wait_end(mgr)
    assert end["state"] == "reached" and end["progress"] == 1.0, end
    assert mgr.status()["state"] == "holding"
    assert mgr.nearest_view()["view"] == "left" and mgr.nearest_view()["at"]
    states = []
    while not q.empty():
        states.append(q.get_nowait()["state"])
    assert states[0] == "moving" and states[-1] == "reached", states
    print("ok  non-blocking start, busy, reached, events:", states)

    # 3. stop mid-move holds
    m = await mgr.start("home"); await asyncio.sleep(0.12)
    st = await mgr.stop()
    assert st["motion"]["state"] == "stopped", st
    j1 = arm.joints(); await asyncio.sleep(0.3); j2 = arm.joints()
    assert j1 == j2, "stop must hold position"
    assert j1 != HOME, "test should stop before arriving"
    print("ok  stop mid-move holds at", j2)

    # 4. unknown view, bad input
    await expect_error(mgr.start("nope"), 404, "unknown_view")
    await expect_error(mgr.start(""), 400, "invalid_request")
    await expect_error(mgr.start(42), 400, "invalid_request")
    print("ok  unknown view and bad input rejected")

    # 5. via: top goes through home first
    seen = []
    q2 = mgr.subscribe()
    m = await mgr.start("top")
    assert m["path"] == ["home", "top"], m
    end = await wait_end(mgr)
    while not q2.empty():
        seen.append(q2.get_nowait())
    assert end["state"] == "reached" and mgr.nearest_view()["view"] == "top", end
    assert any(0 < e["progress"] < 1 for e in seen), "progress events expected"
    print("ok  via path", m["path"], "with", len(seen), "events")

    # 6. stop while idle just holds
    st = await mgr.stop()
    assert st["state"] == "holding", st
    print("ok  stop while idle")

    # 7. watchdog: stale data mid-move -> error, nothing written afterwards
    arm = StaleAfter(8, start=dict(HOME), speed=20000); mgr = mgr_for(arm, move={"min_duration": 1.0, "max_speed": 4000})
    await mgr.start("left")
    end = await wait_end(mgr)
    writes = arm.writes; await asyncio.sleep(0.2)
    assert end["state"] == "error" and "stale" in end["error"], end
    assert arm.writes == writes, "no writes after the watchdog fired"
    print("ok  watchdog:", end["error"])

    # 8. not ready: stale before the move
    arm = StaleAfter(0, start=dict(HOME)); mgr = mgr_for(arm)
    await expect_error(mgr.start("left"), 409, "not_ready")
    print("ok  not_ready when joint data is stale")

    # 9. timeout per leg
    arm = sim(); mgr = mgr_for(arm, move={"min_duration": 2.0, "max_speed": 4000}, leg_timeout_s=0.3)
    await mgr.start("left")
    end = await wait_end(mgr)
    j1 = arm.joints(); await asyncio.sleep(0.2)
    assert end["state"] == "timeout" and arm.joints() == j1, end
    print("ok  timeout holds:", end["error"])

    # 10. stalled: a blocked joint does not arrive
    arm = Blocked(start=dict(HOME), speed=20000); mgr = mgr_for(arm)
    await mgr.start("left")
    end = await wait_end(mgr)
    assert end["state"] == "stalled" and "motor 2" in end["error"], end
    print("ok  stalled:", end["error"])

    lines = (tmp / "events.jsonl").read_text().splitlines()
    assert all(json.loads(l)["kind"] == "motion" for l in lines) and len(lines) >= 10
    print("ok  events.jsonl has", len(lines), "state changes")


class FakeCamera:
    async def still(self):
        return b"\xff\xd8fake-jpeg\xff\xd9"


async def http_tests():
    arm = sim(); mgr = mgr_for(arm, move={"min_duration": 0.6, "max_speed": 4000})
    srv = ControlServer(mgr, camera=None)
    server = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    call = asyncio.to_thread

    def status_of(fn, *a):
        try:
            fn(*a); return 200
        except ctl.CtlError as e:
            return int(str(e).split()[0])

    async with server:
        h = await call(ctl.get_json, base, "/health")
        assert h["station_ok"] and h["views"] == 3, h
        v = await call(ctl.get_json, base, "/views")
        assert [x["name"] for x in v["views"]] == ["home", "left", "top"] and "joints" not in v["views"][0]
        assert await call(status_of, ctl.request, base + "/still.jpg") == 503
        print("ok  GET /health, /views (no joint values), /still.jpg without camera = 503")

        # SSE reader in a thread, collecting until the move ends
        collected = []
        def reader():
            for ev in ctl.events(base, timeout=20):
                collected.append(ev)
                if ev.get("state") in ctl.FINAL and ev.get("kind") == "motion":
                    return
        rt = asyncio.ensure_future(call(reader)); await asyncio.sleep(0.2)

        m = await call(ctl.post_json, base, "/motions", {"view": "left"})
        assert m["state"] == "moving" and m["motion_id"].startswith("m-"), m
        assert await call(status_of, ctl.post_json, base, "/motions", {"view": "home"}) == 409
        srv.camera = FakeCamera()
        assert await call(status_of, ctl.request, base + "/still.jpg") == 409   # moving
        await asyncio.wait_for(rt, 10)
        assert collected[0]["kind"] == "status" and collected[-1]["state"] == "reached", collected[-1]
        got = await call(ctl.get_json, base, f"/motions/{m['motion_id']}")
        assert got["state"] == "reached", got
        print("ok  POST /motions 202, busy 409, SSE until reached:", len(collected), "events")

        _, headers, body = await call(ctl.request, base + "/still.jpg")
        assert body.startswith(b"\xff\xd8") and json.loads(headers["X-Joints"]) and headers["X-Nearest-View"] == "left"
        print("ok  GET /still.jpg while holding, with X-Joints and X-Nearest-View")

        bad = [("POST", "/motions", b"{not json", 400), ("POST", "/motions", b'{"view":"top","speed":9}', 400),
               ("POST", "/motions", b'{"frame":{}}', 501), ("POST", "/motions", b"[]", 400),
               ("GET", "/motions/m-999", None, 404), ("GET", "/nope", None, 404), ("DELETE", "/stop", None, 405)]
        for method, path, body, want in bad:
            def raw(method=method, path=path, body=body):
                import urllib.request, urllib.error
                req = urllib.request.Request(base + path, data=body, method=method,
                                             headers={"Content-Type": "application/json"})
                try:
                    return urllib.request.urlopen(req, timeout=5).status
                except urllib.error.HTTPError as e:
                    return e.code
            got = await call(raw)
            assert got == want, (method, path, got, want)
        print("ok  invalid requests rejected:", [b[3] for b in bad])

        # go --wait through the CLI, then stop via the CLI
        rc = await call(ctl.main, ["--url", base, "go", "top", "--wait"])
        assert rc == 0, rc
        m = await call(ctl.post_json, base, "/motions", {"view": "left"})
        await asyncio.sleep(0.15)
        rc = await call(ctl.main, ["--url", base, "stop"])
        st = await call(ctl.get_json, base, "/status")
        assert rc == 0 and st["motion"]["state"] == "stopped" and st["state"] == "holding", st
        print("ok  ctl.py go --wait and ctl.py stop")
        server.close()


asyncio.run(manager_tests())
asyncio.run(http_tests())
print("ALL OK")
