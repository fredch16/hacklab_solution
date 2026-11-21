"""voice_bridge.py: the local grammar, then the whole path on the simulated arm. No hardware.

    .venv/bin/python tests/test_voice_bridge.py      # ends with ALL OK

Part A checks plan() on a fixed view list. Part B runs capture_server --sim and mcp_server in this
process (like test_mcp_sim.py) and the bridge's HTTP endpoint on a free port, then posts transcripts
the way the dashboard does: Origin checks, a real go_to_view on SimArm, stop, refusals, dedupe.
"""
import json, os, sys, threading, time, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import voice_bridge as vb

VIEWS = [{"name": "top", "checked": True, "aliases": ["overhead", "bird's eye"]},
         {"name": "wide", "checked": True, "aliases": []},
         {"name": "home", "checked": True, "aliases": []},
         {"name": "side_low", "checked": False, "aliases": []}]


def part_a():
    p = lambda t: vb.plan(t, VIEWS)
    goto = lambda v: {"tool": "go_to_view", "args": {"view": v}}
    stop = {"tool": "stop", "args": {}}
    for t in ["Go to top.", "move to the top view", "look at the top", "please go to the overhead view",
              "show me the top", "top", "Camera, switch to top please", "go to bird's eye"]:
        assert p(t) == goto("top"), (t, p(t))
    assert p("go home") == goto("home") and p("Wide shot") == goto("wide"), p("Wide shot")
    for t in ["Stop!", "stop", "halt", "wait", "hold on", "stop, go to top", "wait go to wide", "emergency stop"]:
        assert p(t) == stop, (t, p(t))
    for t in ["stop recording", "start recording", "record", "stop the recording now"]:
        assert p(t) == {"tool": None, "reason": "recording"}, (t, p(t))
    assert p("go to side low") == {"tool": None, "reason": "unchecked_view", "view": "side_low"}
    assert p("go to the kitchen")["reason"] == "unknown_view"
    assert p("where are you")["tool"] == "get_status" and p("what's the status")["tool"] == "get_status"
    assert p("list views")["tool"] == "list_views" and p("which views are there")["tool"] == "list_views"
    assert p("turn towards the window")["reason"] == "unknown_view"
    assert p("tell me a joke") == {"tool": None, "reason": "no_match"}
    assert p("   ") == {"tool": None, "reason": "empty"}
    assert vb.origin_allowed("https://192.168.1.20:8443", set()) and vb.origin_allowed("https://localhost:8443", set())
    assert vb.origin_allowed(None, set()) and vb.origin_allowed("https://phone.lan", {"https://phone.lan"})
    for o in ["https://evil.example", "http://192.168.1.20:8443", "https://8.8.8.8", "null"]:
        assert not vb.origin_allowed(o, set()), o
    print("A grammar + origins OK")


def post(base, body, origin="https://192.168.1.20:8443", ctype="application/json"):
    h = {"Content-Type": ctype}
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(base + "/api/v1/voice-command", json.dumps(body).encode(), h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def start_arm_server():
    """capture_server's State and HTTP server in this process on a free port, on SimArm."""
    import asyncio
    from capture_server import State, Server, make_handler
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    state = State(loop, sim=True)
    state.call(state.start_sim(), timeout=30)
    loop.call_soon_threadsafe(state.refresh_where)
    httpd = Server(("127.0.0.1", 0), make_handler(state), state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return state, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def part_b():
    import asyncio, tempfile
    import motion, capture_server
    from mcp import Client
    from mcp_server import Control, SceneStore, build_server, fetch_views
    motion.LOG_PATH = Path(os.devnull)
    motion.SPEED_FIRST = motion.SPEED_SLOW = motion.SPEED_NORMAL = 1500
    capture_server.SPEEDS = {"slow": 1500, "normal": 1500}

    state, httpd, base = start_arm_server()
    control = Control(base)
    views = asyncio.run(fetch_views(control, 5))
    mcp = build_server(control, SceneStore(Path(tempfile.mkdtemp()) / "scene.json"), views)
    cards = {v["name"]: v for v in asyncio.run(_list_views(mcp))}
    checked = [n for n, v in cards.items() if v["checked"]]
    unchecked = [n for n, v in cards.items() if not v["checked"]]
    print("sim views: checked", checked, "unchecked", unchecked)
    assert checked, "need at least one checked view in views.json"

    bridge = vb.Bridge(mcp)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), vb.make_handler(bridge, set()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    # preflight the browser sends before the JSON POST
    req = urllib.request.Request(url + "/api/v1/voice-command", method="OPTIONS", headers={
        "Origin": "https://192.168.1.20:8443", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 204 and r.headers["Access-Control-Allow-Private-Network"] == "true"
        assert r.headers["Access-Control-Allow-Origin"] == "https://192.168.1.20:8443"
    assert post(url, {"session_id": "s1", "utterance_id": "x", "text": "stop"}, origin="https://evil.example")[0] == 403
    assert post(url, {"session_id": "s1", "utterance_id": "x", "text": "stop"}, ctype="text/plain")[0] == 415
    assert post(url, {"session_id": "s 1", "utterance_id": "x", "text": "stop"})[0] == 400

    target = next(n for n in checked if n != "home") if len(checked) > 1 else checked[0]
    code, r, h = post(url, {"session_id": "s1", "utterance_id": "u1", "text": f"Go to {target.replace('_', ' ')}."})
    assert code == 200 and r["ok"] and r["command"] == "arm.go_to_view", r
    assert h["Access-Control-Allow-Origin"] == "https://192.168.1.20:8443"
    print("goto:", r["message"])
    # the same utterance again (reconnect, duplicate final) runs nothing and returns the first result
    assert post(url, {"session_id": "s1", "utterance_id": "u1", "text": f"go to {target}"})[1] == r
    st = _wait_idle(mcp)
    assert st["current_view"] == target or (st.get("last_result") or {}).get("view") == target, st
    code, r, _ = post(url, {"session_id": "s1", "utterance_id": "u2", "text": "where are you"})
    assert r["ok"] and r["command"] == "arm.status", r
    print("status:", r["message"])

    # start a move, then stop it mid-way
    other = next((n for n in checked if n != target), None)
    if other:
        motion.SPEED_FIRST = motion.SPEED_SLOW = motion.SPEED_NORMAL = 60
        capture_server.SPEEDS = {"slow": 60, "normal": 60}
        r = post(url, {"session_id": "s1", "utterance_id": "u3", "text": f"move to the {other}"})[1]
        assert r["ok"], r
        time.sleep(0.8)
        r = post(url, {"session_id": "s1", "utterance_id": "u4", "text": "Stop!"})[1]
        assert r["ok"] and r["command"] == "arm.stop", r
        print("stop:", r["message"])
        assert not _status(mcp)["moving"]
    if unchecked:
        r = post(url, {"session_id": "s1", "utterance_id": "u5", "text": f"go to {unchecked[0].replace('_', ' ')}"})[1]
        assert not r["ok"] and r["error"] == "VIEW_NOT_CHECKED", r
    r = post(url, {"session_id": "s1", "utterance_id": "u6", "text": "stop recording"})[1]
    assert r == {"utterance_id": "u6", "ok": True, "command": "none", "error": None, "message": None}, r
    r = post(url, {"session_id": "s1", "utterance_id": "u7", "text": "list views"})[1]
    assert r["ok"] and all(n in r["message"] for n in checked), r
    # MCP server unreachable -> a clear failure, nothing crashes
    dead = vb.make_handler(vb.Bridge("http://127.0.0.1:9/mcp"), set())
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), dead)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    r = post(f"http://127.0.0.1:{srv2.server_address[1]}", {"session_id": "s1", "utterance_id": "u8", "text": "stop"})[1]
    assert not r["ok"] and r["error"] == "ARM_UNREACHABLE", r
    print("B sim end to end OK")


async def _list_views(mcp):
    from mcp import Client
    async with Client(mcp) as c:
        return (await c.call_tool("list_views", {})).structured_content["views"]


def _status(mcp):
    import asyncio
    from mcp import Client

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool("get_status", {})).structured_content
    return asyncio.run(go())


def _wait_idle(mcp, timeout=60):
    t = time.monotonic()
    while True:
        st = _status(mcp)
        if not st["moving"] and st.get("current_view"):
            return st
        assert time.monotonic() - t < timeout, st
        time.sleep(0.3)


if __name__ == "__main__":
    part_a()
    part_b()
    print("ALL OK")
