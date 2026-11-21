"""MCP server camera-arm against the control service on the simulated arm. No hardware, no Station.

    .venv/bin/python tests/test_mcp_sim.py

The control service runs in-process on a free port; the MCP server is reached with the SDK's
in-process client, so every tool call goes the real way: MCP -> REST -> motion manager -> SimArm.
"""
import asyncio, json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import Client
from control_server import ControlServer
from mcp_server import Control, SceneStore, build_server, fetch_views
from motion_manager import MotionManager
from sim_arm import SimArm

tmp = Path(tempfile.mkdtemp())
VIEWS = tmp / "views.json"
VIEWS.write_text(json.dumps({"views": {
    "home": {"joints": {"1": 2048, "2": 1000, "3": 3000, "4": 2048, "5": 2048, "6": 2000}, "description": "folded"},
    "left": {"joints": {"1": 1400, "2": 1800, "3": 2200, "4": 2500, "5": 2048, "6": 3000}, "description": "from the left",
             "aliases": ["links"]},
    "top":  {"joints": {"1": 2600, "2": 1600, "3": 2400, "4": 2300, "5": 2048, "6": 2000}, "description": "top-down",
             "via": ["home"]},
}}))


class FakeCamera:
    async def still(self):
        return b"\xff\xd8fake-jpeg\xff\xd9"


def data(res):
    """Structured result of a tool call (or the parsed text of an error)."""
    if res.is_error:
        text = res.content[0].text            # "Error executing tool x: {json}" for control errors
        return {"_error": text[text.find("{"):] if "{" in text else text}
    return res.structured_content


async def wait_state(client, motion_id, timeout=10):
    t = time.monotonic()
    while True:
        st = data(await client.call_tool("get_status", {"motion_id": motion_id}))
        if st["motion"]["state"] != "moving":
            return st
        assert time.monotonic() - t < timeout, "move did not end"
        await asyncio.sleep(0.2)


async def main():
    arm = SimArm(start={1: 2048, 2: 1000, 3: 3000, 4: 2048, 5: 2048, 6: 2000}, speed=20000)
    mgr = MotionManager(arm, views_path=VIEWS, move_kwargs={"min_duration": 0.8, "max_speed": 4000})
    srv = ControlServer(mgr, camera=FakeCamera())
    server = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    control = Control(base)
    async with server:
        views = await fetch_views(control, 2)
        mcp = build_server(control, SceneStore(tmp / "scene.json"), views)
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"list_views", "go_to_view", "get_status", "stop", "get_camera_image",
                                  "get_scene", "update_scene", "remove_scene_object"}, set(tools)
            schema = json.dumps(tools["go_to_view"].input_schema)
            assert all(n in schema for n in ("home", "left", "top")) and "enum" in schema, schema
            print("ok  8 tools; go_to_view only accepts the saved views:", ["home", "left", "top"])

            lv = data(await client.call_tool("list_views", {}))
            assert [v["name"] for v in lv["views"]] == ["home", "left", "top"] and lv["nearest_view"]["view"] == "home"
            assert "joints" not in lv["views"][0] and "via" not in lv["views"][0]
            print("ok  list_views without joint values, nearest view:", lv["nearest_view"]["view"])

            bad = await client.call_tool("go_to_view", {"view": "kitchen"})
            assert bad.is_error, bad
            print("ok  go_to_view('kitchen') rejected by the schema")

            t = time.monotonic()
            m = data(await client.call_tool("go_to_view", {"view": "left"}))
            dt = time.monotonic() - t
            assert m["state"] == "moving" and dt < 0.5, (m, dt)
            busy = data(await client.call_tool("go_to_view", {"view": "home"}))
            assert json.loads(busy["_error"])["error"] == "busy", busy
            img = await client.call_tool("get_camera_image", {})
            assert img.is_error and "moving" in img.content[0].text, img
            st = await wait_state(client, m["motion_id"])
            assert st["motion"]["state"] == "reached" and st["nearest_view"]["view"] == "left", st
            print(f"ok  go_to_view returns in {dt * 1000:.0f} ms, busy while moving, no image while moving, reached")

            img = await client.call_tool("get_camera_image", {})
            assert not img.is_error and img.content[0].type == "image" and img.content[0].mime_type == "image/jpeg"
            meta = json.loads(img.content[1].text)
            assert meta["nearest_view"] == "left", meta
            print("ok  get_camera_image: JPEG + nearest_view", meta["nearest_view"])

            m = data(await client.call_tool("go_to_view", {"view": "top"}))
            assert m["path"] == ["home", "top"], m
            await asyncio.sleep(0.3)
            s = data(await client.call_tool("stop", {}))
            assert s["motion"]["state"] == "stopped" and s["state"] == "holding", s
            print("ok  stop mid-move:", s["motion"]["state"])

            pcb = {"id": "pcb", "label": "Circuit board", "description": "green PCB",
                   "seen": [{"view": "left", "bbox": {"cx": 0.5, "cy": 0.45, "w": 0.4, "h": 0.3}, "visible": 0.9}]}
            sc = data(await client.call_tool("update_scene", {"objects": [pcb]}))
            assert sc["count"] == 1 and sc["objects"][0]["seen"][0]["age_s"] == 0, sc
            iron = {"id": "soldering_iron", "label": "Soldering iron",
                    "seen": [{"view": "left", "bbox": {"cx": 0.8, "cy": 0.6, "w": 0.2, "h": 0.1}}]}
            pcb2 = dict(pcb, seen=[{"view": "left", "bbox": {"cx": 0.52, "cy": 0.47, "w": 0.38, "h": 0.3}, "visible": 0.6}])
            sc = data(await client.call_tool("update_scene", {"objects": [pcb2, iron]}))
            p = next(o for o in sc["objects"] if o["id"] == "pcb")
            assert sc["count"] == 2 and len(p["seen"]) == 1 and p["seen"][0]["visible"] == 0.6, sc
            lv = data(await client.call_tool("list_views", {}))
            assert next(v for v in lv["views"] if v["name"] == "left")["shows"] == ["pcb", "soldering_iron"], lv
            print("ok  update_scene merges by id and view; list_views shows what each view sees")

            for bad_obj, why in [
                (dict(pcb, seen=[{"view": "kitchen", "bbox": {"cx": .5, "cy": .5, "w": .1, "h": .1}}]), "unknown view"),
                (dict(pcb, seen=[{"view": "left", "bbox": {"cx": 1.5, "cy": .5, "w": .1, "h": .1}}]), "bbox > 1"),
                (dict(pcb, id="PCB with spaces"), "bad id"),
            ]:
                r = await client.call_tool("update_scene", {"objects": [bad_obj]})
                assert r.is_error, why
            assert data(await client.call_tool("get_scene", {}))["count"] == 2
            print("ok  update_scene rejects unknown views, boxes outside 0..1 and bad ids; scene unchanged")

            sc = data(await client.call_tool("remove_scene_object", {"id": "soldering_iron"}))
            assert sc["count"] == 1
            assert (await client.call_tool("remove_scene_object", {"id": "nope"})).is_error
            guide = await client.read_resource("camera-arm://guide")
            assert "go_to_view" in guide.contents[0].text
            res = await client.read_resource("camera-arm://scene")
            assert json.loads(res.contents[0].text)["count"] == 1
            print("ok  remove_scene_object, resources guide and scene")

        # control service down -> clear tool error, not a crash
        server.close()
    async with Client(mcp) as client:
        r = await client.call_tool("get_status", {})
        assert r.is_error and "control_unreachable" in r.content[0].text, r.content[0].text
        print("ok  control service down -> tool error control_unreachable")


asyncio.run(main())
print("ALL OK")
