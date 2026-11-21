"""MCP server camera-arm against capture_server.py on the simulated arm. No hardware, no Station.

    .venv/bin/python tests/test_mcp_sim.py      # about a minute, ends with ALL OK

capture_server's State and HTTP server run in this process on a free port (like Part A of
test_server_sim.py), with the motion speeds raised so the simulated moves are quick. The MCP
server is reached with the SDK's in-process client, so every tool call goes the real way:
MCP -> /api/... -> State -> motion.go_to_view -> SimArm.
"""
import asyncio, json, os, sys, tempfile, threading, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import motion
import capture_server
from capture_server import State, Server, make_handler
from mcp import Client
from mcp_server import Control, SceneStore, build_server, fetch_views

motion.LOG_PATH = Path(os.devnull)
# simulated moves at test speed (the real arm keeps its limits: this only changes this process)
motion.SPEED_FIRST = motion.SPEED_SLOW = motion.SPEED_NORMAL = 1500
capture_server.SPEEDS = {"slow": 1500, "normal": 1500}

tmp = Path(tempfile.mkdtemp())


def start_arm_server():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    state = State(loop, sim=True)
    state.call(state.start_sim(), timeout=30)
    loop.call_soon_threadsafe(state.refresh_where)
    httpd = Server(("127.0.0.1", 0), make_handler(state), state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return state, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def data(res):
    """Structured result of a tool call, or {"_error": text} for a tool error."""
    if res.is_error:
        return {"_error": res.content[0].text}
    return res.structured_content


async def wait_idle(client, timeout=60):
    t = time.monotonic()
    while True:
        st = data(await client.call_tool("get_status", {}))
        if not st["moving"] and st["current_view"] is not None or st.get("last_result"):
            if not st["moving"]:
                return st
        assert time.monotonic() - t < timeout, f"move did not end: {st}"
        await asyncio.sleep(0.3)


async def wait_where(client, timeout=10):
    """capture_server refreshes 'where the arm is' about once a second after a move."""
    t = time.monotonic()
    while True:
        st = data(await client.call_tool("get_status", {}))
        if st["current_view"] is not None and not st["moving"]:
            return st
        assert time.monotonic() - t < timeout, f"no current view: {st}"
        await asyncio.sleep(0.3)


async def main():
    state, httpd, base = start_arm_server()
    control = Control(base)
    views = await fetch_views(control, 5)
    names = [v["name"] for v in views]
    mcp = build_server(control, SceneStore(tmp / "scene.json"), views)
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"list_views", "go_to_view", "get_status", "stop", "get_camera_image",
                              "get_scene", "update_scene", "remove_scene_object"}, set(tools)
        schema = json.dumps(tools["go_to_view"].input_schema)
        assert "enum" in schema and all(n in schema for n in names) and "transit" not in names, schema
        print("ok  8 tools; go_to_view only accepts the saved views:", names)

        st = await wait_where(client)
        start_view = st["current_view"]
        lv = data(await client.call_tool("list_views", {}))
        assert [v["name"] for v in lv["views"]] == names and lv["current_view"] == start_view
        assert "joints" not in json.dumps(lv["views"]) and all("checked" in v for v in lv["views"])
        print("ok  list_views without joint values; camera at", start_view)

        assert (await client.call_tool("go_to_view", {"view": "kitchen"})).is_error
        same = data(await client.call_tool("go_to_view", {"view": start_view}))
        assert same["started"] is False, same
        print("ok  unknown view rejected by the schema; current view is a no-op")

        target = "top" if start_view != "top" else "wide"
        t = time.monotonic()
        g = data(await client.call_tool("go_to_view", {"view": target}))
        dt = time.monotonic() - t
        assert g["started"] is True and dt < 1.0, (g, dt)
        busy = data(await client.call_tool("go_to_view", {"view": "home"}))
        assert '"busy"' in busy["_error"] or "press STOP first" in busy["_error"], busy
        img = await client.call_tool("get_camera_image", {})
        assert img.is_error and "moving" in img.content[0].text, img.content[0].text
        st = data(await client.call_tool("get_status", {}))
        assert st["moving"] and st["target_view"] == target and set(st["phase"].split("+")) <= {"check", "rise", "turn", "descend", "roll", "direct", "arc"}, st
        st = await wait_idle(client)
        assert st["last_result"]["ok"] is True and st["last_result"]["view"] == target, st
        st = await wait_where(client)
        assert st["current_view"] == target, st
        print(f"ok  go_to_view returned in {dt * 1000:.0f} ms; busy and no image while moving; "
              f"arrived via {st['last_result'].get('route')}")

        img = await client.call_tool("get_camera_image", {})
        assert not img.is_error, img.content[0].text
        assert img.content[0].type == "image" and img.content[0].mime_type == "image/jpeg"
        meta = json.loads(img.content[1].text)
        assert meta["view"] == target, meta
        print(f"ok  get_camera_image: sim snapshot of {meta['view']!r}")

        other = "wide" if target != "wide" else "left"
        g = data(await client.call_tool("go_to_view", {"view": other}))
        assert g["started"], g
        await asyncio.sleep(0.4)
        s = data(await client.call_tool("stop", {}))
        assert s["ok"] and s["was_moving"] is True and s["hold"] == "confirmed", s
        st = data(await client.call_tool("get_status", {}))
        assert not st["moving"], st
        print("ok  stop mid-move: hold", s["hold"], "; blockers now:", st["blockers"][:1])

        pcb = {"id": "pcb", "label": "Circuit board", "description": "green PCB",
               "seen": [{"view": target, "bbox": {"cx": 0.5, "cy": 0.45, "w": 0.4, "h": 0.3}, "visible": 0.9}]}
        sc = data(await client.call_tool("update_scene", {"objects": [pcb]}))
        assert sc["count"] == 1 and sc["objects"][0]["seen"][0]["age_s"] == 0, sc
        pcb2 = dict(pcb, seen=[{"view": target, "bbox": {"cx": 0.52, "cy": 0.47, "w": 0.38, "h": 0.3}, "visible": 0.6}])
        iron = {"id": "soldering_iron", "label": "Soldering iron",
                "seen": [{"view": target, "bbox": {"cx": 0.8, "cy": 0.6, "w": 0.2, "h": 0.1}}]}
        sc = data(await client.call_tool("update_scene", {"objects": [pcb2, iron]}))
        p = next(o for o in sc["objects"] if o["id"] == "pcb")
        assert sc["count"] == 2 and len(p["seen"]) == 1 and p["seen"][0]["visible"] == 0.6, sc
        lv = data(await client.call_tool("list_views", {}))
        assert next(v for v in lv["views"] if v["name"] == target)["shows"] == ["pcb", "soldering_iron"], lv
        for bad, why in [
            (dict(pcb, seen=[{"view": "kitchen", "bbox": {"cx": .5, "cy": .5, "w": .1, "h": .1}}]), "unknown view"),
            (dict(pcb, seen=[{"view": target, "bbox": {"cx": 1.5, "cy": .5, "w": .1, "h": .1}}]), "bbox > 1"),
            (dict(pcb, id="PCB with spaces"), "bad id"),
        ]:
            assert (await client.call_tool("update_scene", {"objects": [bad]})).is_error, why
        assert data(await client.call_tool("get_scene", {}))["count"] == 2
        assert data(await client.call_tool("remove_scene_object", {"id": "soldering_iron"}))["count"] == 1
        assert (await client.call_tool("remove_scene_object", {"id": "nope"})).is_error
        print("ok  scene: merge by id and view, list_views shows, validation, remove")

        guide = await client.read_resource("camera-arm://guide")
        assert "go_to_view" in guide.contents[0].text
        res = await client.read_resource("camera-arm://scene")
        assert json.loads(res.contents[0].text)["count"] == 1
        print("ok  resources guide and scene")

    # arm web server gone -> clear tool error, not a crash
    state.call(state.shutdown(), timeout=10)
    httpd.shutdown(); httpd.server_close()
    async with Client(mcp) as client:
        r = await client.call_tool("get_status", {})
        assert r.is_error and "arm_server_unreachable" in r.content[0].text, r.content[0].text
        print("ok  web server down -> tool error arm_server_unreachable")


asyncio.run(main())
print("ALL OK")
