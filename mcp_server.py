"""MCP server camera-arm: the tools Claude uses to point the camera. Thin layer over the control service.

    python control_server.py --sim                 # first: the control service (only writer to the arm)
    .venv/bin/python mcp_server.py                 # then: MCP at http://127.0.0.1:8200/mcp
    claude mcp add --transport http camera-arm http://127.0.0.1:8200/mcp

The server moves nothing itself: every move goes through the control service's REST API,
so its safety checks apply. Claude can only pick saved views (enum), never free joint or
Cartesian targets. Moves return at once; get_status tells how they ended.
The scene (objects seen in the camera image) lives here, in scene.json.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal, Optional

import mcp_types as types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

log = logging.getLogger("mcp_server")

ROOT = Path(__file__).parent
SCENE_PATH = ROOT / "scene.json"
VERSION = "0.1.0"
MAX_OBJECTS = 30

GUIDE = """# camera-arm: operating guide for Claude

A robot arm holds a camera above a desk where a person assembles and solders electronics.
You choose where the camera looks. You cannot steer the arm freely: you pick one of the saved
views (list_views), and the control service moves the arm there safely.

## Moving
- go_to_view(view) returns at once with a motion_id and state "moving". It does not wait.
- To learn how it ended, call get_status once after a few seconds (a move takes 2-6 s).
  States: reached = there; stalled = a joint stopped short (blocked?); timeout; stopped (someone
  pressed stop); error (arm data lost). Only "reached" means the camera shows the new view.
- Only one move at a time. A request while moving fails with "busy": wait, or stop first if the
  user asked for something else.
- stop() halts the arm where it is, at any time. Use it whenever the user says stop, wait, halt or
  similar, before anything else.
- Never loop on moves or status. One call, then decide.

## Images and the scene
- get_camera_image() returns the current camera image, only while the arm is not moving.
  Call it when the user asks what is visible or asks you to find something.
- After looking at an image, record what you saw with update_scene: one object per thing that
  matters for filming (pcb, soldering_iron, hands, parts tray, multimeter ...).
  bbox is the box around the object in that image, normalized 0..1: cx, cy = centre (0,0 = top left),
  w, h = width and height as fractions of the image. visible = how much of the object is not covered (0..1).
  Use the view name the image was taken at (nearest_view from get_camera_image).
- get_scene() returns what has been recorded, with how old each sighting is. Old sightings may be
  wrong: the person moves things.

## Choosing a view
Match the user's words to a view's name, description and aliases. If the user wants to see an
object, prefer a view whose sightings show it large and visible. If nothing fits, say so and
offer the closest view instead of guessing.
"""

INSTRUCTIONS = (
    "You point a camera held by a robot arm. Pick saved views with go_to_view (returns at once; check "
    "get_status after a few seconds), stop() whenever the user says stop. get_camera_image shows the current "
    "picture; record what you see with update_scene (bbox normalized 0..1). Read camera-arm://guide for the rules."
)


# -- control service client ------------------------------------------------------

class ControlError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class Control:
    """Minimal REST client for control_server.py (standard library, run in a thread)."""

    def __init__(self, base: str, timeout: float = 10):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, data: dict | None, raw: bool):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"} if body else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
                return (payload, dict(r.headers)) if raw else json.loads(payload)
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
                raise ControlError(e.code, err.get("error", "error"), err.get("detail", ""))
            except (json.JSONDecodeError, AttributeError):
                raise ControlError(e.code, "error", f"HTTP {e.code}")
        except urllib.error.URLError as e:
            raise ControlError(503, "control_unreachable",
                               f"control service not reachable at {self.base} ({e.reason})")

    async def call(self, method: str, path: str, data: dict | None = None, raw: bool = False):
        return await asyncio.to_thread(self._call, method, path, data, raw)


# -- scene -------------------------------------------------------------------------

class BBox(BaseModel):
    cx: float = Field(ge=0, le=1, description="box centre x, 0 = left edge, 1 = right edge")
    cy: float = Field(ge=0, le=1, description="box centre y, 0 = top edge, 1 = bottom edge")
    w: float = Field(gt=0, le=1, description="box width as a fraction of the image width")
    h: float = Field(gt=0, le=1, description="box height as a fraction of the image height")


class Sighting(BaseModel):
    view: str = Field(description="view the image was taken at (nearest_view from get_camera_image)")
    camera: str = Field("arm", max_length=20)
    bbox: BBox
    visible: float = Field(1.0, ge=0, le=1, description="fraction of the object not covered, e.g. by hands")


class SceneObject(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$", description="short stable id, e.g. pcb, soldering_iron, left_hand")
    label: str = Field(max_length=60)
    description: str = Field("", max_length=300)
    seen: list[Sighting] = Field(default_factory=list, max_length=10)


class SceneStore:
    """scene.json: {"objects": {id: {id, label, description, seen: [{view, camera, bbox, visible, at}]}}}"""

    def __init__(self, path: Path = SCENE_PATH):
        self.path = path
        self.lock = asyncio.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {"objects": {}}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            log.warning("%s is not valid JSON; starting an empty scene", self.path)
            return {"objects": {}}

    def save(self, scene: dict):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(scene, indent=1))
        os.replace(tmp, self.path)

    @staticmethod
    def with_ages(scene: dict) -> dict:
        now = time.time()
        out = []
        for obj in scene["objects"].values():
            o = dict(obj)
            o["seen"] = [{**s, "age_s": round(now - s.get("at", now))} for s in obj.get("seen", [])]
            out.append(o)
        return {"objects": out, "count": len(out)}

    async def update(self, objects: list[SceneObject], known_views: set[str], replace: bool) -> dict:
        bad = sorted({s.view for o in objects for s in o.seen if s.view not in known_views})
        if bad:
            raise ToolError(f"unknown view(s) {bad}; known: {sorted(known_views)}")
        async with self.lock:
            scene = {"objects": {}} if replace else self.load()
            now = round(time.time(), 1)
            for o in objects:
                cur = scene["objects"].get(o.id, {"id": o.id, "seen": []})
                cur["label"] = o.label
                if o.description or "description" not in cur:
                    cur["description"] = o.description
                seen = [s for s in cur.get("seen", [])
                        if not any(s["view"] == n.view and s.get("camera") == n.camera for n in o.seen)]
                seen += [{**n.model_dump(), "at": now} for n in o.seen]
                cur["seen"] = seen[-10:]
                scene["objects"][o.id] = cur
            if len(scene["objects"]) > MAX_OBJECTS:
                raise ToolError(f"scene would hold {len(scene['objects'])} objects; max {MAX_OBJECTS}. "
                                "Remove some or use replace=true.")
            self.save(scene)
            return self.with_ages(scene)

    async def remove(self, obj_id: str) -> dict:
        async with self.lock:
            scene = self.load()
            if scene["objects"].pop(obj_id, None) is None:
                raise ToolError(f"no object {obj_id!r} in the scene")
            self.save(scene)
            return self.with_ages(scene)


# -- MCP server --------------------------------------------------------------------

def _tool_error(e: ControlError) -> ToolError:
    return ToolError(json.dumps({"status": e.status, "error": e.code, "detail": e.detail}))


def build_server(control: Control, scene: SceneStore, views: list[dict]) -> MCPServer:
    names = [v["name"] for v in views]
    if not names:
        raise RuntimeError("the control service has no views; capture some first (capture_server.py)")
    ViewName = Literal[tuple(names)]  # Claude can only pick saved views
    known = set(names)

    mcp = MCPServer("camera-arm", instructions=INSTRUCTIONS, version=VERSION)

    async def status_brief() -> dict:
        st = await control.call("GET", "/status")
        return {"state": st["state"], "motion": st["motion"], "nearest_view": st["nearest_view"],
                "last_error": st["last_error"]}

    @mcp.tool(description="The saved camera views: name, description, aliases, and which recorded scene objects "
                          "were seen from each. Also where the arm is now (nearest_view).")
    async def list_views() -> dict[str, Any]:
        try:
            vs = (await control.call("GET", "/views"))["views"]
            st = await status_brief()
        except ControlError as e:
            raise _tool_error(e)
        objs = scene.load()["objects"].values()
        out = []
        for v in vs:
            shows = [o["id"] for o in objs if any(s["view"] == v["name"] for s in o.get("seen", []))]
            out.append({"name": v["name"], "description": v["description"], "aliases": v["aliases"],
                        "shows": shows})
        return {"views": out, "nearest_view": st["nearest_view"], "state": st["state"]}

    @mcp.tool(description="Move the camera to a saved view. Returns at once with a motion_id; the move takes "
                          "2-6 s. Call get_status afterwards to see whether it was reached. Fails with 'busy' "
                          "while another move runs.")
    async def go_to_view(view: ViewName) -> dict[str, Any]:
        try:
            m = await control.call("POST", "/motions", {"view": view})
        except ControlError as e:
            raise _tool_error(e)
        return {"motion_id": m["motion_id"], "view": m["view"], "path": m["path"], "state": m["state"],
                "hint": "moving; call get_status in a few seconds"}

    @mcp.tool(description="Where the arm is and how the last (or a given) move went: state idle|moving|holding, "
                          "motion {motion_id, view, state reached|stalled|timeout|stopped|error|moving, progress, "
                          "error}, nearest_view {view, distance in encoder steps, at}.")
    async def get_status(motion_id: Optional[str] = None) -> dict[str, Any]:
        try:
            st = await status_brief()
            if motion_id:
                st["motion"] = await control.call("GET", f"/motions/{motion_id}")
        except ControlError as e:
            raise _tool_error(e)
        return st

    @mcp.tool(description="Stop the arm immediately and hold it where it is. Works at any time; use it first "
                          "whenever the user says stop, wait or halt.")
    async def stop() -> dict[str, Any]:
        try:
            st = await control.call("POST", "/stop")
        except ControlError as e:
            raise _tool_error(e)
        return {"state": st["state"], "motion": st["motion"], "nearest_view": st["nearest_view"]}

    @mcp.tool(structured_output=False,
              description="The current camera image as JPEG, plus the view it was taken at. Only while the arm "
                          "is not moving (error 'moving' otherwise).")
    async def get_camera_image() -> list[types.ImageContent | types.TextContent]:
        try:
            jpeg, headers = await control.call("GET", "/still.jpg", raw=True)
        except ControlError as e:
            raise _tool_error(e)
        meta = {"nearest_view": headers.get("X-Nearest-View") or None,
                "taken_at": float(headers.get("X-Taken-At", time.time()))}
        return [types.ImageContent(type="image", data=base64.b64encode(jpeg).decode(), mime_type="image/jpeg"),
                types.TextContent(type="text", text=json.dumps(meta))]

    @mcp.tool(description="Objects recorded in the scene, each with the views it was seen from, its box in that "
                          "image (normalized 0..1), how visible it was and how many seconds ago.")
    async def get_scene() -> dict[str, Any]:
        return scene.with_ages(scene.load())

    @mcp.tool(description="Record what you saw in a camera image. Objects are merged by id; a sighting replaces "
                          "the earlier one from the same view and camera. replace=true clears the scene first. "
                          "bbox: cx, cy = centre, w, h = size, all as fractions of the image (0..1, 0,0 = top left).")
    async def update_scene(objects: list[SceneObject], replace: bool = False) -> dict[str, Any]:
        return await scene.update(objects, known, replace)

    @mcp.tool(description="Remove one object from the scene, e.g. when it has been taken away.")
    async def remove_scene_object(id: str) -> dict[str, Any]:
        return await scene.remove(id)

    @mcp.resource("camera-arm://guide", name="camera-arm operating guide", mime_type="text/markdown",
                  description="How to move the camera, read images and record the scene.")
    def guide() -> str:
        return GUIDE

    @mcp.resource("camera-arm://scene", name="Recorded scene", mime_type="application/json",
                  description="Objects seen in camera images, with boxes per view and their age.")
    def scene_resource() -> str:
        return json.dumps(scene.with_ages(scene.load()))

    return mcp


async def fetch_views(control: Control, wait_s: float) -> list[dict]:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return (await control.call("GET", "/views"))["views"]
        except ControlError as e:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{e.detail}. Start control_server.py first.")
            await asyncio.sleep(1)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--control", default="http://127.0.0.1:8100", help="control service URL")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    p.add_argument("--scene", type=Path, default=SCENE_PATH)
    p.add_argument("--wait", type=float, default=10, help="seconds to wait for the control service at start")
    return p.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args(argv)
    control = Control(args.control)
    views = asyncio.run(fetch_views(control, args.wait))
    mcp = build_server(control, SceneStore(args.scene), views)
    log.info("camera-arm MCP on http://%s:%d/mcp  control=%s  views=%s",
             args.host, args.port, args.control, [v["name"] for v in views])
    mcp.run("streamable-http", host=args.host, port=args.port, streamable_http_path="/mcp",
            stateless_http=True, json_response=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        raise SystemExit(f"error: {e}")
