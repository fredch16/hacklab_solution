"""Camera director: target selection, per-preset best-view survey, recall and cinematic, inside
capture_server.py (started with --director). Local only: frames come from the capture server's own
Station camera reader, all vision is camera_director/vision.py, Claude is not involved.

Who moves the arm: only State.start_move (the capture server's single command owner), with the
stop generation from when the job started, so a STOP from anywhere refuses every later move of the
job; STOP and any person's command also cancel the job (State.command / State._stop_cleanup).
Every move is to a saved view or to a stop part-way along a checked line between two checked views
(motion.plan_along): no computed poses.

A director, not a search: the arm does not sweep candidate poses with the camera rolling. The target
is localized on the desk from settled sightings (measured servo positions + live calibration through
fk.py, camera_director/predict.py); every approved pose is scored by prediction without moving; then
each preset gets at most one deliberate move to its predicted best pose, verified by the camera. A
better view passed on the way in can be pulled back to (a short stop along the same checked line).
Keep in view changes shot only when the target stays covered, announces the move, dwells, and does
not bounce straight back.

Physical motion needs the operator's approval file director_safety.json (ignored by git). Without
it the real arm only gets a plan and a score of the current view (DRY RUN). In --sim the same jobs
run on the simulated arm with a rendered camera, labelled SIMULATED.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

import framing
import motion
from station_arm import logger

from . import predict
from . import vision as V

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "director_data"            # sticker reference, catalogue, thumbnails (ignored by git)
SAFETY_PATH = ROOT / "director_safety.json"  # the operator's approval for physical moves (ignored by git)
GRIPPER_CAMERA = "usbvideo/74e4d17bc903e40ebc78117539fca31f"   # docs/HARDWARE.md: the follower's gripper camera (Fred's Mac)
# Station names a camera differently per computer. On the lab Linux laptop the gripper camera (jaws visible at
# the bottom of the frame, checked by eye on 22 Nov) is this queue:
GRIPPER_CAMERAS = (GRIPPER_CAMERA, "usbvideo/dc84eab2202fb0ae9704788db7d9f9a4")

TICK_S = 0.2              # frame sampling / live tracking period
FRAME_CACHE = 60          # frames kept for selection by id (~12 s)
SELECT_MAX_AGE_S = 600.0  # a box drawn on a frozen frame older than this is refused (the frame is pinned meanwhile)
FRAME_FRESH_S = 1.0       # a frame older than this is not used for decisions
SETTLE_S = 0.6            # wait after a move before checking that the joints stand still
SETTLE_TOL = 4            # steps between two reads 0.15 s apart
OBSERVE_FRAMES = 5        # frames scored per view after settling
PULLBACK_MARGIN = 0.08   # a view seen on the way in must beat the arrival by this much to pull back to it
PULLBACK_MIN_S = 0.04     # ...and lie at least this far back along the line
VERIFY_MIN = 0.6          # only presets predicted at least this good get a verification move...
VERIFY_EDGE = 0.5         # ...with the target predicted clear of the picture edge (edge clearance >= 0.5, ~6%)
PREDICTED_DISCOUNT = 0.9  # a predicted (unvisited) shot counts for a bit less than a verified one
WATCH_LOOK_S = 0.5        # keep in view: one look this often
MIN_DWELL_S = 8.0         # ...stay at least this long after any move
NO_RETURN_S = 30.0        # ...do not go back to a view left because it was covered for this long
PULL_BACK_S = 15.0        # ...after this long at a fallback view, try the preferred view once
ANNOUNCE_S = 2.0          # ...every automatic move is announced this long before it starts
MIN_ANGLE_DEG = 20.0      # ...an alternative must look at the target from at least this different a direction
RECHECK_RATIO = 0.85      # a best view counts as verified when a fresh look scores at least this share
SHOT_HOLD_S = 3.0         # cinematic: how long each shot is held
MOVE_SPEED = "slow"
ACTIVE = ("RUNNING",)


class Hold(Exception):
    """The job stops here; the arm is left holding wherever the last move left it."""


class Director:
    def __init__(self, state, data_dir: Path = DATA_DIR, safety_path: Path = SAFETY_PATH):
        self.state, self.data, self.safety_path = state, Path(data_dir), Path(safety_path)
        if state.sim:   # simulated references and results must never reach the real arm's auto-find
            self.data = self.data / "sim"
        self.frames: deque = deque(maxlen=FRAME_CACHE)   # (frame_id, monotonic stamp, jpeg)
        self.seq, self._last_jpeg, self.sim_camera = 0, None, None
        self.pinned: dict[int, tuple] = {}             # frames opened for drawing a box: kept until selected
        self.target: dict | None = None
        self.ref: V.Reference | None = None
        self.tracker: V.Tracker | None = None
        self.obs: dict | None = None
        self.job: dict | None = None
        self.job_task: asyncio.Task | None = None
        self.job_gen = 0
        self._own_move = False
        self.catalogue: dict[str, dict] = {}
        self.sightings: list = []            # (measured joints, box) from settled views of this target
        self.loc: dict | None = None         # predict.localize() result
        self.calibration = self._load_calibration()
        self.samples: list = []              # frames scored during the last sampled move
        self.thumbs: dict[str, bytes] = {}
        self.events: deque = deque(maxlen=60)
        state.loop.call_soon_threadsafe(lambda: setattr(self, "_task", asyncio.ensure_future(self._run())))

    # ------------------------------------------------------------ frames and live tracking

    def note(self, text: str):
        self.events.append({"t": round(time.time(), 2), "text": text})
        logger.info("director: %s", text)

    async def _run(self):
        while True:
            try:
                f = await self._grab()
                if f and self.tracker is not None:
                    tracker = self.tracker
                    gray = await asyncio.to_thread(V.decode_gray, f[2])
                    hit = await asyncio.to_thread(tracker.update, gray, f[0], f[1])
                    if tracker is self.tracker:
                        self.obs = hit
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("director tick")
                self.note(f"vision error: {e}")
            await asyncio.sleep(TICK_S)

    async def _grab(self):
        st = self.state
        if st.sim:
            from .sim_camera import SimCamera
            ranges = st.arm.ranges()
            if self.sim_camera is None:
                self.sim_camera = SimCamera(motion.load_views()["top"], ranges)
            jpeg = await asyncio.to_thread(self.sim_camera.render, st.arm.joints(), ranges)
            stamp = time.monotonic()
        else:
            cam = st.cams.get(st.cam_queue) if st.cam_queue else None
            if cam is None or cam.jpeg is None or cam.jpeg is self._last_jpeg:
                return None
            jpeg, stamp = cam.jpeg, cam.stamp
        self._last_jpeg = jpeg
        self.seq += 1
        self.frames.append((self.seq, stamp, jpeg))
        return self.seq, stamp, jpeg

    def frame(self, frame_id: int | None = None):
        frames = list(self.frames)
        if frame_id is None:
            return frames[-1] if frames else None
        return self.pinned.get(frame_id) or next((f for f in frames if f[0] == frame_id), None)

    def camera_state(self) -> dict:
        f = self.frame()
        age = None if f is None else round(time.monotonic() - f[1], 2)
        size = None
        if f is not None:
            wh = framing.jpeg_size(f[2])
            size = list(wh) if wh else None
        queue = "simulated" if self.state.sim else self.state.cam_queue
        return {"queue": queue, "simulated": self.state.sim, "frame_id": f[0] if f else None, "age_s": age,
                "live": age is not None and age < FRAME_FRESH_S * 2, "size": size,
                "gripper_camera": self.state.sim or any((queue or "").endswith(q) for q in GRIPPER_CAMERAS)}

    # ------------------------------------------------------------ safety gate

    def gate(self) -> dict:
        if self.state.sim:
            return {"mode": "SIMULATED", "moves": True, "reasons": [], "lines": None, "max_fraction": motion.ALONG_MAX}
        reasons, approval = [], {}
        try:
            approval = json.loads(self.safety_path.read_text())
            if not isinstance(approval, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            reasons.append("the operator has not approved physical director moves (no director_safety.json)")
        except (OSError, ValueError) as e:
            reasons.append(f"director_safety.json is unreadable: {e}")
        if approval:
            if approval.get("physical_motion") is not True:
                reasons.append("director_safety.json does not set physical_motion: true")
            for key in ("approved_by", "approved_at"):
                if not approval.get(key):
                    reasons.append(f"director_safety.json has no {key}")
            if approval.get("camera_queue") != self.state.cam_queue:
                reasons.append(f"the approved camera {approval.get('camera_queue')} is not the selected camera {self.state.cam_queue}")
            if self.state.arm is None or approval.get("bus") != self.state.arm.bus_serial:
                reasons.append("the approved follower bus is not the connected arm")
        lines = approval.get("lines") if isinstance(approval.get("lines"), list) else []
        frac = min(float(approval.get("max_fraction") or 0.0), motion.ALONG_MAX) if approval else 0.0
        return {"mode": "DRY RUN" if reasons else "PHYSICAL", "moves": not reasons, "reasons": reasons,
                "lines": sorted(lines), "max_fraction": frac}

    # ------------------------------------------------------------ per-preset camera correction

    def _calibration_path(self) -> Path:
        return self.data / "camera_calibration.json"

    def _load_calibration(self) -> dict:
        try:
            return json.loads(self._calibration_path().read_text())
        except (OSError, ValueError):
            return {"bias": {}}

    async def calibrate(self, req: dict) -> tuple[int, dict]:
        """The object is lying at a tape-measured desk point [x, y] m (x right, y ahead of the base, seen from
        behind the arm): record this preset's camera correction from a settled look."""
        if self.busy():
            return 409, {"ok": False, "error": "a director job is running; press STOP first"}
        pt = req.get("point_m")
        if not (isinstance(pt, list) and len(pt) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pt)
                and -0.8 < pt[0] < 0.8 and 0.0 < pt[1] < 0.9):
            return 400, {"ok": False, "error": "point_m must be [x, y] in metres on the desk (x right, y ahead of the base)"}
        if self.ref is None:
            return 409, {"ok": False, "error": "no target: mark the object first"}
        w = self.where()
        if w.get("where") != "view":
            return 409, {"ok": False, "error": "move the arm to a saved view first (calibration is per preset)"}
        self.job = {"id": uuid.uuid4().hex[:8], "kind": "calibrate", "mode": self.gate()["mode"], "status": "RUNNING",
                    "phase": None, "step": "looking", "progress": {}, "reason": None, "result": None, "moving_to": None,
                    "started_at": round(time.time(), 2), "ended_at": None}
        try:
            joints, t = await self.settle()
            obs = await self.observe(f"{w['view']}/calibration", joints, t)
        except Hold as e:
            self.job.update(status="HOLD", reason=str(e), ended_at=round(time.time(), 2))
            return 409, {"ok": False, "error": str(e)}
        if not obs["valid"]:
            self.job.update(status="HOLD", reason=obs["reason"], ended_at=round(time.time(), 2))
            return 409, {"ok": False, "error": f"the object is not visible here: {obs['reason']}"}
        point = [float(pt[0]), float(pt[1]), framing.TABLE_Z]
        try:
            bias = predict.measured_bias(joints, obs["box"], point, self.state.arm.ranges(), self._aspect())
        except ValueError as e:
            return 409, {"ok": False, "error": str(e)}
        self.calibration.setdefault("bias", {})[w["view"]] = list(bias)
        self.calibration.setdefault("records", {})[w["view"]] = {
            "point_m": point, "box": obs["box"], "measured_joints": {str(m): v for m, v in joints.items()},
            "at": round(time.time(), 1)}
        self.data.mkdir(parents=True, exist_ok=True)
        self._calibration_path().write_text(json.dumps(self.calibration, indent=1))
        self.sightings = []          # earlier sightings were cast without this correction
        self.loc = None
        self.job.update(status="DONE", step="done", result={"preset": w["view"], "bias": bias}, ended_at=round(time.time(), 2))
        self.note(f"camera correction for {w['view']}: {bias} (image fractions) from the point {point[:2]} m")
        return 200, {"ok": True, "preset": w["view"], "bias": bias,
                     "bias_px": [round(bias[0] * 1280), round(bias[1] * 720)]}

    # ------------------------------------------------------------ target

    def _set_target(self, ref: V.Reference, frame_id: int, how: str):
        self.ref, self.tracker, self.obs = ref, V.Tracker(ref), None
        self.target = {"id": uuid.uuid4().hex[:8], "label": ref.label, "kind": ref.kind, "frame_id": frame_id,
                       "how": how, "set_at": round(time.time(), 2)}
        self.catalogue.clear()   # results for another target say nothing about this one
        self.thumbs.clear()
        self.sightings, self.loc = [], None
        self.note(f"target {ref.label} ({how})")

    async def select(self, req: dict) -> tuple[int, dict]:
        if self.busy():
            return 409, {"ok": False, "error": "a director job is running; press STOP first"}
        box = req.get("box")
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
            return 400, {"ok": False, "error": "box must be [x1, y1, x2, y2] in 0..1"}
        x1, y1, x2, y2 = (float(v) for v in box)
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            return 400, {"ok": False, "error": "box must be [x1, y1, x2, y2] with 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1"}
        fid = req.get("frame_id")
        f = self.frame(fid) if isinstance(fid, int) and not isinstance(fid, bool) else None
        if f is None or time.monotonic() - f[1] > SELECT_MAX_AGE_S:
            return 409, {"ok": False, "error": "that picture is too old; take a new one and draw the box again"}
        gray = await asyncio.to_thread(V.decode_gray, f[2])
        h, w = gray.shape
        if (x2 - x1) * w < 12 or (y2 - y1) * h < 12:
            return 400, {"ok": False, "error": "the box is too small (at least 12 pixels a side)"}
        as_sticker = req.get("as_sticker") is True
        label = "Hack Lab sticker" if as_sticker else (str(req.get("label") or "").strip()[:40] or "selected object")
        try:
            ref = V.Reference.from_frame(gray, [x1, y1, x2, y2], label, "sticker" if as_sticker else "manual")
        except ValueError as e:
            return 400, {"ok": False, "error": str(e)}
        if len(ref.pts) < V.MIN_INLIERS and float(ref.patch.std()) < 8:
            return 422, {"ok": False, "error": "nothing recognisable in that box; draw it around something with more detail"}
        if as_sticker:
            await asyncio.to_thread(ref.save, self.data / "sticker")
            self.note("saved the Hack Lab sticker reference from a confirmed crop")
        self._set_target(ref, f[0], "saved sticker crop" if as_sticker else "drawn box")
        return 200, {"ok": True, "target": self.target, "features": len(ref.pts)}

    async def fresh_frames(self, n: int, after: float | None = None, timeout_s: float | None = None) -> list:
        """n distinct frames newer than `after` (monotonic) and the last seen frame id; Hold if the camera stalls."""
        out, last = [], self.seq
        deadline = time.monotonic() + (timeout_s or n * 1.0 + 3.0)
        while len(out) < n:
            if time.monotonic() > deadline:
                raise Hold("the camera stopped delivering fresh frames")
            f = self.frame()
            if f and f[0] > last and (after is None or f[1] >= after) and time.monotonic() - f[1] < FRAME_FRESH_S:
                out.append(f)
                last = f[0]
            await asyncio.sleep(0.05)
        return out

    async def autofind(self) -> tuple[int, dict]:
        if self.busy():
            return 409, {"ok": False, "error": "a director job is running; press STOP first"}
        ref = await asyncio.to_thread(V.Reference.load, self.data / "sticker")
        if ref is None:
            return 404, {"ok": False, "error": "no_reference",
                         "detail": "No saved sticker reference yet. Click Select object, draw a box around the real "
                                   "sticker, tick Save as Hack Lab sticker and confirm. That is needed once."}
        try:
            frames = await self.fresh_frames(3, timeout_s=4.0)
        except Hold as e:
            return 503, {"ok": False, "error": "no_camera", "detail": str(e)}
        hits = []
        for f in frames:
            gray = await asyncio.to_thread(V.decode_gray, f[2])
            hits.append(await asyncio.to_thread(V.detect, ref, gray))
        found = [h for h in hits if h]
        consistent = len(found) >= 2 and V._iou(found[0]["box"], found[-1]["box"]) > 0.5
        if not consistent:
            return 404, {"ok": False, "error": "not_found",
                         "detail": f"The sticker was recognised in {len(found)} of 3 fresh frames; not enough to be sure. "
                                   "Go to the top view, or draw a box around it with Select object."}
        self._set_target(ref, frames[-1][0], "auto-found sticker")
        self.obs = {**found[-1], "frame_id": frames[-1][0], "stamp": frames[-1][1], "stable": True}
        return 200, {"ok": True, "target": self.target, "observation": found[-1]}

    def clear(self) -> tuple[int, dict]:
        if self.busy():
            return 409, {"ok": False, "error": "a director job is running; press STOP first"}
        self.target = self.ref = self.tracker = self.obs = None
        return 200, {"ok": True}

    # ------------------------------------------------------------ observing a settled view

    async def settle(self) -> tuple[dict, float]:
        await asyncio.sleep(SETTLE_S)
        try:
            prev = self.state.arm.joints(max_age_s=motion.STALE_S)
            for _ in range(20):
                await asyncio.sleep(0.15)
                now = self.state.arm.joints(max_age_s=motion.STALE_S)
                if max(abs(now[m] - prev[m]) for m in motion.MOVABLE) <= SETTLE_TOL:
                    return now, time.monotonic()
                prev = now
        except RuntimeError as e:
            raise Hold(f"no fresh joint data: {e}")
        raise Hold("the arm did not stand still after the move")

    def _thumb(self, jpeg: bytes, box: list | None, text: str) -> bytes:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        if box:
            cv2.rectangle(img, (int(box[0] * w), int(box[1] * h)), (int(box[2] * w), int(box[3] * h)), (60, 220, 60), max(2, w // 240))
        img = cv2.resize(img, (320, int(320 * h / w)), interpolation=cv2.INTER_AREA)
        cv2.putText(img, text, (6, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
        return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()

    async def observe(self, key: str, joints: dict, after: float) -> dict:
        """Re-acquire the target from its reference in fresh settled frames and score the view."""
        ref = self.ref
        if ref is None:
            raise Hold("the target was cleared")
        frames = await self.fresh_frames(OBSERVE_FRAMES, after=after)
        grays, hits = [], []
        for f in frames:
            g = await asyncio.to_thread(V.decode_gray, f[2])
            grays.append(g)
            hits.append(await asyncio.to_thread(V.detect, ref, g))
        sc = await asyncio.to_thread(V.score_view, hits, grays)
        pick = max(range(len(frames)), key=lambda i: hits[i]["confidence"] if hits[i] else -1)
        label = f"{key}  {sc['score']:.2f}" if sc["valid"] else f"{key}  not seen"
        self.thumbs[key] = self._thumb(frames[pick][2], hits[pick]["box"] if hits[pick] else None, label)
        return {**sc, "thumb": key, "measured_joints": {str(m): v for m, v in joints.items()},
                "frame_ids": [f[0] for f in frames], "observed_at": round(time.time(), 2)}

    # ------------------------------------------------------------ moving

    async def move(self, view: str, along: tuple[str, float] | None = None, sample: bool = False) -> bool:
        """One deliberate move through the capture server's command owner. True when it arrived, False when
        the motion layer refused before commanding anything; Hold when a move failed part-way. With `sample`,
        frames seen on the way are scored (self.samples) so a better view passed en route can be pulled back to."""
        self.job["moving_to"] = view if not along else f"{view} → {along[0]} {along[1]:.0%}"
        self._own_move, self.samples = True, []
        try:
            try:
                await self.state.start_move(view, MOVE_SPEED, gen=self.job_gen, along=along)
            except asyncio.CancelledError:
                raise
            except Exception as e:     # refused by the command owner (STOP since the job began, busy, no data)
                raise Hold(f"move refused: {e}")
            task = self.state.move_task
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)     # never cancels the move: only STOP does
                if sample and not task.done():
                    await self._sample()
            out = self.state.move_outcome or {}
        finally:
            self._own_move = False
            self.job["moving_to"] = None
        if out.get("ok"):
            return True
        if out.get("hold") is None:          # refused before any goal was written: the arm did not move
            self.note(f"move to {view} {along or ''} refused: {out.get('error')}")
            return False
        raise Hold(f"the move to {view} did not finish: {out.get('error')}")

    async def _sample(self):
        f = self.frame()
        try:
            joints = self.state.arm.joints(max_age_s=motion.STALE_S)
        except RuntimeError:
            return
        if f is None or time.monotonic() - f[1] > 0.3 or self.ref is None:
            return
        gray = await asyncio.to_thread(V.decode_gray, f[2])
        hit = await asyncio.to_thread(V.detect, self.ref, gray)
        if hit:
            self.samples.append({"joints": joints, "score": V.frame_score(hit, gray), "box": hit["box"]})

    def pullback(self, dest: str, start: str | None, gate: dict, than: float | None) -> dict | None:
        """A stop on the line start -> dest, near dest, where the camera saw the target better on the way in
        (e.g. a hand covers it at dest). Only on a checked line within the allowed fraction of dest."""
        if not self.samples or not start or start == dest or start not in motion.view_names():
            return None
        kind, _ = motion.route_kind(dest, start)
        line = "-".join(sorted((dest, start)))
        if kind not in ("direct", "arc") or (gate["lines"] is not None and line not in gate["lines"]):
            return None
        views = motion.load_views()
        best = max(self.samples, key=lambda x: x["score"])
        s = round(1.0 - predict.line_fraction(best["joints"], views[start], views[dest]), 3)
        if not PULLBACK_MIN_S <= s <= min(gate["max_fraction"], motion.ALONG_MAX):
            return None
        if than is not None and best["score"] < than + PULLBACK_MARGIN:
            return None
        return {"toward": start, "s": s, "route": kind, "seen_score": best["score"]}

    def where(self) -> dict:
        w = (self.state.where or {}).get("where") or {}
        return w

    async def go_shot(self, p: str, toward: str | None, s: float, key: str, sample: bool = False) -> dict:
        """Move to a shot (a preset, or a stop along one of its checked lines) and look. Moves only when
        the arm is not already there; a stop on the same line is reached directly along it."""
        w = self.where()
        on_line = toward and w.get("where") == "between" and {w.get("view"), w.get("to")} == {p, toward}
        at_p = w.get("where") == "view" and w.get("view") == p
        if not (on_line or at_p):
            if not await self.move(p, sample=sample):
                raise Hold(f"the route to {p} was refused")
        if toward and not (on_line and abs(self._line_s(p, toward) - s) < 0.02):
            if not await self.move(p, (toward, s)):
                raise Hold(f"the stop {s:.0%} toward {toward} was refused")
        joints, t = await self.settle()
        return await self.observe(key, joints, t)

    def _line_s(self, p: str, toward: str) -> float:
        w = self.where()
        if w.get("where") != "between":
            return 0.0
        return w["s"] if w.get("view") == p else 1.0 - w["s"]

    # ------------------------------------------------------------ jobs

    def busy(self) -> bool:
        return self.job_task is not None and not self.job_task.done()

    def preempt(self, reason: str):
        if self.busy():
            self.job["reason"] = reason
            self.job_task.cancel()

    def preempt_unless_own(self, reason: str):
        """A person's command while a job runs: the job ends, and a move it started is stopped."""
        if self.busy() and asyncio.current_task() is not self.job_task:
            if self._own_move and self.state.moving:
                asyncio.ensure_future(self.state.stop())
            self.preempt(reason)

    def start(self, req: dict) -> tuple[int, dict]:
        kind = req.get("kind")
        runners = {"survey": self._survey, "recall": self._recall, "cinematic": self._cinematic,
                   "tour": self._tour, "center": self._center, "watch": self._watch}
        if kind not in runners:
            return 400, {"ok": False, "error": f"kind must be one of {sorted(runners)}"}
        if self.busy():
            return 409, {"ok": False, "error": f"a {self.job['kind']} job is already running"}
        if self.state.stopping:
            return 409, {"ok": False, "error": "STOP is still being handled"}
        if kind != "tour" and self.ref is None:
            return 409, {"ok": False, "error": "no target: select an object (or auto-find the saved sticker) first"}
        gate = self.gate()
        self.job_gen = self.state.gen
        self.job = {"id": uuid.uuid4().hex[:8], "kind": kind, "mode": gate["mode"], "status": "RUNNING", "phase": None,
                    "step": "starting", "progress": {}, "reason": None, "result": None, "moving_to": None,
                    "started_at": round(time.time(), 2), "ended_at": None}
        self.job_task = asyncio.ensure_future(self._wrap(runners[kind], req, gate))
        return 200, {"ok": True, "job": self.job}

    async def _wrap(self, runner, req: dict, gate: dict):
        try:
            await runner(req, gate)
            if self.job["status"] == "RUNNING":
                self.job["status"] = "DONE"
        except asyncio.CancelledError:
            self.job["status"] = "STOPPED"
            self.job["reason"] = self.job["reason"] or "stopped"
        except Hold as e:
            self.job["status"], self.job["reason"] = "HOLD", str(e)
        except Exception as e:
            logger.exception("director job")
            self.job["status"], self.job["reason"] = "HOLD", f"internal error: {type(e).__name__}: {e}"
        finally:
            self.job["phase"] = None
            self.job["ended_at"] = round(time.time(), 2)
            self.job["step"] = self.job["status"].lower()
            self.note(f"{self.job['kind']} job {self.job['status']}" + (f": {self.job['reason']}" if self.job["reason"] else ""))
            self._save()

    def step(self, text: str, **progress):
        self.job["step"] = text
        self.job["progress"].update(progress)

    # ---- localize and predict (no motion)

    def _aspect(self) -> float:
        f = self.frame()
        wh = framing.jpeg_size(f[2]) if f else None
        return wh[0] / wh[1] if wh else 4 / 3

    async def localize(self, look: bool = True) -> dict:
        """Add a settled sighting from the current pose (if `look`) and localize the target from all of them."""
        if look:
            joints, t = await self.settle()
            obs = await self.observe("locate", joints, t)
            if not obs["valid"]:
                raise Hold(f"the target is not visible from the current pose ({obs['reason']}); point the camera at it first")
            self._add_sighting(joints, obs["box"])
        self.loc = await asyncio.to_thread(predict.localize, self.sightings, self.state.arm.ranges(), self._aspect())
        if not self.loc["ok"]:
            raise Hold(f"cannot localize the target: {self.loc['reason']}")
        self.note(f"target at {self.loc['point_m']} m, {self.loc['size_m'] * 100:.1f} cm wide ({self.loc['method']}, "
                  f"±{self.loc['uncertainty_m'] * 100:.1f} cm)" + (f"; {self.loc['warning']}" if self.loc.get('warning') else ""))
        return self.loc

    def _add_sighting(self, joints: dict, box: list):
        far = all(max(abs(joints[m] - s[0][m]) for m in motion.MOVABLE) > 40 for s in self.sightings)
        if far:
            bias = predict.bias_for(self.where(), self.calibration)
            self.sightings = (self.sightings + [(dict(joints), list(box), bias)])[-8:]

    def predict_all(self, gate: dict) -> dict[str, dict]:
        """Predicted best shot per checked preset from the current localization."""
        ranges = self.state.arm.ranges()
        grip = self.state.arm.joints()[motion.GRIPPER]
        aspect, out = self._aspect(), {}
        # A dry run plans over every checked line; only physical moves are limited to the approved ones.
        plan_gate = gate if gate["moves"] else {"lines": None, "max_fraction": motion.ALONG_MAX}
        for p, cands in predict.candidate_poses(ranges, grip, plan_gate).items():
            preds = []
            f = self.frame()
            wh = framing.jpeg_size(f[2]) if f else None
            for c in cands:
                bias = predict.bias_for({"preset": p, "toward": c["toward"], "s": c["s"]}, self.calibration)
                pv = predict.predict_view(c["joints"], self.loc, ranges, aspect, image_w=wh[0] if wh else predict.IMAGE_W,
                                          bias=bias)
                preds.append({k: v for k, v in c.items() if k != "joints"} | pv)
            vis = [x for x in preds if x["visible"]]
            out[p] = {"best": max(vis, key=lambda x: x["score"]) if vis else None, "count": len(preds),
                      "visible": len(vis), "why_not": None if vis else preds[0].get("reason")}
        return out

    # ---- survey: estimate, then one deliberate verification move per preset

    async def _survey(self, req: dict, gate: dict):
        presets = req.get("presets") or motion.view_names()
        known = motion.view_names()
        bad = [p for p in presets if p not in known]
        if bad or not presets:
            raise Hold(f"unknown presets {bad}; known: {known}")
        self.job["phase"] = "estimate"
        self.step("localizing the target from the measured arm pose")
        await self.localize()
        self._predict_into_catalogue(presets, gate)
        self.job["result"] = {"localization": self.loc}
        if not gate["moves"] or req.get("verify") is False:
            self.job["result"]["verified"] = 0
            return
        self.job["phase"] = "verify"
        todo = sorted((p for p in presets if self.catalogue[p]["status"] == "PREDICTED"
                       and self.catalogue[p]["predicted"]["score"] >= VERIFY_MIN
                       and self.catalogue[p]["predicted"]["components"]["edge_clearance"] >= VERIFY_EDGE),
                      key=lambda p: -self.catalogue[p]["predicted"]["score"])
        for p in presets:
            if self.catalogue[p]["status"] == "PREDICTED" and p not in todo:
                pr = self.catalogue[p]["predicted"]
                self.catalogue[p]["reason"] = (f"predicted {pr['score']:.2f}" + (", cut by the picture edge"
                                               if pr["components"]["edge_clearance"] < VERIFY_EDGE else "")
                                               + ": not worth a move")
        for i, p in enumerate(todo, 1):
            self.step(f"verifying {p}", preset=p, preset_index=i, preset_count=len(todo))
            await self._verify(p, gate)
            self._predict_into_catalogue([q for q in todo[i:]], gate)    # refined by the new sighting
        valid = [r for r in self.catalogue.values() if r["status"] == "VALID"]
        best = max(valid, key=lambda r: r["best"]["score"])["preset"] if valid else None
        self.job["result"].update(verified=len(valid), presets=len(presets), best=best, localization=self.loc)
        if best:
            b = self.catalogue[best]["best"]
            self.step(f"holding the best view ({best})")
            await self.go_shot(best, b.get("toward"), b.get("s", 0.0), f"{best}/best")

    def _predict_into_catalogue(self, presets: list[str], gate: dict):
        preds = self.predict_all(gate)
        for p in presets:
            base = {"preset": p, "target_id": self.target["id"], "target": self.target["label"], "mode": gate["mode"],
                    "camera": self.camera_state()["queue"], "baseline": None, "best": None, "candidates": [],
                    "predicted": None, "reason": None, "surveyed_at": round(time.time(), 2)}
            if p not in preds:
                self.catalogue[p] = {**base, "status": "BLOCKED", "reason": "not checked on the real arm, so it has no approved route"}
                continue
            pr = preds[p]
            if pr["best"] is None:
                self.catalogue[p] = {**base, "status": "SKIPPED", "reason": f"predicted not visible: {pr['why_not']}"}
                continue
            self.catalogue[p] = {**base, "status": "PREDICTED", "predicted": pr["best"],
                                 "reason": f"best of {pr['visible']} visible of {pr['count']} approved poses, by the model; not visited"}

    async def _verify(self, p: str, gate: dict):
        r = self.catalogue[p]
        pred = r["predicted"]
        w = self.where()
        start = w.get("view") if w.get("where") == "view" else None
        obs = await self.go_shot(p, pred.get("toward"), pred["s"], f"{p}/best", sample=True)
        obs.update(toward=pred.get("toward"), s=pred["s"], label="predicted best", predicted_score=pred["score"])
        pb = self.pullback(p, start, gate, obs["score"] if obs["valid"] else None) if not pred.get("toward") else None
        if pb:
            self.step(f"{p}: the view was better on the way in; pulling back {pb['s']:.0%} toward {pb['toward']}")
            back = await self.go_shot(p, pb["toward"], pb["s"], f"{p}/pullback")
            back.update(toward=pb["toward"], s=pb["s"], label="seen on the way in", predicted_score=None)
            r["candidates"].append(back)
            if back["valid"] and (not obs["valid"] or back["score"] >= obs["score"]):
                obs = back
            elif obs["valid"]:
                obs = await self.go_shot(p, None, 0.0, f"{p}/best")
                obs.update(toward=None, s=0.0, label="predicted best", predicted_score=pred["score"])
        if obs["valid"]:
            self._add_sighting({int(m): v for m, v in obs["measured_joints"].items()}, obs["box"])
            await self.localize(look=False)
            r.update(status="VALID", best=obs, reason=f"verified by the camera (model predicted {pred['score']:.2f})")
        else:
            r.update(status="NOT_CONFIRMED", best=None, baseline=obs,
                     reason=f"the model predicted it visible, the camera did not see it ({obs['reason']})")

    def _shots(self, req: dict) -> list[dict]:
        order = req.get("order") or motion.view_names()
        return [self.catalogue[p] for p in order if self.catalogue.get(p, {}).get("status") == "VALID"]

    async def _recall(self, req: dict, gate: dict):
        r = self.catalogue.get(str(req.get("preset")))
        if not r or r["status"] != "VALID":
            raise Hold("no verified best view for that preset; run Optimize all views first")
        if r["target_id"] != self.target["id"]:
            raise Hold("that view was found for another target; survey again")
        if not gate["moves"]:
            self.job.update(status="BLOCKED", reason="physical moves not approved", result={"preset": r["preset"]})
            return
        self.step(f"recalling {r['preset']}", preset=r["preset"])
        b = r["best"]
        look = await self.go_shot(r["preset"], b.get("toward"), b.get("s", 0.0), f"{r['preset']}/recall")
        ok = look["valid"] and look["score"] >= RECHECK_RATIO * b["score"]
        self.job["result"] = {"preset": r["preset"], "saved_score": b["score"], "score_now": look["score"],
                              "verified": ok, "measured_joints": look["measured_joints"]}
        if not ok:
            raise Hold(f"recalled view does not match: score now {look['score']} against {b['score']}")

    async def _cinematic(self, req: dict, gate: dict):
        shots = self._shots(req)
        board = [{"preset": s["preset"], "toward": s["best"].get("toward"), "s": s["best"].get("s"),
                  "score": s["best"]["score"], "thumb": s["best"]["thumb"]} for s in shots]
        self.job["result"] = {**(self.job["result"] or {}), "storyboard": board, "shots": []}
        if not shots:
            raise Hold("no verified best shots yet: run Optimize all views first")
        if not gate["moves"]:
            self.job.update(status="BLOCKED", reason="physical moves not approved: storyboard only (no moving take)")
            return
        self.job["phase"] = "cinematic"
        for i, s in enumerate(shots, 1):
            self.step(f"shot {i} of {len(shots)}: {s['preset']}", shot_index=i, shot_count=len(shots), preset=s["preset"])
            b = s["best"]
            look = await self.go_shot(s["preset"], b.get("toward"), b.get("s", 0.0), f"{s['preset']}/shot")
            if not look["valid"]:
                raise Hold(f"target lost at shot {i} ({s['preset']}): {look['reason']}")
            self.step(f"shot {i} of {len(shots)}: holding {s['preset']}")
            misses, end = 0, time.monotonic() + SHOT_HOLD_S
            while time.monotonic() < end:
                await asyncio.sleep(0.5)
                f = (await self.fresh_frames(1))[0]
                hit = await asyncio.to_thread(V.detect, self.ref, await asyncio.to_thread(V.decode_gray, f[2]))
                misses = 0 if hit else misses + 1
                if misses >= 2:
                    raise Hold(f"target lost while holding shot {i} ({s['preset']})")
            self.job["result"]["shots"].append({"preset": s["preset"], "score": look["score"]})

    async def _tour(self, req: dict, gate: dict):
        if self.ref is None:
            self.step("auto-finding the saved sticker (no object selected)")
            status, body = await self.autofind()
            if status != 200:
                raise Hold(body.get("detail") or body.get("error"))
        await self._survey(req, gate)
        survey = self.job["result"]
        if gate["moves"] and survey.get("verified"):
            await self._cinematic(req, gate)
        elif not gate["moves"]:
            self.job.update(status="DRY_RUN", reason="physical moves not approved: localized and predicted only")

    # ---- keep in view: hold the shot, change view only when the target stays covered

    async def _look(self) -> dict | None:
        f = (await self.fresh_frames(1))[0]
        return await asyncio.to_thread(V.detect, self.ref, await asyncio.to_thread(V.decode_gray, f[2]))

    def _shot_of(self, p: str) -> dict | None:
        r = self.catalogue.get(p) or {}
        b = r.get("best") if r.get("status") == "VALID" else r.get("predicted") if r.get("status") == "PREDICTED" else None
        if not b:
            return None
        score = b["score"] if r["status"] == "VALID" else b["score"] * PREDICTED_DISCOUNT
        return {"preset": p, "toward": b.get("toward"), "s": b.get("s", 0.0), "score": score, "status": r["status"]}

    def _direction(self, shot: dict) -> np.ndarray | None:
        cands = predict.candidate_poses(self.state.arm.ranges(), self.state.arm.joints()[motion.GRIPPER], {"lines": None})
        for c in cands.get(shot["preset"], []):
            if c["toward"] == shot["toward"] and abs(c["s"] - shot["s"]) < 1e-6 or (not shot["toward"] and not c["toward"]):
                pv = predict.predict_view(c["joints"], self.loc, self.state.arm.ranges(), self._aspect())
                return np.array(pv["direction"]) if pv.get("visible") else None
        return None

    async def _watch(self, req: dict, gate: dict):
        if self.loc is None:
            self.job["phase"] = "estimate"
            self.step("localizing the target")
            await self.localize()
            self._predict_into_catalogue(motion.view_names(), gate)
        shots = [s for s in (self._shot_of(p) for p in motion.view_names()) if s]
        if not shots:
            raise Hold("no view is known or predicted to see the target")
        preferred = max(shots, key=lambda s: s["score"])
        current = None
        w = self.where()
        here = next((s for s in shots if w.get("where") == "view" and w.get("view") == s["preset"] and not s["toward"]), None)
        if here is None and gate["moves"]:
            self.step(f"going to the preferred view ({preferred['preset']})")
            await self.go_shot(preferred["preset"], preferred["toward"], preferred["s"], f"{preferred['preset']}/watch")
            current = preferred
        else:
            current = here or preferred
        self.job["phase"] = "watch"
        joints, t = await self.settle()
        clear = await self.observe("watch/settled", joints, t)
        base = clear.get("inliers") or None
        looks: deque = deque(maxlen=4)
        arrived, left = time.monotonic(), {}
        self.job["result"] = {"preferred": preferred["preset"], "moves": []}
        while True:
            await asyncio.sleep(WATCH_LOOK_S)
            hit = await self._look()
            ratio = None if hit is None or not base or hit.get("inliers") is None else round(hit["inliers"] / base, 2)
            covered = hit is None or (ratio is not None and ratio < V.COVERED_RATIO)
            looks.append(covered)
            self.job["progress"].update(preset=current["preset"], visible_share=ratio if hit else 0.0)
            now = time.monotonic()
            label = current["preset"] + (f" ({current['s']:.0%} toward {current['toward']})" if current["toward"] else "")
            if len(looks) == 4 and sum(looks) >= 3 and now - arrived >= MIN_DWELL_S:
                alt = self._alternative(current, shots, left, now)
                if alt is None:
                    self.step(f"covered at {label}: no other approved view is expected to see it; holding")
                    continue
                if not gate["moves"]:
                    self.step(f"covered at {label}: would cut to {alt['preset']} (physical moves not approved)")
                    continue
                self.step(f"covered at {label}: cutting to {alt['preset']} in {ANNOUNCE_S:.0f} s (STOP cancels)")
                await asyncio.sleep(ANNOUNCE_S)
                left[current["preset"]] = now
                obs = await self.go_shot(alt["preset"], alt["toward"], alt["s"], f"{alt['preset']}/watch", sample=True)
                self.job["result"]["moves"].append({"from": current["preset"], "to": alt["preset"], "why": "covered",
                                                    "seen": obs["valid"], "t": round(time.time(), 1)})
                current, arrived, base = alt, time.monotonic(), (obs.get("inliers") or base)
                looks.clear()
                if not obs["valid"]:      # covered here too: the next look round picks another view, not the one left
                    left[alt["preset"]] = time.monotonic()
                    arrived -= MIN_DWELL_S
                    looks.extend([True] * 4)
                continue
            if (current is not preferred and not covered and now - arrived >= PULL_BACK_S
                    and now - left.get(preferred["preset"], -1e9) >= NO_RETURN_S and gate["moves"]):
                self.step(f"trying to pull back to the preferred view ({preferred['preset']}) in {ANNOUNCE_S:.0f} s")
                await asyncio.sleep(ANNOUNCE_S)
                prev = current
                obs = await self.go_shot(preferred["preset"], preferred["toward"], preferred["s"], f"{preferred['preset']}/watch")
                self.job["result"]["moves"].append({"from": prev["preset"], "to": preferred["preset"], "why": "pull back",
                                                    "seen": obs["valid"], "t": round(time.time(), 1)})
                if obs["valid"]:
                    current, base = preferred, obs.get("inliers") or base
                else:      # still covered there: back to the view that worked, and wait longer before trying again
                    left[preferred["preset"]] = time.monotonic()
                    obs = await self.go_shot(prev["preset"], prev["toward"], prev["s"], f"{prev['preset']}/watch")
                    current, base = prev, obs.get("inliers") or base
                arrived = time.monotonic()
                looks.clear()
                continue
            self.step(f"holding {label}" + (" (partly covered)" if covered else ""))

    def _alternative(self, current: dict, shots: list[dict], left: dict, now: float) -> dict | None:
        """The best other shot that looks at the target from a clearly different direction."""
        here = self._direction(current)
        best = None
        for s in shots:
            if s["preset"] == current["preset"] or now - left.get(s["preset"], -1e9) < NO_RETURN_S:
                continue
            d = self._direction(s)
            if d is None or (here is not None and math.degrees(math.acos(float(np.clip(d @ here, -1, 1)))) < MIN_ANGLE_DEG):
                continue
            if best is None or s["score"] > best["score"]:
                best = s
        return best

    async def _center(self, req: dict, gate: dict):
        """Keep in frame: what a centring correction would be (framing.plan 'center'). Preview only:
        moves to computed poses are not enabled in this system."""
        f = self.frame()
        obs = self.obs
        if obs is None or f is None or f[0] - obs["frame_id"] > 10:
            raise Hold("the target is not being tracked right now")
        joints, ranges = self.state.arm.joints(max_age_s=motion.STALE_S), self.state.arm.ranges()
        b = obs["box"]
        wh = framing.jpeg_size(f[2]) or (4, 3)
        bbox = {"cx": (b[0] + b[2]) / 2, "cy": (b[1] + b[3]) / 2, "w": b[2] - b[0], "h": b[3] - b[1]}
        try:
            plan = await asyncio.to_thread(framing.plan, joints, bbox, "center", 0.1, wh[0] / wh[1], ranges)
        except framing.FramingError as e:
            plan = e.body()
        err = [round(bbox["cx"] - 0.5, 3), round(bbox["cy"] - 0.5, 3)]
        self.job.update(status="DRY_RUN", reason="preview only: moves to computed poses are not enabled",
                        result={"center_error": err, "plan": plan})

    # ------------------------------------------------------------ state and HTTP

    def _save(self):
        try:
            (self.data / "thumbs").mkdir(parents=True, exist_ok=True)
            (self.data / "catalogue.json").write_text(json.dumps({"target": self.target, "catalogue": self.catalogue,
                                                                  "job": self.job}, indent=1))
            for key, jpeg in self.thumbs.items():
                (self.data / "thumbs" / (key.replace("/", "__") + ".jpg")).write_bytes(jpeg)
        except OSError as e:
            self.note(f"could not save the catalogue: {e}")

    def snapshot(self) -> dict:
        obs = self.obs
        tr = self.tracker
        joints = None
        try:
            joints = self.state.arm.joints(max_age_s=1.0) if self.state.arm else None
        except RuntimeError:
            pass
        cam = self.camera_state()
        target = None
        if self.target:
            age = round(time.monotonic() - obs["stamp"], 2) if obs else None
            target = {**self.target, "status": tr.status() if tr else "SEARCHING",
                      "observation": None if obs is None else {k: obs[k] for k in ("box", "quad", "confidence", "method", "evidence", "frame_id", "stable")} | {"age_s": age}}
        order = motion.view_names()
        return {"ok": True, "gate": self.gate(), "camera": cam, "target": target, "localization": self.loc,
                "calibration": {"presets": sorted((self.calibration.get("bias") or {}))},
                "sticker_reference": (self.data / "sticker.png").exists(),
                "arm": {"joints": joints, "where": self.where(), "moving": self.state.moving, "generation": self.state.gen},
                "presets": [{"name": p, "checked": motion.view_checked(p)} for p in order],
                "job": self.job, "catalogue": [self.catalogue[p] for p in order if p in self.catalogue],
                "events": list(self.events)[-12:]}

    def http(self, method: str, path: str, req: dict):
        """Called from an HTTP thread. Returns (status, dict | bytes, content type)."""
        url = urlparse(path)
        name, q = url.path, parse_qs(url.query)
        st = self.state
        if method == "GET" and name == "state":
            return 200, st.call(self._on_loop(self.snapshot)), "application/json"
        if method == "GET" and name == "frame.jpg":
            fid = q.get("id", [None])[0]
            f = self.frame() if fid is None else (self.frame(int(fid)) if fid.isdigit() else None)
            if f is not None and fid is not None:   # the dashboard froze this frame to draw on: keep it until then
                self.pinned = {k: v for k, v in list(self.pinned.items())[-4:]} | {f[0]: f}
            return (200, f[2], "image/jpeg") if f else (404, {"ok": False, "error": "no such frame"}, "")
        if method == "GET" and name.startswith("thumb/"):
            jpeg = self.thumbs.get(name[len("thumb/"):].removesuffix(".jpg"))
            return (200, jpeg, "image/jpeg") if jpeg else (404, {"ok": False, "error": "no such thumbnail"}, "")
        if method == "POST":
            if name == "sim/hand" and st.sim:   # tests and demos without hardware: put a "hand" over the target
                return 200, st.call(self._on_loop(lambda: self._sim_hand(req))), "application/json"
            handlers = {"select": lambda: self.select(req), "autofind": self.autofind, "calibrate": lambda: self.calibrate(req),
                        "clear": lambda: self._on_loop(self.clear), "job": lambda: self._on_loop(lambda: self.start(req))}
            if name in handlers:
                status, body = st.call(handlers[name](), timeout=20)
                return status, body, "application/json"
        return 404, {"ok": False, "error": f"unknown director call {method} {name}"}, ""

    def _sim_hand(self, req: dict) -> dict:
        cam = self.sim_camera
        if cam is None:
            return {"ok": False, "error": "no simulated camera yet"}
        x, y = cam.label_at
        cam.hand = (x + float(req.get("dx", 0.0)), y + float(req.get("dy", 0.0)), float(req.get("z", 0.12)),
                    float(req.get("r", 0.05))) if req.get("on") else None
        return {"ok": True, "hand": cam.hand}

    @staticmethod
    async def _on_loop(fn):
        return fn()
