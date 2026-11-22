"""MCP server camera-arm: the tools Claude uses to point the camera. Thin layer over capture_server.py.

    python3 capture_server.py --bus 5B79016329     # first: the web server, the one program that moves the arm
    python3 capture_server.py --sim --port 8891    #   (or the simulated arm)
    .venv/bin/python mcp_server.py                 # then: MCP at http://127.0.0.1:8200/mcp (--arm-url for --sim)
    claude mcp add --transport http camera-arm http://127.0.0.1:8200/mcp

The arm server owns motion and STOP. MCP exposes saved views, guarded director jobs,
and the live motor and camera data needed to choose between them.
The scene (objects seen in the camera image) lives here, in scene.json.
"""

import argparse
import asyncio
import base64
import asyncio
import json
import logging
import math
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
MAX_STILLS = 20   # image numbers remembered for update_scene / preview_frame

GUIDE = """# camera-arm: operating guide for Claude

A robot arm holds a camera above a desk. Use list_views and director_status to check the available
shots and current target. Prefer an existing view or director job when it fits the operator's request.
For a new camera move, pass the operator's brief to director_job(kind="film", brief=...).
The local director plans camera-space motion and checks the route before execution.

## Moving
- go_to_view(view) returns at once ("started"). The motion owner selects a checked glide, lift-over,
  or transit route. Views not yet checked on the real arm move at first-run speed
  (30-60 s); checked views take several seconds.
- To learn how it went, call get_status once after a while (use the "eta_hint"). moving=true means
  still on the way; otherwise current_view says where the camera is and last_result.ok whether the
  move worked. If it failed, the error says why in plain words: tell the user.
- Only one move at a time. A request while moving is refused: wait, or stop first if the user asked
  for something else. A move is also refused while the arm is in an unknown pose or limp
  (blockers in get_status say what to do; that needs a person at the arm).
- stop() halts the arm where it is, at any time. Use it whenever the user says stop, wait, halt or
  similar, before anything else. A person can also press STOP on the /control page.
- Never loop on moves or status. One call, then decide.
- get_arm_telemetry reads all eight motors, including current and goal encoder steps, calibrated and
  motion limits, load, temperature, voltage and fault status. It also reports camera geometry and blockers.
- director_job exposes survey, recall, cinematic, tour, watch, show, gimbal, scene, film, center,
  selfcal, snap and return. Use center for a dry-run framing preview. Scene and return require
  computed-path approval for free-camera motion. Start selfcal only when the operator is ready and
  the marked object can remain still through the calibration views.

## Recording video
- record_camera(camera, action) starts or stops one recording on an explicit request.
- For recording without a named camera, choose phone. Choose robot only when the operator names it.
- Robot recording requires a visible dashboard and live feed. The arm stop tool controls motion separately.

## Images and the scene
- get_camera_image() returns the current camera image, only while the arm is not moving.
  Call it when the user asks what is visible or asks you to find something.
- After looking at an image, record what you saw with update_scene: one object per thing that
  matters for filming (pcb, soldering_iron, hands, parts tray, multimeter ...).
  bbox is the box around the object in that image, normalized 0..1: cx, cy = centre (0,0 = top left),
  w, h = width and height as fractions of the image. visible = how much of the object is not covered (0..1).
  Give the image number ('still' from get_camera_image) with each sighting; the view is then filled in.
- best_view(object_id) moves to the saved view that showed the object best so far.
- preview_frame(object_id) only computes where the camera would go to frame the object from where it
  is now. It moves nothing (stage 2 preview). Tell the user the result in plain words.
- get_scene() returns what has been recorded, with how old each sighting is. Old sightings may be
  wrong: the person moves things.

## Choosing a view
Match the user's words to a view's name, description and aliases. If the user wants to see an
object, prefer a view whose sightings show it large and visible. If nothing fits, say so and
offer the closest view instead of guessing.
"""

INSTRUCTIONS = (
    "You direct a camera held by a robot arm. Read list_views and director_status for available shots "
    "and the marked target. Read get_arm_telemetry for all eight motors, camera geometry and blockers. "
    "Prefer go_to_view or an existing director_job when it matches the request. Use director_job "
    "with kind film and the user's brief when it calls for original motion. The local motion owner "
    "checks and executes the camera move. An accepted command is asynchronous. Check get_status "
    "or director_status for the outcome. Call stop immediately when the user asks to stop. "
    "Read camera-arm://guide for capabilities and operating rules."
)


# -- control service client ------------------------------------------------------

class ControlError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


ERROR_CODES = {400: "invalid_request", 403: "forbidden", 404: "not_found", 409: "refused", 500: "error",
               503: "unavailable", 504: "timeout"}


class Control:
    """Minimal client for capture_server.py's /api (standard library, run in a thread)."""

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
                detail = err.get("error") or err.get("detail") or f"HTTP {e.code}"
            except (json.JSONDecodeError, AttributeError):
                detail = f"HTTP {e.code}"
            raise ControlError(e.code, ERROR_CODES.get(e.code, "error"), str(detail))
        except urllib.error.URLError as e:
            raise ControlError(503, "arm_server_unreachable",
                               f"arm web server not reachable at {self.base} ({e.reason}); start capture_server.py")

    async def call(self, method: str, path: str, data: dict | None = None, raw: bool = False):
        return await asyncio.to_thread(self._call, method, path, data, raw)


# -- scene -------------------------------------------------------------------------

class BBox(BaseModel):
    cx: float = Field(ge=0, le=1, description="box centre x, 0 = left edge, 1 = right edge")
    cy: float = Field(ge=0, le=1, description="box centre y, 0 = top edge, 1 = bottom edge")
    w: float = Field(gt=0, le=1, description="box width as a fraction of the image width")
    h: float = Field(gt=0, le=1, description="box height as a fraction of the image height")


class Sighting(BaseModel):
    view: Optional[str] = Field(None, description="view the image was taken at ('view' from get_camera_image); "
                                                  "may be left out when 'still' is given")
    still: Optional[int] = Field(None, description="number of the image the box is from ('still' from "
                                                   "get_camera_image); needed for preview_frame")
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
            o["seen"] = [{**{k: v for k, v in s.items() if k != "pose"}, "age_s": round(now - s.get("at", now)),
                          **({"has_pose": True} if "pose" in s else {})} for s in obj.get("seen", [])]
            out.append(o)
        return {"objects": out, "count": len(out)}

    async def update(self, objects: list[SceneObject], known_views: set[str], replace: bool,
                     stills: dict[int, dict] | None = None) -> dict:
        stills = stills or {}
        for o in objects:
            for s in o.seen:
                if s.still is not None:
                    if s.still not in stills:
                        raise ToolError(f"unknown image number {s.still}; use 'still' from a recent get_camera_image")
                    s.view = s.view or stills[s.still]["view"]
                if not s.view and s.still is None:
                    raise ToolError("each sighting needs 'view' or 'still'")
        bad = sorted({s.view for o in objects for s in o.seen if s.view and s.view not in known_views})
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
                        if not any(s.get("view") == n.view and s.get("camera") == n.camera
                                   and (n.view or s.get("still") == n.still) for n in o.seen)]
                for n in o.seen:
                    entry = {**n.model_dump(exclude_none=True), "at": now}
                    if n.still is not None:   # the arm pose and image size the box belongs to
                        entry.update(pose=stills[n.still]["joints"], image_size=stills[n.still]["image_size"])
                    seen.append(entry)
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

def rank_views(obj: dict, known: set[str], now: float | None = None) -> list[dict]:
    """Score each saved view by the object's sighting from it: large, centred, visible and recent is better."""
    now = now or time.time()
    best: dict[str, dict] = {}
    for s in obj.get("seen", []):
        v = s.get("view")
        if not v or v not in known:
            continue
        b = s["bbox"]
        size = min(1.0, (b["w"] * b["h"]) / 0.25)                       # a quarter of the image counts as full
        off = min(1.0, math.hypot(b["cx"] - 0.5, b["cy"] - 0.5) / 0.5)
        age = now - s.get("at", now)
        score = s.get("visible", 1.0) * (0.3 + 0.7 * size) * (1 - 0.5 * off) * (0.6 if age > 600 else 1.0)
        if v not in best or score > best[v]["score"]:
            best[v] = {"view": v, "score": round(score, 3), "visible": s.get("visible", 1.0),
                       "size": round(b["w"] * b["h"], 3), "age_s": round(age)}
    return sorted(best.values(), key=lambda r: -r["score"])


def _tool_error(e: ControlError) -> ToolError:
    return ToolError(json.dumps({"status": e.status, "error": e.code, "detail": e.detail}))


def _overall_progress(m: dict) -> float:
    n = m.get("phase_count") or 0
    if not m.get("moving") or n <= 0:
        return 1.0 if (m.get("last_result") or {}).get("ok") else 0.0
    return round((max(m.get("phase_index", 1), 1) - 1 + (m.get("progress") or 0.0)) / n, 3)


def status_from(st: dict) -> dict:
    """What Claude needs from capture_server's /api/state (no joint values)."""
    m = st.get("motion") or {}
    last = m.get("last_result") or None
    return {
        "moving": bool(m.get("moving")),
        "target_view": m.get("view"),
        "phase": m.get("phase"), "route": m.get("route") or [], "progress": _overall_progress(m),
        "current_view": st.get("current_view"),
        "can_move": st.get("can_move"),
        "blockers": st.get("blockers") or [],
        "stopping": st.get("stopping"),
        "hold": st.get("hold"),
        "error": m.get("error"),
        "last_result": ({k: last.get(k) for k in ("ok", "view", "error", "duration", "route") if k in last}
                        if isinstance(last, dict) else None),
    }


def build_server(control: Control, scene: SceneStore, views: list[dict], dashboard: Control | None = None) -> MCPServer:
    names = [v["name"] for v in views]
    if not names:
        raise RuntimeError("the arm web server has no views; capture some first (capture_server.py, page /)")
    ViewName = Literal[tuple(names)]  # Claude can only pick saved views
    known = set(names)

    mcp = MCPServer("camera-arm", instructions=INSTRUCTIONS, version=VERSION)
    stills: dict[int, dict] = {}   # image number -> {joints, image_size, view} of the last MAX_STILLS images
    still_no = 0

    async def state() -> dict:
        try:
            return await control.call("GET", "/api/state")
        except ControlError as e:
            raise _tool_error(e)

    @mcp.tool(description="The saved camera views: name, description, aliases, whether the view was already "
                          "checked on the real arm (unchecked = slow first-run moves), and which recorded scene "
                          "objects were seen from each. Also where the camera is now (current_view).")
    async def list_views() -> dict[str, Any]:
        st = await state()
        raw = st.get("views") or {}
        objs = scene.load()["objects"].values()
        out = []
        for c in st.get("cards") or []:
            n = c["name"]
            shows = [o["id"] for o in objs if any(s["view"] == n for s in o.get("seen", []))]
            out.append({"name": n, "description": c.get("description", ""),
                        "aliases": list((raw.get(n) or {}).get("aliases", [])),
                        "checked": bool(c.get("checked")), "shows": shows})
        brief = status_from(st)
        return {"views": out, "current_view": brief["current_view"], "moving": brief["moving"],
                "can_move": brief["can_move"], "blockers": brief["blockers"]}

    @mcp.tool(description="Move the camera to a saved view. Returns at once; the move runs rise -> turn -> "
                          "descend and takes seconds (checked views) up to a minute (unchecked views, first-run "
                          "speed). Call get_status later. Refused while another move runs or while the arm is in "
                          "an unknown pose.")
    async def go_to_view(view: ViewName) -> dict[str, Any]:
        st = await state()
        brief = status_from(st)
        if brief["moving"]:
            raise ToolError(json.dumps({"status": 409, "error": "busy",
                                        "detail": f"already moving to {brief['target_view']!r}; stop first"}))
        if brief["current_view"] == view:
            return {"started": False, "view": view, "detail": "the camera is already at this view"}
        try:
            # frame_object: with a marked object the view ends framed on it (the camera director)
            r = await control.call("POST", "/api/goto", {"view": view, "generation": st["generation"],
                                                         "frame_object": True})
        except ControlError as e:
            raise _tool_error(e)
        card = next((c for c in st.get("cards") or [] if c["name"] == view), {})
        first_run = not card.get("checked")
        return {"started": bool(r.get("started")), "view": view, "speed": r.get("speed"), "first_run_speed": first_run,
                "eta_hint": "30-60 s (first-run speed)" if first_run else "a few seconds",
                "hint": "call get_status later to see whether it arrived"}

    @mcp.tool(description="Where the camera is and how the current or last move is going: moving, target_view, "
                          "phase (rise/turn/descend), progress 0..1, current_view, last_result {ok, view, error, "
                          "duration}, and blockers (why no move can start right now).")
    async def get_status() -> dict[str, Any]:
        return status_from(await state())

    @mcp.tool(description="Read the live state of all eight motors and the camera. Positions, goals and limits "
                          "are encoder steps. Load is in tenths of a percent. Includes status bits, temperature, "
                          "voltage, current, calibrated ranges, allowed motion envelope, modelled camera and arm "
                          "geometry, telemetry freshness and movement blockers. This tool performs no movement.")
    async def get_arm_telemetry() -> dict[str, Any]:
        try:
            st, motors, pose = await asyncio.gather(
                state(), control.call("GET", "/api/telemetry"), control.call("GET", "/api/pose"))
        except ControlError as e:
            raise _tool_error(e)
        return {"status": status_from(st), "freshness": st.get("data"), "camera": st.get("camera_state"),
                "motors": motors, "geometry": pose}

    @mcp.tool(description="Stop the arm immediately and hold it where it is. Works at any time; use it first "
                          "whenever the user says stop, wait or halt.")
    async def stop() -> dict[str, Any]:
        try:
            r = await control.call("POST", "/api/stop", {})
        except ControlError as e:
            raise _tool_error(e)
        return {k: r.get(k) for k in ("ok", "hold", "was_moving", "view", "error") if k in r}

    def director_summary(st: dict) -> dict[str, Any]:
        """High-level only: no joint values, no motion paths."""
        job = st.get("job") or {}
        return {"mode": (st.get("gate") or {}).get("mode"), "target": (st.get("target") or {}).get("label"),
                "target_status": (st.get("target") or {}).get("status"),
                "gate": st.get("gate"), "camera": st.get("camera"),
                "observation": (st.get("target") or {}).get("observation"), "localization": st.get("localization"),
                "job": {k: job.get(k) for k in ("id", "kind", "status", "phase", "step", "reason")} if job else None,
                "views": [{"preset": c["preset"], "status": c["status"], "reason": c.get("reason"),
                           "score": (c.get("best") or {}).get("score"), "predicted": (c.get("predicted") or {}).get("score")}
                          for c in st.get("catalogue") or []]}

    @mcp.tool(description="Start a guarded camera job for the marked object. Prefer the existing job when it fits. "
                          "survey finds views (verify=false only predicts), recall rechecks a verified preset, "
                          "cinematic plays verified shots, tour combines survey and cinematic, watch handles occlusion, "
                          "show films a wide-to-close sequence, gimbal arcs with heavy joints parked, scene plans a free "
                          "camera move when approved, film interprets a brief, follow turns the camera to keep the object in frame until stop, wave waves the camera hello, gesture performs a described arm gesture (brief: dance, nod, bow), center is a dry-run preview, selfcal fits "
                          "the camera from a stationary target, snap returns to a nearby preset, and return retraces the "
                          "last trail when computed paths are approved. Poll director_status. Call stop to halt.")
    async def director_job(kind: Literal["survey", "cinematic", "tour", "watch", "recall", "film", "show", "gimbal",
                                        "scene", "center", "follow", "wave", "gesture", "selfcal", "snap", "return"],
                           preset: Optional[str] = None, brief: Optional[str] = None,
                           planner: Literal["local", "claude"] = "local", verify: bool = True,
                           yaw: int = Field(default=500, ge=25, le=500),
                           roll: float = Field(default=0, ge=-18, le=18),
                           speed: Optional[Literal["slow", "normal", "fast"]] = None) -> dict[str, Any]:
        body = {"kind": kind, "planner": planner, "verify": verify, "yaw": yaw, "roll": roll}
        if speed is not None:
            body["speed"] = speed           # this one motion only ("wave fast"); the next job is back to normal
        if preset is not None:
            body["preset"] = preset
        if brief is not None:
            body["brief"] = brief
        try:
            r = await control.call("POST", "/api/director/job", body)
        except ControlError as e:
            raise _tool_error(e)
        job = r.get("job") or {}
        return {"started": True, "job_id": job.get("id"), "kind": kind, "mode": job.get("mode")}

    @mcp.tool(description="Camera director status: mode (SIMULATED / DRY RUN / PHYSICAL), the marked object and "
                          "whether it is tracked, the running job's step, and each preset's outcome and score.")
    async def director_status() -> dict[str, Any]:
        try:
            return director_summary(await control.call("GET", "/api/director/state"))
        except ControlError as e:
            raise _tool_error(e)

    @mcp.tool(description="Start or stop one recording. Phone is the default camera for unqualified "
                          "requests. Robot recording requires an actively viewed robot feed in the laptop dashboard.")
    async def record_camera(camera: Literal["phone", "robot"], action: Literal["start", "stop"],
                            tab: str | None = None, session: str | None = None) -> dict[str, Any]:
        service = dashboard or Control("http://127.0.0.1:8300")
        try:
            result = await service.call("POST", "/laptop/recording", {"camera": camera, "action": action, "tab": tab, "session": session})
        except ControlError as e:
            raise _tool_error(e)
        return result

    @mcp.tool(structured_output=False,
              description="The current camera image as JPEG, plus the view it was taken at. Only while the arm "
                          "is not moving (refused otherwise).")
    async def get_camera_image() -> list[types.ImageContent | types.TextContent]:
        try:
            jpeg, headers = await control.call("GET", "/api/still.jpg", raw=True)
        except ControlError as e:
            raise _tool_error(e)
        nonlocal still_no
        still_no += 1
        try:
            joints = json.loads(headers.get("X-Joints") or "{}")
        except json.JSONDecodeError:
            joints = {}
        stills[still_no] = {"joints": joints, "image_size": headers.get("X-Image-Size") or "",
                            "view": headers.get("X-View") or None}
        for old in [k for k in stills if k <= still_no - MAX_STILLS]:
            del stills[old]
        meta = {"still": still_no, "view": headers.get("X-View") or None,
                "image_size": headers.get("X-Image-Size") or None,
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
        return await scene.update(objects, known, replace, stills)

    @mcp.tool(description="Remove one object from the scene, e.g. when it has been taken away.")
    async def remove_scene_object(id: str) -> dict[str, Any]:
        return await scene.remove(id)

    @mcp.tool(description="Pick the saved view that shows an object best, from the recorded sightings (large, "
                          "centred, visible, recent), and move there unless move=false. Returns the ranking.")
    async def best_view(object_id: str, move: bool = True) -> dict[str, Any]:
        obj = scene.load()["objects"].get(object_id)
        if obj is None:
            raise ToolError(f"no object {object_id!r} in the scene; look with get_camera_image and record it first")
        ranking = rank_views(obj, known)
        if not ranking:
            raise ToolError(f"{object_id!r} has no sighting from a saved view yet")
        best = ranking[0]["view"]
        out: dict[str, Any] = {"object": object_id, "best_view": best, "ranking": ranking}
        if move:
            out["move"] = await go_to_view(best)
        return out

    @mcp.tool(description="PREVIEW ONLY, nothing moves: where the camera would have to go to frame an object from "
                          "the image its newest sighting is from. framing 'fill' = as large as is safe, 'center' = "
                          "centred at the present distance. Says whether the target passes the checks (reach, joint "
                          "limits, height above the table) or why not. The sighting must carry 'still' from an image "
                          "taken where the arm is now.")
    async def preview_frame(object_id: str, framing: Literal["fill", "center"] = "fill",
                            margin: float = 0.1) -> dict[str, Any]:
        obj = scene.load()["objects"].get(object_id)
        if obj is None:
            raise ToolError(f"no object {object_id!r} in the scene")
        posed = [s for s in obj.get("seen", []) if s.get("pose")]
        if not posed:
            raise ToolError(f"no sighting of {object_id!r} with an image number; take an image, then record the "
                            "box with update_scene and 'still'")
        s = max(posed, key=lambda x: x.get("at", 0))
        try:
            r = await control.call("POST", "/api/frame", {
                "bbox": s["bbox"], "framing": framing, "margin": margin, "still_joints": s["pose"],
                "image_size": s.get("image_size") or "", "dry_run": True})
        except ControlError as e:
            raise _tool_error(e)
        keep = ("ok", "framing", "table_point_m", "object_size_m", "camera_distance_now_m", "camera_distance_m",
                "joint_travel", "min_height_m", "notes", "from_view", "dry_run")
        return {"object": object_id, **{k: r[k] for k in keep if k in r}}

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
            st = await control.call("GET", "/api/state")
            return [{"name": c["name"]} for c in st.get("cards") or []]
        except ControlError as e:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{e.detail}")
            await asyncio.sleep(1)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dashboard-url", default="http://127.0.0.1:8300")
    p.add_argument("--arm-url", default="http://127.0.0.1:8890",
                   help="capture_server.py base URL (default 8890; the --sim server usually runs on 8891)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    p.add_argument("--scene", type=Path, default=SCENE_PATH)
    p.add_argument("--wait", type=float, default=10, help="seconds to wait for the arm web server at start")
    return p.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args(argv)
    control = Control(args.arm_url)
    views = asyncio.run(fetch_views(control, args.wait))
    mcp = build_server(control, SceneStore(args.scene), views, Control(args.dashboard_url))
    log.info("camera-arm MCP on http://%s:%d/mcp  arm=%s  views=%s",
             args.host, args.port, args.arm_url, [v["name"] for v in views])
    mcp.run("streamable-http", host=args.host, port=args.port, streamable_http_path="/mcp",
            stateless_http=True, json_response=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        raise SystemExit(f"error: {e}")
