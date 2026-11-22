"""Camera director inside capture_server.py (--director). Station supplies the frames and local
vision tracks the selected object. Claude can propose camera-space plans from a filming brief.

Who moves the arm: State.start_move and State.start_path share the capture server's command owner and the
stop generation from when the job started, so a STOP from anywhere refuses every later move of the
job; STOP and any person's command also cancel the job (State.command / State._stop_cleanup).
Saved views use motion.plan_along. Computed shots use orbit.py and the motion layer's path checks.

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
import concurrent.futures
import concurrent.futures.process
import json
import multiprocessing
import math
import re
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

from . import brief, fuse, orbit, predict, selfcal, twin
from . import vision as V

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "director_data"            # sticker reference, catalogue, thumbnails (ignored by git)
SAFETY_PATH = ROOT / "director_safety.json"  # the operator's approval for physical moves (ignored by git)
GRIPPER_CAMERA = "usbvideo/74e4d17bc903e40ebc78117539fca31f"   # docs/HARDWARE.md: the follower's gripper camera (Fred's Mac)
# Station names a camera differently per computer. On the lab Linux laptop the gripper camera (jaws visible at
# the bottom of the frame, checked by eye on 22 Nov) is this queue:
GRIPPER_CAMERAS = (GRIPPER_CAMERA, "usbvideo/dc84eab2202fb0ae9704788db7d9f9a4",
                   "usbvideo/ca70cc12cf078fa3798e48cd0a11fabd",   # the same camera after the 22 Nov reboot
                   "usbvideo/c88516e5f5bd836efe44ed66553d8cf5")   # ...and after a replug the same afternoon

TICK_S = 0.2              # frame sampling period
TRACK_S = 0.15            # live tracking period (learned tracker, a few ms per frame; also while the arm moves)
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
ANNOUNCE_S = 2.0          # ...every automatic move is announced this long before it starts
MIN_ANGLE_DEG = 20.0      # ...an alternative must look at the target from at least this different a direction
RECHECK_RATIO = 0.85      # a best view counts as verified when a fresh look scores at least this share
SHOT_HOLD_S = 3.0         # cinematic: how long each shot is held
MOVE_SPEED = "slow"
ACTIVE = ("RUNNING",)


class Hold(Exception):
    """The job stops here; the arm is left holding wherever the last move left it."""


class TooHeavy(Hold):
    """A computed move stopped on the shoulder-load guard (motion.LoadGuardError): lock at the last safe pose."""


FOLLOW_MOVE_M = 0.03      # follow: the object moved this far (fused, 3-D) -> the camera moves with it
SLIP_FRAC = 0.35         # framing: a tracker box this far (picture fraction) from where the object should be has slid
UNFRAMED_VIEWS: tuple = ()          # views never turned onto the object (none: every view centres it, owner 22 Nov)
# "slow" / "fast" in a command change that one motion's speed; Claude's custom takes default to first-run speed
SKILL_FILLER = {"hey", "peter", "claude", "please", "can", "could", "you", "do", "a", "an", "the", "now", "again",
                "for", "me", "us", "film", "shot", "plan", "it", "that", "this", "some", "just", "go", "ahead",
                "fast", "faster", "quick", "quickly", "slow", "slowly", "gently", "carefully"}   # speed: not the move
JOB_SPEEDS = {"slow": motion.SPEED_SLOW, "normal": motion.SPEED_NORMAL, "fast": motion.SPEED_FAST}
TAKE_KEEP_HALF = 0.2      # at most this half-size (picture fraction) of the object must stay in frame in a take
TAKE_LOST_S = 4.0         # a take stops when the object has been out of sight this long
PICK_ABOVE_M = 0.10       # pick: the jaw tips first go this far above the object...
# ...then down to the lowest height (orbit.grasp_low, from 2.2 cm) at which the open jaws stay 8 mm off the desk
PICK_FLOOR_M = 0.002      # the descent may bring a part of the arm this close to the desk (pick only)
PICK_STALL_STEPS = 60     # jaws that stop this far from fully closed are holding something
PICK_MAX_UNCERTAINTY_M = 0.03  # reject shallow desk projections before planning a grasp
WAVE_YAW, WAVE_ROLL = 220, 350   # steps of base sway and wrist roll for a wave
CENTER_STEPS = 8          # framing on the object at a view: at most this many follow-sized corrections
WATCH_STUCK_S = 8.0       # ...or this much later when the last attempt could not turn any further
WATCH_FRAME_S = 1.5       # keep in view re-frames on the object at most this often
CENTER_DEAD = 0.04        # ...until its centre is this close to the picture's centre
FOLLOW_SPEED = motion.SPEED_NORMAL   # follow's corrections: <= 150 steps of base yaw and wrist, shoulder parked
FOLLOW_START = 0.10       # follow corrects once the object's centre is this far off (fraction of the picture)...
FOLLOW_STOP = 0.04        # ...and stops once it is this close: centred enough, no micro-movements
FOLLOW_SMOOTH = 0.5       # low-pass on the measured centre (box jitter)
FOLLOW_JUMP = 0.15        # a centre shift this big is a real move: no smoothing
SNAP_MAX = 200            # steps: "snap" brings the arm onto a saved view only from this close


class Director:
    def __init__(self, state, data_dir: Path = DATA_DIR, safety_path: Path = SAFETY_PATH):
        self.state, self.data, self.safety_path = state, Path(data_dir), Path(safety_path)
        if state.sim:   # simulated references and results must never reach the real arm's auto-find
            self.data = self.data / "sim"
        self.frames: deque = deque(maxlen=FRAME_CACHE)   # (frame_id, monotonic stamp, jpeg)
        self.seq, self._last_jpeg, self.sim_camera = 0, None, None
        self._tracked_at = 0.0
        # all image processing runs in this worker process, never on the arm server's event loop
        self.pool = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                                           initializer=V._worker_init)
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
        framing.CAMERA_CAL = self.calibration.get("model")      # the self-calibrated camera model, if any
        self.samples: list = []              # frames scored during the last sampled move
        self.looks = 0                       # frames examined during the last sampled move (hits are in samples)
        self.reach: dict | None = None       # orbit.reach_map for the current localization
        self.obj = fuse.ObjectFilter()       # the object's position, fused over every tracked frame
        self._framed = False                 # the arm stands next to a view, turned onto the object (trail)
        self._framed_view = None
        self._fuse_bias = (0.0, 0.0)
        self.heavy: set = set()              # views whose approach tripped the shoulder guard: not tried again
        self.trail: list = self._load_trail()   # poses driven by the last scene, for "return" (survives a restart)
        self.thumbs: dict[str, bytes] = {}
        self.events: deque = deque(maxlen=60)
        saved = None if state.sim else V.Reference.load(self.data / "target")
        if saved is not None:                # the object marked before a restart (the tracker re-finds it)
            self._set_target(saved, -1, "restored after restart")
            try:
                self._init_args = (saved, (self.data / "target_frame.jpg").read_bytes(),
                                   json.loads((self.data / "target_box.json").read_text()))
                state.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.vision(V.target_init, *self._init_args)))
            except (OSError, ValueError):
                pass
        state.loop.call_soon_threadsafe(lambda: setattr(self, "_task", asyncio.ensure_future(self._run())))

    # ------------------------------------------------------------ frames and live tracking

    async def vision(self, fn, *args):
        cal = self.calibration.get("model") if hasattr(self, "calibration") else None
        args = (cal, fn) + args
        fn = selfcal.call
        try:
            return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)
        except concurrent.futures.process.BrokenProcessPool:     # the worker died: start a new one, try once more
            self.note("vision worker restarted")
            self.pool = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                                               initializer=V._worker_init)
            if getattr(self, "_init_args", None):
                await asyncio.get_running_loop().run_in_executor(self.pool, selfcal.call, cal, V.target_init, *self._init_args)
            return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)

    def _hint(self):
        """Where to look first: the live tracker's last box while it is fresh."""
        return self.tracker.hint() if self.tracker is not None else None

    def note(self, text: str):
        self.events.append({"t": round(time.time(), 2), "text": text})
        logger.info("director: %s", text)

    async def _run(self):
        while True:
            try:
                f = await self._grab()
                # live tracking, also while the arm moves (the learned tracker follows the motion); the slow
                # SIFT re-find only while the arm stands still
                if f and self.tracker is not None and time.monotonic() - self._tracked_at >= TRACK_S:
                    self._tracked_at = time.monotonic()
                    tracker = self.tracker
                    try:
                        joints = self.state.arm.joints(max_age_s=motion.STALE_S)   # the camera pose of this frame
                    except RuntimeError:
                        joints = None                 # late joint data: track, but do not fuse this frame
                    hit = tracker.record(await self.vision(V.track, tracker.ref, f[2], tracker.hint(),
                                                           not self.state.moving), f[0], f[1])
                    if tracker is self.tracker:
                        self.obs = hit
                        if hit is not None and joints is not None:
                            self._fuse(hit, joints, f[1])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("director tick")
                self.note(f"vision error: {e}")
            await asyncio.sleep(TICK_S)

    def _fuse(self, hit: dict, joints: dict, stamp: float):
        """Fold one tracked frame into the object's 3-D position (fuse.ObjectFilter)."""
        b = hit["box"]
        u, v = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        w = self.where()
        if w.get("where") in ("view", "between"):
            self._fuse_bias = predict.bias_for(w, self.calibration)
        bias = self._fuse_bias                # off the views: keep the last view's offset (no jump as the camera turns)
        pose = framing.camera_pose(joints, self.state.arm.ranges())
        d = framing.ray(u - bias[0], v - bias[1], pose, self._aspect())
        size = (b[2] - b[0] + b[3] - b[1]) / 2
        if self.obj.x is None:
            if d[2] > -0.1:
                return                        # not looking down at the table: no starting point yet
            point = np.array(pose[0]) + np.array(d) * (framing.TABLE_Z - pose[0][2]) / d[2]
            self.obj.start(point, pose[0], size, stamp)
        elif not self.state.moving:           # while moving the tracker box lags the picture: the ray would be off
            self.obj.update(pose[0], d, size, stamp)

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
            return {"mode": "SIMULATED", "moves": True, "reasons": [], "lines": None, "max_fraction": motion.ALONG_MAX,
                    "computed": True}
        reasons, approval = [], {}
        try:
            config = json.loads(self.safety_path.read_text())
            if not isinstance(config, dict):
                raise ValueError("not an object")
            approval = config
        except FileNotFoundError:
            reasons.append("the operator has not approved physical director moves (no director_safety.json)")
        except (OSError, ValueError) as e:
            reasons.append(f"director_safety.json is unreadable: {e}")
        if not reasons:
            if approval.get("physical_motion") is not True:
                reasons.append("director_safety.json does not set physical_motion: true")
            for key in ("approved_by", "approved_at"):
                if not approval.get(key):
                    reasons.append(f"director_safety.json has no {key}")
            if approval.get("camera_queue") != self.state.cam_queue:
                reasons.append(f"the approved camera {approval.get('camera_queue')} is not the selected camera {self.state.cam_queue}")
            if self.state.arm is None or approval.get("bus") != self.state.arm.bus_serial:
                reasons.append("the approved follower bus is not the connected arm")
        lines = approval.get("lines", [])
        if not isinstance(lines, list) or any(not isinstance(line, str) or line.count("-") != 1 for line in lines):
            reasons.append("director_safety.json lines must be a list of preset pairs")
            lines = []
        frac = approval.get("max_fraction", 0.0)
        if type(frac) not in (int, float) or not 0.0 <= frac <= 1.0:
            reasons.append("director_safety.json max_fraction must be between 0 and 1")
            frac = 0.0
        return {"mode": "DRY RUN" if reasons else "PHYSICAL", "moves": not reasons, "reasons": reasons,
                "lines": sorted(lines), "max_fraction": min(frac, motion.ALONG_MAX),
                # moves to computed poses (scene, return): a separate opt-in; on the real arm the shoulder servo's
                # overload protection tripped three times during them (22 Nov)
                "computed": not reasons and approval.get("computed_paths") is True}

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
        try:
            bias = self._record_bias(w["view"], joints, obs["box"], pt)
        except ValueError as e:
            return 409, {"ok": False, "error": str(e)}
        self.job.update(status="DONE", step="done", result={"preset": w["view"], "bias": bias}, ended_at=round(time.time(), 2))
        self.note(f"camera correction for {w['view']}: {bias} (image fractions) from the point {point[:2]} m")
        return 200, {"ok": True, "preset": w["view"], "bias": bias,
                     "bias_px": [round(bias[0] * 1280), round(bias[1] * 720)]}

    def _record_bias(self, view: str, joints: dict, box: list, pt: list) -> tuple:
        point = [float(pt[0]), float(pt[1]), framing.TABLE_Z]
        bias = predict.measured_bias(joints, box, point, self.state.arm.ranges(), self._aspect())
        bias = (float(bias[0]), float(bias[1]))
        self.calibration.setdefault("bias", {})[view] = list(bias)
        self.calibration["reference_point_m"] = point[:2]
        self.calibration.setdefault("records", {})[view] = {
            "point_m": point, "box": box, "measured_joints": {str(m): v for m, v in joints.items()}, "at": round(time.time(), 1)}
        self.data.mkdir(parents=True, exist_ok=True)
        self._calibration_path().write_text(json.dumps(self.calibration, indent=1))
        self.sightings, self.loc = [], None      # earlier sightings were cast without this correction
        self.note(f"camera correction for {view}: {bias} from the point {point[:2]} m")
        return bias

    async def _calibrate_tour(self, req: dict, gate: dict):
        """The object lies at a tape-measured point: visit each checked preset (approved routes), look once
        from the preset itself and record its camera correction where the object is visible."""
        pt = req.get("point_m") or self.calibration.get("reference_point_m")
        if not (isinstance(pt, list) and len(pt) == 2):
            raise Hold("give point_m [x, y]: where the object lies, tape-measured from the arm base")
        if not gate["moves"]:
            raise Hold("physical moves not approved: move the arm to each preset and calibrate there instead")
        presets = [p for p in (req.get("presets") or motion.view_names()) if motion.view_checked(p)]
        done = {}
        self.job["phase"] = "calibrate"
        for i, p in enumerate(presets, 1):
            self.step(f"calibrating {p}", preset=p, preset_index=i, preset_count=len(presets))
            obs = await self.go_shot(p, None, 0.0, f"{p}/calibration")
            if obs["valid"]:
                done[p] = list(self._record_bias(p, {int(m): v for m, v in obs["measured_joints"].items()}, obs["box"], pt))
            else:
                done[p] = f"not visible: {obs['reason']}"
        self.job["result"] = {"point_m": pt, "presets": done}

    # ------------------------------------------------------------ target

    def _set_target(self, ref: V.Reference, frame_id: int, how: str, box: list | None = None):
        self.ref, self.tracker, self.obs = ref, V.Tracker(ref), None
        self.obj.reset()
        if how != "restored after restart":
            try:
                ref.save(self.data / "target")
            except OSError as e:
                self.note(f"could not save the target for a restart: {e}")
        f = self.frame(frame_id)
        if f is not None and box is not None:     # start the learned tracker on this exact frame and box
            self._init_args = (ref, f[2], list(box))
            asyncio.ensure_future(self.vision(V.target_init, *self._init_args))
            try:                                  # ...and again from them after a restart
                (self.data / "target_frame.jpg").write_bytes(f[2])
                (self.data / "target_box.json").write_text(json.dumps(list(box)))
            except OSError:
                pass
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
        self._set_target(ref, f[0], "saved sticker crop" if as_sticker else "drawn box", [x1, y1, x2, y2])
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
        hits = [await self.vision(V.look, ref, f[2]) for f in frames]
        found = [h for h in hits if h]
        consistent = len(found) >= 2 and V._iou(found[0]["box"], found[-1]["box"]) > 0.5
        if not consistent:
            return 404, {"ok": False, "error": "not_found",
                         "detail": f"The sticker was recognised in {len(found)} of 3 fresh frames; not enough to be sure. "
                                   "Go to the top view, or draw a box around it with Select object."}
        self._set_target(ref, frames[-1][0], "auto-found sticker", found[-1]["box"])
        self.obs = {**found[-1], "frame_id": frames[-1][0], "stamp": frames[-1][1], "stable": True}
        return 200, {"ok": True, "target": self.target, "observation": found[-1]}

    def clear(self) -> tuple[int, dict]:
        if self.busy():
            return 409, {"ok": False, "error": "a director job is running; press STOP first"}
        self.target = self.ref = self.tracker = self.obs = None
        return 200, {"ok": True}

    # ------------------------------------------------------------ observing a settled view

    async def _joints(self, wait_s: float = 3.0) -> dict:
        """Fresh joint readings; rides out a short gap in Station's data instead of failing the job."""
        end = time.monotonic() + wait_s
        while True:
            try:
                return self.state.arm.joints(max_age_s=motion.STALE_S)
            except RuntimeError:
                if time.monotonic() > end:
                    raise
                await asyncio.sleep(0.1)

    async def settle(self) -> tuple[dict, float]:
        await asyncio.sleep(SETTLE_S)
        try:
            prev = (await self._joints())
            for _ in range(20):
                await asyncio.sleep(0.15)
                now = (await self._joints())
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
        hits, sc = await self.vision(V.look_many, ref, [f[2] for f in frames])
        pick = max(range(len(frames)), key=lambda i: hits[i]["confidence"] if hits[i] else -1)
        label = f"{key}  {sc['score']:.2f}" if sc["valid"] else f"{key}  not seen"
        self.thumbs[key] = self._thumb(frames[pick][2], hits[pick]["box"] if hits[pick] else None, label)
        return {**sc, "thumb": key, "measured_joints": {str(m): v for m, v in joints.items()},
                "frame_ids": [f[0] for f in frames], "observed_at": round(time.time(), 2)}

    # ------------------------------------------------------------ moving

    async def move(self, view: str, along: tuple[str, float] | None = None, sample: bool = False,
                   *, direct_only: bool = False) -> bool:
        """One deliberate move through the capture server's command owner. True when it arrived, False when
        the motion layer refused before commanding anything; Hold when a move failed part-way. With `sample`,
        frames seen on the way are scored (self.samples) so a better view passed en route can be pulled back to."""
        if self._framed and self.where().get("where") != "view":
            self._framed = False              # the shot was framed on the object: back onto its view first
            await self._return(self.trail)
        self.job["moving_to"] = view if not along else f"{view} → {along[0]} {along[1]:.0%}"
        self._own_move, self.samples, self.looks = True, [], 0
        try:
            try:
                await self._joints()          # a fresh reading first: a momentary gap would refuse the move ('no arm data')
                self.state.note_data()
                await self.state.start_move(view, MOVE_SPEED, gen=self.job_gen, along=along, load_guard=True,
                                            direct_only=direct_only or (self.job["kind"] == "selfcal" and not self.state.sim))
            except asyncio.CancelledError:
                raise
            except Exception as e:     # refused by the command owner (STOP since the job began, busy, no data)
                raise Hold(f"move refused: {e}")
            task = self.state.move_task
            while not task.done():
                await asyncio.wait({task}, timeout=0.5)      # never cancels the move: only STOP does
                if sample and not task.done():
                    await self._sample()
            out = self.state.move_outcome or {}
            await self._fresh_where()
        finally:
            self._own_move = False
            self.job["moving_to"] = None
        if out.get("ok"):
            return True
        if out.get("hold") is None:          # refused before any goal was written: the arm did not move
            self.note(f"move to {view} {along or ''} refused: {out.get('error')}")
            return False
        raise Hold(f"the move to {view} did not finish: {out.get('error')}")

    async def _sample(self) -> bool:
        f = self.frame()
        try:
            joints = (await self._joints())
        except RuntimeError:
            return False
        if f is None or time.monotonic() - f[1] > 0.3 or self.ref is None:
            return False
        self.looks += 1
        hit = await self.vision(V.track, self.ref, f[2], self._hint(), False)
        if hit:
            self.samples.append({"joints": joints, "score": hit["frame_score"], "box": hit["box"]})
        return hit is not None

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

    async def _fresh_where(self):
        """Wait for the server's new answer on where the arm is: right after a move the cached one is empty."""
        self.state.refresh_where()
        task = getattr(self.state, "where_task", None)
        if task is not None:
            await asyncio.wait({task}, timeout=3)

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
                   "tour": self._tour, "center": self._center, "follow": self._follow, "watch": self._watch, "calibrate": self._calibrate_tour,
                   "scene": self._scene, "return": self._return_job, "snap": self._snap, "gimbal": self._gimbal,
                   "selfcal": self._selfcal, "show": self._show, "film": self._film, "goto": self._goto, "wave": self._wave, "skill": self._skill,
                   "gesture": self._gesture, "pick": self._pick}
        if not isinstance(kind, str) or kind not in runners:
            return 400, {"ok": False, "error": f"kind must be one of {sorted(runners)}"}
        if kind == "film" and (not isinstance(req.get("brief"), str) or not 1 <= len(req["brief"].strip()) <= 500):
            return 400, {"ok": False, "error": "enter a filming brief of 1–500 characters"}
        if self.busy():
            return 409, {"ok": False, "error": f"a {self.job['kind']} job is already running"}
        if self.state.stopping:
            return 409, {"ok": False, "error": "STOP is still being handled"}
        if kind == "gesture" and (not isinstance(req.get("brief"), str) or not 1 <= len(req["brief"].strip()) <= 300):
            return 400, {"ok": False, "error": "say which gesture"}
        if kind in ("film", "gesture") and (learned := self._skill_for(req["brief"])):
            kind, req = "skill", {**req, "kind": "skill", "name": learned["name"]}   # done before: replay it
        if kind not in ("tour", "return", "snap", "goto", "wave", "skill", "gesture") and self.ref is None:
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

    async def localize(self, look: bool = True, *, current_only: bool = False) -> dict:
        """Localize from a new settled sighting alone, or from accumulated sightings for camera jobs."""
        sightings = self.sightings
        if look:
            joints, t = await self.settle()
            obs = await self.observe("locate", joints, t)
            if not obs["valid"]:
                raise Hold(f"the target is not visible from the current pose ({obs['reason']}); point the camera at it first")
            if current_only:
                sightings = [(joints, obs["box"], predict.bias_for(self.where(), self.calibration))]
            else:
                self._add_sighting(joints, obs["box"])
                sightings = self.sightings
        self.loc = await asyncio.to_thread(predict.localize, sightings, self.state.arm.ranges(), self._aspect())
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

    # ---- cinematic: seamless glides between verified shots, cuts where no glide keeps the target in frame

    def _shot(self, r: dict) -> dict:
        b = r["best"]
        return {"id": r["preset"], "preset": r["preset"], "toward": b.get("toward"), "s": b.get("s") or 0.0,
                "score": b["score"], "thumb": b["thumb"]}

    def _transitions(self, shots: list[dict], gate: dict) -> dict:
        ranges, grip = self.state.arm.ranges(), self.state.arm.joints()[motion.GRIPPER]
        lines, views = predict.checked_lines(ranges, grip, gate if gate["moves"] else {"lines": None})
        out = {}
        for a in shots:
            for b in shots:
                if a is not b:
                    out[(a["id"], b["id"])] = predict.best_transition(a, b, lines, views, self.loc, ranges,
                                                                      self._aspect(), self.calibration, grip)
        return out

    @staticmethod
    def _local_order(shots: list[dict], trans: dict) -> list[str]:
        """The order with the most seamless transitions, then the most time in frame, then the best shots first."""
        import itertools
        best = None
        for order in itertools.permutations([s["id"] for s in shots]):
            steps = [trans[(a, b)] for a, b in zip(order, order[1:])]
            key = (sum(t["seamless"] for t in steps), sum(t["visible"] for t in steps),
                   -sum(i * next(s["score"] for s in shots if s["id"] == sid) for i, sid in enumerate(order)))
            if best is None or key > best[0]:
                best = (key, list(order))
        return best[1]

    def _claude_order(self, shots: list[dict], trans: dict) -> tuple[list[str], dict[str, float], str]:
        """Claude as planner only: it picks the order and hold times from the shot list and the transition table
        (no images, no joints). The plan is validated here; anything unknown or malformed raises ValueError."""
        import anthropic
        from claude_config import claude_api_key, load_env
        load_env(ROOT / ".env")
        key = claude_api_key()
        if not key:
            raise ValueError("no Claude key (CLAUDE_API_KEY in .env)")
        table = {"shots": [{"id": s["id"], "position": s["preset"] if not s["toward"] else
                            f"{s['s']:.0%} of the way from {s['preset']} toward {s['toward']}", "score": s["score"]} for s in shots],
                 "transitions": [{"from": a, "to": b, "seamless": t["seamless"], "target_in_frame_share": t["visible"]}
                                 for (a, b), t in trans.items()]}
        ids = [s["id"] for s in shots]
        schema = {"type": "object", "additionalProperties": False, "required": ["order", "hold_seconds", "reason"],
                  "properties": {"order": {"type": "array", "items": {"type": "string", "enum": ids}},
                                 "hold_seconds": {"type": "array", "items": {"type": "number"}},
                                 "reason": {"type": "string"}}}
        client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
        response = client.beta.messages.create(
            model="claude-opus-5-5", max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            system=("You plan a short robot-arm camera take of one object. The local controller has already verified "
                    "every shot and every move; you only choose the order and how long to hold each shot. Prefer "
                    "seamless transitions (the camera glides and keeps the object in frame); a non-seamless one "
                    "becomes a cut. Use each shot at most once, start with a strong establishing shot, and hold "
                    "each shot 2 to 6 seconds. Answer with the JSON only."),
            messages=[{"role": "user", "content": json.dumps(table)}])
        if response.stop_reason == "refusal":
            raise ValueError("Claude declined to plan")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        order = [o for o in data["order"] if o in ids]
        if not order or len(set(order)) != len(order):
            raise ValueError(f"unusable order from Claude: {data['order']}")
        holds = {sid: max(2.0, min(6.0, float(h))) for sid, h in zip(order, data["hold_seconds"])}
        return order, holds, str(data.get("reason", ""))[:300]

    async def _glide(self, segments: list) -> None:
        """Move along the planned glide: each segment ends at a preset or a stop along its line."""
        for line, _f0, f1 in segments:
            x, y = sorted(line)
            if f1 <= 1e-6:
                ok = await self.move(x)
            elif f1 >= 1 - 1e-6:
                ok = await self.move(y)
            elif f1 <= motion.ALONG_MAX:
                ok = await self.move(x, (y, round(f1, 3)))
            elif 1 - f1 <= motion.ALONG_MAX:
                ok = await self.move(y, (x, round(1 - f1, 3)))
            else:
                raise Hold("the glide ends too far from a preset")
            if not ok:
                raise Hold(f"the glide along {x}-{y} was refused")

    async def _cinematic(self, req: dict, gate: dict):
        shots = [self._shot(r) for r in self._shots(req)]
        if not shots:
            raise Hold("no verified best shots yet: run Optimize all views first")
        if self.loc is None:
            raise Hold("the target is not localized; run Optimize all views first")
        trans = await asyncio.to_thread(self._transitions, shots, gate)
        planner, holds, reason = "local", {}, ""
        if req.get("planner") == "claude":
            try:
                order, holds, reason = await asyncio.to_thread(self._claude_order, shots, trans)
                planner = "claude"
            except Exception as e:      # Claude is optional: the local plan always works
                self.note(f"Claude planner unavailable ({type(e).__name__}: {e}); using the local plan")
                order, reason = self._local_order(shots, trans), f"local plan (Claude: {e})"
        else:
            order = self._local_order(shots, trans)
        by_id = {s["id"]: s for s in shots}
        plan = []
        for i, sid in enumerate(order):
            t = trans.get((order[i - 1], sid)) if i else None
            plan.append({**by_id[sid], "hold_s": holds.get(sid, SHOT_HOLD_S),
                         "transition": None if t is None else {"seamless": t["seamless"], "visible": t["visible"],
                                                                "why": t.get("why")}})
        self.job["result"] = {**(self.job["result"] or {}), "planner": planner, "plan_reason": reason,
                              "storyboard": [{k: p[k] for k in ("preset", "toward", "s", "score", "thumb", "hold_s", "transition")}
                                             for p in plan], "shots": []}
        if not gate["moves"]:
            self.job.update(status="BLOCKED", reason="physical moves not approved: storyboard only (no moving take)")
            return
        for i, p in enumerate(plan, 1):
            t = trans.get((order[i - 2], p["id"])) if i > 1 else None
            if t and t["seamless"]:
                self.job["phase"] = "cinematic"          # the glide is part of the take
                self.step(f"shot {i} of {len(plan)}: gliding to {p['preset']} (in frame {t['visible']:.0%})",
                          shot_index=i, shot_count=len(plan), preset=p["preset"])
                await self._glide(t["segments"])
                joints, tt = await self.settle()
                look = await self.observe(f"{p['preset']}/shot", joints, tt)
            else:
                self.job["phase"] = "reposition"         # a cut: the recording pauses while the arm repositions
                self.step(f"shot {i} of {len(plan)}: repositioning to {p['preset']} (cut)",
                          shot_index=i, shot_count=len(plan), preset=p["preset"])
                look = await self.go_shot(p["preset"], p["toward"], p["s"], f"{p['preset']}/shot")
                self.job["phase"] = "cinematic"
            if not look["valid"]:
                raise Hold(f"target lost at shot {i} ({p['preset']}): {look['reason']}")
            self.step(f"shot {i} of {len(plan)}: holding {p['preset']}")
            misses, end = 0, time.monotonic() + p["hold_s"]
            while time.monotonic() < end:
                await asyncio.sleep(0.5)
                f = (await self.fresh_frames(1))[0]
                hit = await self.vision(V.track, self.ref, f[2], self._hint())
                misses = 0 if hit else misses + 1
                if misses >= 2:
                    raise Hold(f"target lost while holding shot {i} ({p['preset']})")
            self.job["result"]["shots"].append({"preset": p["preset"], "score": look["score"],
                                                "arrived_by": "glide" if t and t["seamless"] else "cut"})

    # ---- free camera scenes: moves generated around the object (arcs, cranes, push-ins, mixes)

    async def move_path(self, poses: list[dict], label: str, sample: bool = False, require_visible: bool = False,
                        speed: float = motion.SPEED_FIRST, floor: float | None = None) -> None:
        """One continuous computed move (motion.go_through) through the command owner, frames scored if `sample`."""
        self.job["moving_to"] = label
        self._own_move, self.samples, self.looks = True, [], 0
        try:
            try:
                await self._joints()          # a fresh reading first: a momentary gap would refuse the move ('no arm data')
                self.state.note_data()
                await self.state.start_path(poses, label, gen=self.job_gen, speed=speed, floor=floor)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                raise Hold(f"move refused: {e}")
            task = self.state.move_task
            seen_at = time.monotonic()
            while not task.done():
                await asyncio.wait({task}, timeout=0.4)
                if sample and not task.done():
                    if await self._sample():
                        seen_at = time.monotonic()
                    # quick sways (a dance, a nod) hide the object for a moment; only a take that really lost it stops
                    if require_visible and time.monotonic() - seen_at > TAKE_LOST_S and not task.done():
                        self.job["phase"] = "occluded"
                        await self.state.stop("subject lost during the take")
            out = self.state.move_outcome or {}
            await self._fresh_where()
        finally:
            self._own_move = False
            self.job["moving_to"] = None
        if not out.get("ok"):
            if out.get("load_guard") and out.get("hold") == "confirmed":
                raise TooHeavy(f"{label}: {out.get('error')}")
            raise Hold(f"{label} did not finish: {out.get('error')}")

    @staticmethod
    def _bridge(a: dict, b: dict, step: int = 200) -> list[dict]:
        """Joint-space waypoints from a to b at most `step` apart (the path check then vouches for each)."""
        n = max(1, int(math.ceil(max(abs(b[m] - a[m]) for m in motion.MOVABLE) / step)))
        return [{m: int(round(a[m] + (b[m] - a[m]) * k / n)) for m in motion.MOVABLE} for k in range(1, n + 1)]

    async def ensure_reach(self) -> dict:
        if self.reach is None or self.reach.get("point") != self.loc["point_m"]:
            self.step("mapping where the camera can go around the object")
            views = motion.load_views()
            seeds = tuple(views[n] for n in ("wide", "left", "right", "top") if n in views)
            m = await self.vision(orbit.reach_map, self.loc["point_m"], self.state.arm.joints(), self.state.arm.ranges(), seeds)
            self.reach = {**m, "point": self.loc["point_m"]}
        return self.reach

    def _local_scene(self, reach: dict) -> list[dict]:
        """A contextual default scene from the reach map: a wide establishing arc at the elevation and distance
        with the widest continuous reach, craning up through the front, then a push-in at the end."""
        best = None
        for key, ths in reach["ok"].items():
            e, d = (float(v) for v in key.split("|"))
            runs, run = [], []
            for t in reach["thetas"]:
                if t in ths:
                    run.append(t)
                else:
                    if run:
                        runs.append(run)
                    run = []
            if run:
                runs.append(run)
            for r in runs:
                span = r[-1] - r[0]
                if best is None or (span, -abs((r[0] + r[-1]) / 2), e) > best[0]:
                    best = ((span, -abs((r[0] + r[-1]) / 2), e), e, d, r)
        if best is None or best[0][0] <= 0:
            raise Hold("the reach map shows no arc around the object; try another object position")
        _, e, d, r = best
        mid = (r[0] + r[-1]) / 2
        return [{"name": "establishing arc with a crane", "keyframes": [
                    {"theta": r[0], "elev": e, "dist": d},
                    {"theta": mid, "elev": min(e + 10, 75), "dist": d},
                    {"theta": r[-1], "elev": e, "dist": d}]},
                {"name": "push in", "keyframes": [
                    {"theta": r[-1], "elev": e, "dist": d},
                    {"theta": r[-1] - 15 if r[-1] > mid else r[-1] + 15, "elev": min(e + 10, 75), "dist": max(0.14, d - 0.04)}]}]

    def _claude_scene(self, reach: dict) -> tuple[list[dict], str]:
        """Claude as scene planner: designs moves (keyframes) from the reach map. Validated and executed locally."""
        import anthropic
        from claude_config import claude_api_key, load_env
        load_env(ROOT / ".env")
        key = claude_api_key()
        if not key:
            raise ValueError("no Claude key (CLAUDE_API_KEY in .env)")
        kf = {"type": "object", "additionalProperties": False, "required": ["theta", "elev", "dist"],
              "properties": {"theta": {"type": "number"}, "elev": {"type": "number"}, "dist": {"type": "number"}}}
        schema = {"type": "object", "additionalProperties": False, "required": ["moves", "reason"],
                  "properties": {"reason": {"type": "string"},
                                 "moves": {"type": "array", "items": {
                                     "type": "object", "additionalProperties": False, "required": ["name", "keyframes"],
                                     "properties": {"name": {"type": "string"}, "keyframes": {"type": "array", "items": kf}}}}}}
        brief = {"object": {"label": self.target["label"], "size_cm": round(self.loc["size_m"] * 100, 1)},
                 "camera_space": "theta = azimuth around the object in degrees (0 = from the robot's base side, +90 = "
                                 "from the right, -90 = from the left); elev = degrees above the desk; dist = metres "
                                 "from the object",
                 "reachable": {k: v for k, v in reach["ok"].items()}}
        client = anthropic.Anthropic(api_key=key, timeout=40.0, max_retries=1)
        response = client.beta.messages.create(
            model="claude-opus-5-5", max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            system=("You are the director of a robot camera arm filming one small object on a desk for a video. Design "
                    "2 to 4 camera moves that make good footage: arcs around the object, crane moves up or down, "
                    "push-ins and pull-outs, and combinations. Each move is a list of keyframes the camera glides "
                    "through. Use only theta/elev/dist combinations listed as reachable (key 'elev|dist' -> reachable "
                    "thetas); keyframes between listed values are fine if both neighbours are reachable. Keep each "
                    "move continuous (neighbouring keyframes close in the reachable set). Answer with the JSON only."),
            messages=[{"role": "user", "content": json.dumps(brief)}])
        if response.stop_reason == "refusal":
            raise ValueError("Claude declined to plan")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        moves = []
        for mv in data["moves"][:4]:
            frames = [{"theta": max(-170.0, min(170.0, float(k["theta"]))), "elev": max(20.0, min(80.0, float(k["elev"]))),
                       "dist": max(0.13, min(0.3, float(k["dist"])))} for k in mv["keyframes"][:8]]
            if len(frames) >= 2:
                moves.append({"name": str(mv["name"])[:60], "keyframes": frames})
        if not moves:
            raise ValueError("Claude returned no usable move")
        return moves, str(data.get("reason", ""))[:400]

    async def _scene(self, req: dict, gate: dict):
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        if not gate.get("computed"):
            # free-camera moves are off on this arm (shoulder overload): film the scene with the moves it does
            # within its limit: glides between views and gimbal arcs with the shoulder parked (Director Show)
            self.note("scene: computed poses are off on this arm, filming it as a Director Show")
            await self._show(req, gate)
            self.job["result"]["planner"] = "director show (shoulder-safe moves)"
            return
        if self.where().get("where") not in ("view", "between"):
            await self.ensure_view()
        w = self.where()
        self.job["phase"] = "estimate"
        if self.loc is None:
            await self.localize()
        home_pose = (await self._joints())
        planner, reason = "request", ""
        moves = req.get("moves")
        reach = None if moves else await self.ensure_reach()
        if not moves and req.get("planner") == "claude":
            try:
                moves, reason = await asyncio.to_thread(self._claude_scene, reach)
                planner = "claude"
            except Exception as e:
                self.note(f"Claude scene planner unavailable ({type(e).__name__}: {e}); using the local scene")
                reason = f"local scene (Claude: {e})"
        if not moves:
            moves, planner = self._local_scene(reach), ("local" if planner != "claude" else planner)
        bias = predict.bias_for(w, self.calibration)
        views = motion.load_views()
        seeds = tuple(views[n] for n in ("wide", "left", "right", "top") if n in views)
        self.job["result"] = {"planner": planner, "plan_reason": reason, "moves": [], "reach": reach and reach["ok"]}
        here = home_pose
        trail = self.trail = [{m: home_pose[m] for m in motion.MOVABLE}]   # every pose driven, to retrace later
        self._lifted_from = None
        await self._scene_moves(moves, here, trail, bias, seeds)
        self.job["phase"] = "reposition"
        await self._return(trail)
        if self._lifted_from:                    # came up through transit: back down by the normal route
            self.step(f"back to {self._lifted_from}")
            await self.move(self._lifted_from)

    async def _film(self, req: dict, gate: dict):
        """Film a free-form brief using fresh measured framing and a locally checked camera path."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        start_where = self.where()
        self.job["phase"] = "estimate"
        self.step("looking at the marked subject")
        joints, stamp = await self.settle()
        look = await self.observe("film/start", joints, stamp)
        if not look["valid"]:
            look = self._live_look() or look   # a plain or big object: the live tracker sees it, feature matching not
        if not look["valid"]:
            raise Hold("the marked subject is not visible")
        self.sightings, self.loc = [], None
        self._add_sighting(joints, look["box"])
        await self.localize(look=False)
        ranges, aspect, point = self.state.arm.ranges(), self._aspect(), self.loc["point_m"]
        pose = framing.camera_pose(joints, ranges)
        x, y, z = np.asarray(pose[0]) - point
        context = {"subject": self.target["label"], "box": look["box"],
                   "camera": {"theta": round(math.degrees(math.atan2(x, -y)), 1),
                              "elev": round(math.degrees(math.atan2(z, math.hypot(x, y))), 1),
                              "dist": round(math.sqrt(x*x + y*y + z*z), 3)}}
        computed = gate["computed"]
        where = self.where()
        actions = {"hold": "Record the current framing without moving",
                   "watch": "Keep the object visible until STOP, cutting only on sustained occlusion"}
        if where.get("where") in ("view", "between"):
            actions.update({f"view:{v['name']}": v["description"] for v in self.state.snapshot()["guided"]
                            if motion.view_checked(v["name"])})
            actions["show"] = "Cinematic show: establish wide, push in to top, arc, pull out, and hold wide"
            actions["gimbal"] = "Gimbal take: the base swings the camera around the object, the wrist keeps it centred"
            actions["follow"] = "Follow the object: keep turning the camera so it stays centred, until STOP"
            actions["wave"] = "Wave hello: the base sways and the wrist rolls three times, then back"
            if self._shots({}):
                actions["cinematic"] = "Play this object's verified shots using checked glides or cuts"
        context.update(actions=actions, current_pose=where)

        async def ask():
            self.step("Claude is planning the shot")
            try:
                return await asyncio.to_thread(brief.plan, req["brief"].strip(), context, computed)
            except ValueError as e:
                raise Hold(str(e)) from e
            except Exception as e:
                raise Hold(f"Claude planning failed ({type(e).__name__})") from e

        plan = await ask()
        action = plan.get("action", "custom")
        if action != "custom":
            if action not in actions:
                raise Hold("the selected camera command is unavailable")
            summary = {"planner": "Claude", "plan_reason": plan["reason"][:400], "action": action}
            try:
                if action in ("watch", "show", "cinematic", "gimbal", "follow", "wave"):
                    await {"watch": self._watch, "show": self._show, "cinematic": self._cinematic,
                           "gimbal": self._gimbal, "follow": self._follow, "wave": self._wave}[action](
                        {"hold": plan["hold"], "speed": req.get("speed")}, gate)
                else:
                    if action.startswith("view:"):
                        self.job["phase"] = "reposition"
                        view = action.removeprefix("view:")
                        self.step(f"moving to the checked {view} view")
                        await self.go_shot(view, None, 0, "film/checked-view")
                    self.job["phase"] = "cinematic"
                    if not await self._hold_shot(plan["hold"], action.removeprefix("view:")):
                        raise Hold("marked subject left the shot")
            finally:
                self.job["result"] = {**(self.job["result"] or {}), **summary}
            return
        if not plan["shots"]:
            raise Hold(plan["reason"][:400])
        self.job["result"] = {"planner": "Claude", "plan_reason": plan["reason"][:400], "moves": []}
        if computed:
            trail = self.trail = [{m: joints[m] for m in motion.MOVABLE}]
            views = motion.load_views()
            seeds = tuple(views[n] for n in ("wide", "left", "right", "top") if n in views)
            await self._scene_moves(plan["shots"], joints, trail, predict.bias_for(self.where(), self.calibration),
                                    seeds, filming=True)
            if not any(m["done"] for m in self.job["result"]["moves"]):
                raise Hold("no locally feasible camera path for this brief")
            return
        retry = True
        take_speed = JOB_SPEEDS.get(req.get("speed"), motion.SPEED_FIRST)
        driven: list[dict] = []               # every commanded pose of the takes: the learned move
        i = 0
        while i < len(plan["shots"]):
            shot = plan["shots"][i]
            self.job["phase"] = "estimate"
            self.step(f"checking {shot['name'][:60]}", shot_index=i + 1, shot_count=len(plan["shots"]))
            if i:
                joints, stamp = await self.settle()
                look = await self.observe("film/start", joints, stamp)
                if not look["valid"]:
                    look = self._live_look() or look
                if not look["valid"]:
                    raise Hold("subject lost before the next shot")
            box = look["box"]
            centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            model = framing.project(point, framing.camera_pose(joints, ranges), aspect)
            if model is None:
                raise Hold("the camera model places the subject behind the camera")
            bias = tuple(a - b for a, b in zip(centre, model))
            # the part of the object that must stay in the picture through the take: all of a small one, the
            # middle of a big one (a board filling the frame would refuse every gesture otherwise)
            half_box = (min((box[2] - box[0]) / 2, TAKE_KEEP_HALF), min((box[3] - box[1]) / 2, TAKE_KEEP_HALF))
            path = await self.vision(orbit.composed_take, point, joints, ranges, shot["keyframes"], bias, aspect, half_box,
                                     self.calibration.get("shoulder_reach_limit_m"))
            if not path["ok"]:
                if i == 0 and retry:
                    retry = False
                    context["local_rejection"] = path["reason"]
                    context["actions"] = {}
                    plan = await ask()
                    if not plan["shots"]:
                        raise Hold(plan["reason"][:400])
                    self.job["result"]["plan_reason"] = plan["reason"][:400]
                    continue
                raise Hold(f"the brief cannot be filmed here: {path['reason']}")
            name = shot["name"][:60]
            trail = [{m: joints[m] for m in motion.MOVABLE}]
            self.trail = trail + path["stage"] + path["path"][1:]
            self._save_trail(self.trail)
            self.job["phase"] = "reposition"
            try:
                await self.move_path(path["stage"], f"set up {name}", speed=take_speed)
            except TooHeavy as e:
                self._lock_at_limit(trail + path["stage"], name, str(e))
            joints, stamp = await self.settle()
            ready = await self.observe("film/ready", joints, stamp)
            if not ready["valid"]:
                ready = self._live_look() or ready
            if not ready["valid"]:
                raise Hold("marked subject is not visible in the opening position")
            self.job["phase"] = "cinematic"
            composed = any(abs(k.get("u", 0.5) - 0.5) > 0.15 or abs(k.get("v", 0.5) - 0.5) > 0.15
                           for k in shot["keyframes"])    # the brief asked for an off-centre composition
            if not await self._hold_shot(2.0, f"opening {name}", center=not composed):
                raise Hold("marked subject left the opening shot")
            self.step(name)
            try:
                await self.move_path(path["path"][1:], name, sample=True, require_visible=True, speed=take_speed)
            except TooHeavy as e:
                self._lock_at_limit(trail + path["stage"] + path["path"][1:], name, str(e))
            seen = round(len(self.samples) / self.looks, 2) if self.looks else None
            steady = await self._hold_shot(shot["hold"], name, center=not composed)
            self.job["result"]["moves"].append({"name": name, "done": True, "complete": steady, "in_frame": seen})
            if not steady:
                raise Hold("marked subject left the closing shot")
            driven += path["stage"] + path["path"][1:]
            i += 1
        if start_where.get("where") == "view" and driven:     # it worked: remember it as a repeatable move
            saved = self._save_skill(req["brief"], start_where["view"], driven, take_speed)
            self.job["result"]["learned"] = saved["name"]
            self.note(f"learned a new move: “{saved['name']}” (say it again, or replay it from the menu)")

    def _trail_path(self) -> Path:
        return self.data / "trail.json"

    def _load_trail(self) -> list:
        try:
            return [{int(m): v for m, v in q.items()} for q in json.loads(self._trail_path().read_text())]
        except (OSError, ValueError):
            return []

    def _save_trail(self, trail: list):
        try:
            self.data.mkdir(parents=True, exist_ok=True)
            self._trail_path().write_text(json.dumps(trail))
        except OSError:
            pass

    def _lock_at_limit(self, trail: list[dict], name: str, why: str) -> None:
        """Remember the measured boundary and stop this job while motion's hold stays in effect."""
        now = self.state.arm.joints(max_age_s=motion.STALE_S)
        i = min(range(len(trail)), key=lambda k: max(abs(trail[k][m] - now[m]) for m in motion.MOVABLE))
        self.trail = trail[:i + 1]
        self._save_trail(self.trail)
        reach = orbit.shoulder_reach(now, self.state.arm.ranges(), now[motion.GRIPPER])
        old = self.calibration.get("shoulder_reach_limit_m")
        limit = round(reach - 0.005, 3) if old is None else round(min(old, reach - 0.005), 3)
        self.calibration["shoulder_reach_limit_m"] = limit
        try:
            self._calibration_path().write_text(json.dumps(self.calibration, indent=1))
        except OSError:
            pass
        self.note(f"shoulder limit: {why}; holding at {reach * 100:.1f} cm reach, "
                  f"limit now {limit * 100:.1f} cm")
        self.job["result"] = self.job["result"] or {}
        self.job["result"].setdefault("moves", []).append({"name": name, "done": True, "complete": False,
                                            "why": "stopped at the shoulder's limit",
                                            "shoulder_reach_limit_m": limit})
        raise TooHeavy(f"{name}: holding at the shoulder's limit")

    async def _return(self, trail: list[dict]):
        """Retrace driven poses back to the start, beginning at the trail pose nearest to where the arm is."""
        now = (await self._joints())
        i = min(range(len(trail)), key=lambda k: max(abs(trail[k][m] - now[m]) for m in motion.MOVABLE))
        gap = max(abs(trail[i][m] - now[m]) for m in motion.MOVABLE)
        if gap > motion.PATH_STEP_MAX:
            raise Hold(f"the arm is {gap} steps from the path it came along; recover with Arm controls")
        back = list(reversed(trail[:i + 1]))
        if len(back) > 1 or gap > motion.SKIP_TOL:
            self.step("returning to the start pose along the path it came")
            await self.move_path(back, "return")
        self.trail = []
        self._save_trail([])

    async def ensure_view(self, best_for_object: bool = False) -> str:
        """Get onto a saved view without bothering the operator: finish a line to its nearer end, snap onto a view
        the arm stopped close to, and (best_for_object) move on to the checked view predicted to show the marked
        object best. Only a pose far from every view and line needs the operator (Recover in Arm controls)."""
        w = self.where()
        if w.get("where") == "between":
            near = w["view"] if w.get("s", 0.0) < 0.5 else w["to"]
            self.step(f"finishing the line to {near}")
            if not await self.move(near):
                raise Hold(f"the route to {near} was refused")
        elif w.get("where") != "view":
            now = (await self._joints())
            views = motion.load_views()
            name, gap = min(((n, max(abs(v[m] - now[m]) for m in motion.MOVABLE)) for n, v in views.items()
                             if n != motion.TRANSIT), key=lambda x: x[1])
            if gap > SNAP_MAX:
                if w.get("where") in ("hub", "spoke", "above"):
                    target = "top" if "top" in views else name
                    self.step(f"going to {target}")
                    if not await self.move(target):
                        raise Hold(f"the route to {target} was refused")
                elif self.trail:          # a free take ended here: go back the way it came, then onto its view
                    await self._return(self.trail)
                    return await self.ensure_view(best_for_object)
                else:
                    raise Hold(f"the arm is {gap} steps from the nearest view ({name}); use Recover in Arm controls")
            else:
                self.step(f"snapping onto {name}")
                await self.move_path(self._bridge(now, {m: views[name][m] for m in motion.MOVABLE}, step=100),
                                     f"snap to {name}")
        view = self.where().get("view") or ""
        if best_for_object and self.loc is not None:
            gate = self.gate()
            preds = self.predict_all(gate)
            ranked = sorted(((p["best"]["score"], name) for name, p in preds.items()
                             if p["best"] and not p["best"].get("toward") and motion.view_checked(name)), reverse=True)
            if ranked and ranked[0][1] != view and ranked[0][0] > 0.5:
                self.step(f"moving to {ranked[0][1]}, the view predicted best for the object")
                if await self.move(ranked[0][1]):
                    view = ranked[0][1]
        return self.where().get("view") or view

    async def _gimbal(self, req: dict, gate: dict):
        """Gimbal take: shoulder and elbow stay where the current view holds them; base yaw swings the camera
        around and the wrist joints keep the marked object centred (a parallax arc), out, across and back."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        self.job["phase"] = "reposition"
        view = await self.ensure_view()
        self.job["phase"] = "estimate"
        if self.loc is None:
            try:
                await self.localize()
            except Hold:                    # not visible from here: try the best-known view for it
                view = await self.ensure_view(best_for_object=True)
                await self.localize()
        # start where a gimbal take actually works: the current view first, then the views predicted best for the
        # object (never the folded home pose); plan each before moving anywhere
        yaw, roll = int(req.get("yaw", 500)), float(req.get("roll", 0.0))
        preds = self.predict_all(gate)
        ranked = [n for _, n in sorted(((p["best"]["score"], n) for n, p in preds.items() if p["best"]), reverse=True)]
        grip = (await self._joints())[motion.GRIPPER]
        chosen = None
        for cand in dict.fromkeys([view] + ranked):
            if cand in ("home", motion.TRANSIT) or cand in self.heavy or not motion.view_checked(cand):
                continue
            self.step(f"checking a gimbal take from {cand}")
            bias = predict.bias_for({"where": "view", "view": cand}, self.calibration)
            shot = await self.vision(orbit.gimbal_shot, self.loc["point_m"], {**motion.load_views()[cand],
                                     motion.GRIPPER: grip}, self.state.arm.ranges(), yaw, roll, bias, self._aspect())
            if not shot["ok"]:
                continue
            if cand != self.where().get("view"):
                self.job["phase"] = "reposition"
                self.step(f"moving to {cand} for the gimbal take")
                try:
                    if not await self.move(cand):
                        continue
                except Hold as e:
                    if "load stayed above" not in str(e):
                        raise
                    self.heavy.add(cand)        # the shoulder strains on the way there: back, and try the next view
                    self.note(f"{cand} skipped: {e}")
                    self.step(f"the shoulder strains on the way to {cand}; back to {view}")
                    if not await self.move(view):
                        raise Hold(f"the route back to {view} was refused") from e
                    continue
            chosen = cand
            break
        if chosen is None:
            raise Hold("no view gives a gimbal take that keeps the object in frame"
                       + (f" (skipped, the shoulder strains: {', '.join(sorted(self.heavy))})" if self.heavy else ""))
        self.job["result"] = await self._gimbal_take(chosen, yaw, roll)

    async def _gimbal_take(self, view: str, yaw: int, roll: float = 0.0) -> dict:
        """Plan and shoot one gimbal take from `view` (the arm is on it), then put it exactly back on the view."""
        w = {"where": "view", "view": view}
        joints, t = await self.settle()
        bias = predict.bias_for(w, self.calibration)
        look = await self.observe(f"{w['view']}/gimbal-start", joints, t)
        if look["valid"]:
            # keep the framing the view already has (no lurch to recentre first): aim so the object stays where the
            # camera sees it now. bias_eff = bias - (seen - centre), in the aim's real-minus-model convention.
            c = ((look["box"][0] + look["box"][2]) / 2, (look["box"][1] + look["box"][3]) / 2)
            bias = (bias[0] - (c[0] - 0.5), bias[1] - (c[1] - 0.5))
        self.step("planning the gimbal take")
        shot = await self.vision(orbit.gimbal_shot, self.loc["point_m"], {**motion.load_views()[w["view"]],
                                 motion.GRIPPER: joints[motion.GRIPPER]}, self.state.arm.ranges(),
                                 yaw, roll, bias, self._aspect())
        if not shot["ok"]:
            raise Hold(f"no gimbal take from {w['view']}: {shot['reason']}")
        self.job["phase"] = "cinematic"
        self.step(f"gimbal take from {w['view']}: yaw {shot['yaw_range'][0]:+d}..{shot['yaw_range'][1]:+d} steps")
        path = self._bridge(joints, shot["path"][0], step=100) + shot["path"][1:]
        trail = [{m: joints[m] for m in motion.MOVABLE}] + path
        self._save_trail(trail)
        try:
            await self.move_path(path, "gimbal take", sample=True)
        except TooHeavy as e:
            self._lock_at_limit(trail, "gimbal take", str(e))
        seen = len(self.samples) / self.looks if self.looks else None
        self.job["phase"] = "reposition"
        await self.snap_home(w["view"])
        return {"view": w["view"], "yaw_range": shot["yaw_range"], "poses": shot["poses"],
                "complete": shot["complete"], "why": shot["reason"],
                "in_frame": None if seen is None else round(seen, 2), "moves": []}

    async def _hold_shot(self, seconds: float, label: str, center: bool = True):
        """A steady held shot: the editor's clean in/out point. Watches that the object stays in the picture.
        The shot is framed on the object first (the view is where the arm stands; the object is what it films)."""
        if center:                            # a planned composition (a Claude brief) is kept as planned
            await self._center_on_object()
        self.step(f"holding: {label}")
        end, misses = time.monotonic() + seconds, 0
        while time.monotonic() < end:
            await asyncio.sleep(0.5)
            f = (await self.fresh_frames(1))[0]
            hit = await self.vision(V.track, self.ref, f[2], self._hint())
            misses = 0 if hit else misses + 1
            if misses >= 3:
                self.note(f"the object left the picture while holding {label}")
                return False
        return True

    async def _show(self, req: dict, gate: dict):
        """Director Show: film grammar with the moves this arm does within its shoulder limit.
        Establish wide (hold) -> push in along the checked wide-top line (hold) -> gimbal arc around the
        object with the shoulder parked (hold) -> pull back out to wide (hold). One deliberate move at a
        time, slow and eased, steady holds between them; recording follows the cinematic phase."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        views = motion.load_views()
        wide, close = req.get("wide", "wide"), req.get("close", "top")
        if wide not in views or close not in views:
            raise Hold(f"needs the views {wide} and {close}")
        if self.where().get("where") not in ("view", "between"):
            await self.ensure_view()
        hold = float(req.get("hold", 3.0))
        shots = []
        if not (self.where().get("where") == "view" and self.where().get("view") == wide):
            self.job["phase"] = "reposition"
            self.step(f"getting to the establishing view ({wide})")
            if not await self.move(wide):
                raise Hold(f"the route to {wide} was refused")
        self.job["phase"] = "cinematic"
        shots.append({"shot": f"establishing ({wide})", "steady": await self._hold_shot(hold, f"establishing, {wide}")})
        self.step(f"push in: {wide} -> {close}")
        if not await self.move(close):
            raise Hold(f"the push-in to {close} was refused")
        shots.append({"shot": f"push in to {close}", "steady": await self._hold_shot(hold * 0.7, f"{close}")})
        if self.loc is None:
            self.job["phase"] = "estimate"
            await self.localize()
            self.job["phase"] = "cinematic"
        try:
            take = await self._gimbal_take(close, int(req.get("yaw", 500)))
            shots.append({"shot": f"arc around the object at {close}", **take})
        except Hold:
            self.job["result"] = {**(self.job["result"] or {}), "shots": shots}
            raise
        self.job["phase"] = "cinematic"
        shots.append({"shot": f"after the arc ({close})", "steady": await self._hold_shot(hold * 0.7, close)})
        self.step(f"pull out: {close} -> {wide}")
        if not await self.move(wide):
            raise Hold(f"the pull-out to {wide} was refused")
        shots.append({"shot": f"closing wide ({wide})", "steady": await self._hold_shot(hold, f"closing, {wide}")})
        self.job["result"] = {"shots": shots}

    def _live_look(self) -> dict | None:
        """The live tracker's fresh, confident detection as a settled look (YOLOE, or ViT above VIT_MIN)."""
        o, f = self.obs, self.frame()
        if not o or not f or f[0] - o["frame_id"] > 8 or (o.get("method") == "vit" and o["confidence"] < V.VIT_MIN):
            return None
        return {"valid": True, "box": o["box"], "confidence": o["confidence"], "method": o.get("method")}

    def _note_once(self, text: str):
        if getattr(self, "_last_note", None) != text:
            self._last_note = text
            self.note(text)

    async def _center_on_object(self) -> int:
        """At a view: turn the camera (base yaw + wrist, shoulder and elbow on the view) until the tracked object is
        centred, in follow's small checked steps. The steps are saved as the trail, so the next move steps back
        onto the view first (move(), ensure_view)."""
        w = self.where()
        if self.tracker is None:
            return 0
        if w.get("where") == "view" and w.get("view") in UNFRAMED_VIEWS:
            return 0                          # the owner's side views stay exactly as taught
        if w.get("where") == "view" and w.get("view") != motion.TRANSIT:
            view = w["view"]
            vpose = {m: motion.load_views()[view][m] for m in motion.MOVABLE}
            commanded, steps = dict(vpose), []
        elif self._framed and self.trail:     # already framed next to a view: refine from there
            view, vpose = self._framed_view, dict(self.trail[0])
            commanded, steps = dict(self.trail[-1]), list(self.trail[1:])
        else:
            return 0
        bias = predict.bias_for({"where": "view", "view": view}, self.calibration)
        ranges, since = self.state.arm.ranges(), (self.frame() or (-1,))[0]
        misses, good = 0, dict(commanded)     # good: the last pose that saw the object where it should be
        expect = None                          # where the object should appear after the last turn (picture)
        grip = (await self._joints())[motion.GRIPPER]
        for _ in range(CENTER_STEPS):
            obs = None
            for _ in range(30):               # a confident detection from after the last step
                await asyncio.sleep(0.1)
                o = self.obs
                if o is not None and o["frame_id"] > since and not (o.get("method") == "vit" and o["confidence"] < V.VIT_MIN):
                    obs = o
                    break
            if obs is not None and expect is not None and obs.get("method") == "vit":
                b = obs["box"]
                if math.hypot((b[0] + b[2]) / 2 - expect[0], (b[1] + b[3]) / 2 - expect[1]) > SLIP_FRAC:
                    # the box jumped far from where the last turn should have put the object: the tracker slid onto
                    # something else. Look for the object there (in the picture; the camera model is not trusted
                    # at extreme side views).
                    f = self.frame()
                    found = await self.vision(V.refind, self.ref, f[2], expect) if f else None
                    self._note_once(f"framing at {view}: the tracker slid off; " + (
                        f"re-found the object ({found['method']})" if found else "the object is not where it should be"))
                    obs = found
            if obs is None:
                misses += 1
                if misses >= 2 and good != commanded:
                    # lost it (or the tracker slid onto something else): back to where it was last seen, then retry
                    self.step(f"lost the object: back to where it was last seen at {view}")
                    await self.move_path([good], f"back to the object at {view}", speed=FOLLOW_SPEED)
                    commanded, misses = dict(good), 0
                    steps.append(commanded)
                    since = (self.frame() or (since,))[0]
                    continue
                if misses >= 2:
                    self._note_once(f"framing at {view}: the object is not seen")
                    break
                continue
            misses, good = 0, dict(commanded)
            b = obs["box"]
            c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            # after the next turn the object should sit part of the way toward the picture's centre
            expect = (c[0] + orbit.FOLLOW_GAIN * (0.5 - c[0]), c[1] + orbit.FOLLOW_GAIN * (0.5 - c[1]))
            b = obs["box"]
            centre = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            if abs(centre[0] - 0.5) < CENTER_DEAD and abs(centre[1] - 0.5) < CENTER_DEAD:
                break
            step = await self.vision(orbit.follow_step, vpose, {**commanded, motion.GRIPPER: grip}, ranges, centre,
                                     bias, self._aspect())
            if not step["ok"]:
                self._note_once(f"framing at {view}: cannot turn further ({step['reason'][:100]})")
                break
            self.step(f"framing the object at {view}")
            await self.move_path([step["pose"]], f"frame the object at {view}", speed=FOLLOW_SPEED)
            commanded = dict(step["pose"])
            steps.append(commanded)
            since = (self.frame() or (since,))[0]
        moved = len(steps) - (len(self.trail) - 1 if self._framed and w.get("where") != "view" else 0)
        if steps:
            self.trail = [vpose] + steps
            self._save_trail(self.trail)
            self._framed, self._framed_view = True, view
        return max(0, moved)

    async def snap_home(self, view: str):
        """After a take that ended next to its view, put the arm exactly back on it."""
        now = (await self._joints())
        v = motion.load_views()[view]
        if max(abs(v[m] - now[m]) for m in motion.MOVABLE) > 15:
            await self.move_path(self._bridge(now, {m: v[m] for m in motion.MOVABLE}, step=100), f"back onto {view}")

    async def _selfcal(self, req: dict, gate: dict):
        """Calibrate the camera model from the arm's own joint readings and camera images of the marked object
        (any object; it must not move): look from the saved views and stops along the approved lines, then fit
        (camera_director/selfcal.py). No measurements. Afterwards every object is localized from images alone."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        start_view = await self.ensure_view()
        views = motion.load_views()
        plan = [(start_view, None, 0.0)]
        if gate["mode"] == "PHYSICAL":
            if start_view not in ("top", "wide") or "top-wide" not in gate["lines"] or gate["max_fraction"] < 0.16:
                raise Hold("start at top or wide with the top-wide line approved for calibration")
            other = "wide" if start_view == "top" else "top"
            if not all(n in views and motion.view_checked(n) for n in (start_view, other)):
                raise Hold("self-calibration needs checked top and wide views")
            end = min(0.32, gate["max_fraction"])
            fractions = [round(end * i / 4, 4) for i in range(1, 5)]
            plan += [(start_view, other, s) for s in fractions]
            plan += [(other, start_view, s) for s in reversed(fractions)]
            plan.append((other, None, 0.0))
        else:                                # simulator: more viewing angles for fit validation
            for name in ("top", "wide", "home"):
                if name in views and name != start_view and motion.view_checked(name):
                    plan.append((name, None, 0.0))
            for a, b in (("top", "wide"), ("right", "wide"), ("right", "top")):
                for s in (0.16, 0.32):
                    plan += [(a, b, s), (b, a, s)]
        obs, skipped = [], []
        self.job["phase"] = "calibrate"
        old = framing.CAMERA_CAL
        for i, (p, toward, s) in enumerate(plan, 1):
            self.step(f"self-calibration: view {i} of {len(plan)}", preset_index=i, preset_count=len(plan))
            try:
                look = await self.go_shot(p, toward, s, "selfcal/last")
            except Hold as e:
                skipped.append(f"{p}{'→' + toward if toward else ''}: {e}")
                self.job["result"] = {"ok": False, "views": len(obs), "skipped": skipped}
                raise Hold(f"self-calibration stopped after {len(obs)} usable views: {e}") from e
            if look["valid"]:
                c = ((look["box"][0] + look["box"][2]) / 2, (look["box"][1] + look["box"][3]) / 2)
                obs.append(({int(m): v for m, v in look["measured_joints"].items()}, c))
            else:
                skipped.append(f"{p}{'→' + toward if toward else ''}: object not seen")
        # Keep the current camera model active until a replacement fit is accepted.
        estimate = self.loc or predict.localize([(j, [u, v, u, v]) for j, (u, v) in obs],
                                                self.state.arm.ranges(), self._aspect())
        point0 = estimate.get("point_m") or [0.0, 0.3, 0.0]
        self.step(f"fitting the camera model to {len(obs)} views")
        fit = await asyncio.get_running_loop().run_in_executor(self.pool, selfcal.fit, obs, self.state.arm.ranges(),
                                                                self._aspect(), point0, True,
                                                                (old or {}).get("roll_180") is True)
        self.job["result"] = {**{k: v for k, v in fit.items() if k != "cal"}, "skipped": skipped}
        if not fit["ok"] or fit["rms_after"] > 0.05:
            framing.CAMERA_CAL = old
            raise Hold(f"self-calibration did not converge well: {fit.get('reason') or fit.get('rms_after')}")
        self.calibration["model"] = fit["cal"]
        self.calibration["model_fit"] = {k: fit[k] for k in ("rms_before", "rms_after", "views", "params")}
        framing.CAMERA_CAL = fit["cal"]
        self.data.mkdir(parents=True, exist_ok=True)
        self._calibration_path().write_text(json.dumps(self.calibration, indent=1))
        self.sightings, self.loc = [], None       # re-localize with the calibrated model
        self.note(f"self-calibrated from {fit['views']} views: image error {fit['rms_before']:.3f} -> {fit['rms_after']:.3f}")
        if gate["mode"] != "PHYSICAL":
            self.step(f"back to {start_view}")
            await self.move(start_view)

    async def _snap(self, req: dict, gate: dict):
        """Back onto the nearest saved view with one short checked move, when the arm stopped close to it
        (instead of the motion layer's lift to transit). Only within SNAP_MAX steps on every joint."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        now = (await self._joints())
        views = motion.load_views()
        name, gap = min(((n, max(abs(v[m] - now[m]) for m in motion.MOVABLE)) for n, v in views.items()),
                        key=lambda x: x[1])
        if gap > SNAP_MAX:
            raise Hold(f"the nearest view ({name}) is {gap} steps away; use Recover (lift to transit) instead")
        self.step(f"snapping back onto {name} ({gap} steps)")
        await self.move_path(self._bridge(now, {m: views[name][m] for m in motion.MOVABLE}, step=100), f"snap to {name}")
        self.job["result"] = {"view": name, "gap_steps": gap}

    # ---- learned moves: Claude-planned motions that worked, kept with the joint poses they drove ----
    def _skills_path(self) -> Path:
        return self.data / "skills.json"

    def _skills(self) -> list[dict]:
        try:
            return json.loads(self._skills_path().read_text())
        except (OSError, ValueError):
            return []

    @staticmethod
    def _skill_key(text: str) -> str:
        words = re.findall(r"[a-z0-9']+", text.lower())
        return " ".join(w for w in words if w not in SKILL_FILLER)

    def _skill_for(self, text: str) -> dict | None:
        key = self._skill_key(text or "")
        return next((s for s in self._skills() if key and (key == s["key"] or re.search(rf"\b{re.escape(s['key'])}\b", key))),
                    None)

    def _save_skill(self, brief: str, view: str, poses: list[dict], speed: float) -> dict:
        key = self._skill_key(brief)[:60] or "move"
        skill = {"name": key, "key": key, "brief": brief.strip()[:200], "view": view, "speed": speed,
                 "poses": [{str(m): int(p[m]) for m in motion.MOVABLE} for p in poses], "created": round(time.time())}
        skills = [s for s in self._skills() if s["key"] != key] + [skill]
        self.data.mkdir(parents=True, exist_ok=True)
        self._skills_path().write_text(json.dumps(skills, indent=1))
        return skill

    async def _skill(self, req: dict, gate: dict):
        """Replay a learned move: back to the view it started from, then exactly the poses it drove, checked again."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        skill = next((s for s in self._skills() if s["name"] == req.get("name")), None)
        if skill is None:
            raise Hold(f"no learned move called {req.get('name')!r}")
        self.job["phase"] = "reposition"
        await self.ensure_view()
        if self.where().get("view") != skill["view"]:
            self.step(f"going to {skill['view']}, where “{skill['name']}” starts")
            if not await self.move(skill["view"]):
                raise Hold(f"the route to {skill['view']} was refused")
        ranges = self.state.arm.ranges()
        env = motion.envelope(ranges)
        start = motion.clamp_pose({m: motion.load_views()[skill["view"]][m] for m in motion.MOVABLE}, env)
        poses = [{int(m): v for m, v in p.items()} for p in skill["poses"]]
        # in the vision worker: thousands of fk checks on this loop would starve the arm's data (a 'no arm data' refusal)
        problem = await self.vision(motion.path_problem, [start] + poses + [start], ranges, env,
                                    (await self._joints())[motion.GRIPPER])
        if problem:
            raise Hold(f"“{skill['name']}” no longer passes the checks: {problem}")
        self.job["phase"] = "cinematic"
        self.step(f"replaying “{skill['name']}”")
        await self.move_path(poses + [start], skill["name"][:60],
                             speed=JOB_SPEEDS.get(req.get("speed"), skill.get("speed", motion.SPEED_NORMAL)))
        self.job["result"] = {"replayed": skill["name"], "view": skill["view"]}

    async def _gesture(self, req: dict, gate: dict):
        """A gesture of the arm itself ('do a little dance', 'nod', 'bow'): Claude choreographs joint offsets from
        the current view; only the arm's own safety is checked (envelope, desk, itself). No subject involved."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        self.job["phase"] = "reposition"
        view = await self.ensure_view()
        if not view or view == motion.TRANSIT:
            raise Hold("a gesture starts from a saved view")
        self.job["phase"] = "estimate"
        self.step("Claude is choreographing the gesture")
        try:
            plan = await asyncio.to_thread(brief.plan_gesture, req["brief"].strip())
        except ValueError as e:
            raise Hold(str(e)) from e
        except Exception as e:
            raise Hold(f"Claude planning failed ({type(e).__name__})") from e
        ranges = self.state.arm.ranges()
        env = motion.envelope(ranges)
        vpose = motion.clamp_pose({m: motion.load_views()[view][m] for m in motion.MOVABLE}, env)
        deg = 4096 / 360
        keys = [motion.clamp_pose({**vpose, 1: vpose[1] + round(f["yaw"] * deg), 3: vpose[3] + round(f["bob"] * deg),
                                   6: vpose[6] + round(f["pitch"] * deg), 7: vpose[7] + round(f["roll"] * deg)}, env)
                for f in plan["keyframes"]] + [vpose]
        poses, prev = [], vpose
        for k in keys:                        # split big swings: the path allows PATH_STEP_MAX per waypoint
            n = max(1, math.ceil(max(abs(k[m] - prev[m]) for m in motion.MOVABLE) / 300))
            poses += [{m: round(prev[m] + (k[m] - prev[m]) * i / n) for m in motion.MOVABLE} for i in range(1, n + 1)]
            prev = k
        grip = (await self._joints())[motion.GRIPPER]
        problem = await self.vision(motion.path_problem, [vpose] + poses, ranges, env, grip)   # off this loop
        if problem:
            raise Hold(f"that gesture does not fit here: {problem}")
        self.job["result"] = {"planner": "Claude", "plan_reason": plan["reason"][:300], "view": view}
        self.job["phase"] = "cinematic"
        self.step(f"{req['brief'].strip()[:40]}")
        await self.move_path(poses, req["brief"].strip()[:60] or "gesture",
                             speed=JOB_SPEEDS.get(req.get("speed"), motion.SPEED_NORMAL))
        saved = self._save_skill(req["brief"], view, poses, JOB_SPEEDS.get(req.get("speed"), motion.SPEED_NORMAL))
        self.job["result"]["learned"] = saved["name"]
        self.note(f"learned a new move: “{saved['name']}” (say it again, or replay it from the menu)")

    async def _pick(self, req: dict, gate: dict):
        """Pick up the marked object (owner, 22 Nov): the only job allowed down to the table. From a view: open the
        jaws, move above the object, descend slowly to grip height (desk floor lowered for this path only), close
        until the jaws stall on it, lift straight back up and hold there. Every path is checked like any other and
        runs with the shoulder load guard."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        self.job["phase"] = "reposition"
        view = await self.ensure_view()
        if not view or view == motion.TRANSIT:
            raise Hold("picking up starts from a saved view")
        self.job["phase"] = "estimate"
        self.step("locating the object")
        loc = await self.localize(current_only=True)
        if loc["uncertainty_m"] > PICK_MAX_UNCERTAINTY_M:
            line = "-".join(sorted((view, "top")))
            if (view != "top" and all(motion.view_checked(n) for n in (view, "top"))
                    and (gate["lines"] is None or line in gate["lines"])):
                self.job["phase"] = "reposition"
                self.step("getting a clearer view from above")
                if not await self.move("top", direct_only=True):
                    raise Hold("the route to top was refused; use Arm controls to position the camera above the object")
                self.job["phase"] = "estimate"
                loc = await self.localize(current_only=True)
            if loc["uncertainty_m"] > PICK_MAX_UNCERTAINTY_M:
                raise Hold(f"the marked object's position is uncertain by {loc['uncertainty_m'] * 100:.1f} cm; "
                           "use a view looking down at the object and mark it again")
        x, y, z = loc["point_m"]
        z = max(0.0, z)
        ranges = self.state.arm.ranges()
        lo, hi = ranges[motion.GRIPPER]
        jaw_open, jaw_closed = int(lo + 0.8 * (hi - lo)), int(lo + 10)
        now = await self._joints()
        start = {m: now[m] for m in motion.MOVABLE}
        above = await self.vision(orbit.grasp_pose, {**start, motion.GRIPPER: jaw_open}, ranges, (x, y, z + PICK_ABOVE_M))
        low = await self.vision(orbit.grasp_low, {**above["pose"], motion.GRIPPER: jaw_open}, ranges, x, y, z, jaw_open)
        if not above["ok"] or not low["ok"]:
            raise Hold(f"the jaws cannot reach the marked object from above "
                       f"(estimated position {loc['point_m']} m; grasp error "
                       f"{max(above['error_m'], low['error_m']) * 100:.1f} cm)")
        approach = self._bridge(start, above["pose"], step=100)
        down = await self.vision(orbit.grasp_descent, {**above["pose"], motion.GRIPPER: jaw_open}, ranges, x, y, z,
                                 PICK_ABOVE_M, low["height_m"])
        self.trail = [start] + approach            # the way back to the view for the next move
        self._save_trail(self.trail)
        self.job["phase"] = "cinematic"
        self.step("opening the jaws")
        await self.state.set_gripper(jaw_open)
        await asyncio.sleep(1.0)
        self.step("moving above the object")
        await self.move_path(approach, "above the object")
        self.step("descending to the object")
        await self.move_path(down, "down to the object", floor=PICK_FLOOR_M)
        self.step("closing the jaws")
        await self.state.set_gripper(jaw_closed)
        last, steady = None, 0
        for _ in range(20):                        # until the jaws stop: on the object, or fully closed
            await asyncio.sleep(0.2)
            pos = (await self._joints())[motion.GRIPPER]
            steady = steady + 1 if last is not None and abs(pos - last) <= 4 else 0
            last = pos
            if steady >= 3:
                break
        gripped = last is not None and last > jaw_closed + PICK_STALL_STEPS
        self.step("lifting")
        await self.move_path(list(reversed(down[:-1])) + [above["pose"]], "lift", floor=PICK_FLOOR_M)
        self.job["result"] = {"gripped": gripped, "jaws": last, "object_m": [round(x, 3), round(y, 3), round(z, 3)],
                              "holding_above": True}
        self.note("picked it up: holding it above the table" if gripped else
                  "the jaws closed all the way: nothing was gripped (the object may be elsewhere)")
        if not gripped:
            raise Hold("nothing was gripped: the jaws closed all the way")

    async def _wave(self, req: dict, gate: dict):
        """Wave hello: from a view, the base sways and the wrist rolls, three times, then back. Shoulder and elbow
        stay on the view; the whole gesture is checked like a gimbal take before it starts."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        self.job["phase"] = "reposition"
        view = await self.ensure_view()
        if not view or view == motion.TRANSIT:
            raise Hold("wave starts from a saved view")
        ranges = self.state.arm.ranges()
        env = motion.envelope(ranges)
        vpose = motion.clamp_pose({m: motion.load_views()[view][m] for m in motion.MOVABLE}, env)
        # side, centre, other side, centre: each swing stays under the path's per-waypoint step limit
        sway = [p for _ in range(3) for s in (1, -1)
                for p in (motion.clamp_pose({**vpose, 1: vpose[1] + s * WAVE_YAW, 7: vpose[7] + s * WAVE_ROLL}, env),
                          vpose)]
        grip = (await self._joints())[motion.GRIPPER]
        problem = await self.vision(motion.path_problem, [vpose] + sway, ranges, env, grip)    # off this loop
        if problem:
            raise Hold(f"no room to wave here: {problem}")
        self.job["phase"] = "cinematic"
        self.step(f"waving hello from {view}")
        await self.move_path(sway, "wave", speed=JOB_SPEEDS.get(req.get("speed"), motion.SPEED_NORMAL))
        self.job["result"] = {"view": view}

    async def _goto(self, req: dict, gate: dict):
        """A view move from where a take left the arm (off every view): back along the take's path, then on."""
        view = req.get("view")
        if view not in motion.load_views():
            raise Hold(f"unknown view {view!r}")
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        self.job["phase"] = "reposition"
        await self.ensure_view()
        if self.where().get("view") != view:
            self.step(f"going to {view}")
            if not await self.move(view):
                raise Hold(f"the route to {view} was refused")
        if req.get("frame_object") and self.ref is not None:
            await self._center_on_object()
        self.job["result"] = {"view": view, "framed": self._framed}

    async def _return_job(self, req: dict, gate: dict):
        if not gate["moves"] or not gate.get("computed"):
            raise Hold("moves to computed poses are not approved (computed_paths in director_safety.json)")
        if not self.trail:
            raise Hold("no scene path to retrace")
        await self._return(self.trail)

    async def _scene_moves(self, moves, here, trail, bias, seeds, filming=False):
        views = motion.load_views()
        transit = {**views[motion.TRANSIT], motion.GRIPPER: here[motion.GRIPPER]}
        for i, mv in enumerate(moves, 1):
            self.step(f"move {i} of {len(moves)}: planning {mv['name']}", shot_index=i, shot_count=len(moves))
            now = (await self._joints())
            can_lift = not filming and len(trail) == 1 and self.where().get("where") == "view"
            starts = [now, transit] if can_lift else [now]
            plan = await self.vision(orbit.plan_shot, self.loc["point_m"], starts, self.state.arm.ranges(),
                                     mv["keyframes"], bias, self._aspect(), seeds,
                                     self.calibration.get("shoulder_reach_limit_m"), self.loc if filming else None)
            if not plan["ok"]:
                self.job["result"]["moves"].append({"name": mv["name"], "done": False, "why": plan["reason"]})
                if filming:
                    raise Hold(f"{mv['name'][:60]}: {plan['reason']}")
                continue
            self.job["phase"] = "reposition"
            if plan["start_index"] == 1:          # the direct approach would dip: up to transit by the normal route
                self._lifted_from = self.where()["view"]
                self.step(f"move {i} of {len(moves)}: rising to transit first")
                if not await self.move(motion.TRANSIT):
                    raise Hold("the route up to the transit pose was refused")
                now = (await self._joints())
                trail[:] = [{m: now[m] for m in motion.MOVABLE}]
            self.step(f"move {i} of {len(moves)}: getting into position for {mv['name']}")
            approach = self._bridge(now, plan["poses"][0])
            self._save_trail(trail + approach)        # saved before moving: a Stop midway still knows the way back
            try:
                await self.move_path(approach, f"approach {mv['name']}")
            except TooHeavy as e:
                self._lock_at_limit(trail + approach, mv["name"], str(e))
            trail.extend(approach)
            joints, t = await self.settle()
            look = await self.observe(f"scene/{i}-start", joints, t)
            if filming and not look["valid"]:
                raise Hold("marked subject is not visible in the opening position")
            if look["valid"]:          # measure the aim from the real camera and replan the move with it
                c = ((look["box"][0] + look["box"][2]) / 2, (look["box"][1] + look["box"][3]) / 2)
                first = plan["keyframes"][0]
                aim = (bias[0] + c[0] - first.get("u", .5), bias[1] + c[1] - first.get("v", .5))
                plan2 = await self.vision(orbit.plan_shot, self.loc["point_m"], [joints], self.state.arm.ranges(),
                                          plan["keyframes"], aim, self._aspect(), seeds,
                                          self.calibration.get("shoulder_reach_limit_m"), self.loc if filming else None)
                if plan2["ok"] and len(plan2["poses"]) >= len(plan["poses"]) - 1:
                    plan, bias = plan2, aim
            self.job["phase"] = "cinematic"
            if filming and not await self._hold_shot(2.0, "opening"):
                raise Hold("marked subject left the opening shot")
            self.step(f"move {i} of {len(moves)}: {mv['name']}")
            now = (await self._joints())
            path = self._bridge(now, plan["poses"][0]) + plan["poses"][1:]
            self._save_trail(trail + path)
            try:
                await self.move_path(path, mv["name"], sample=True, require_visible=filming)
            except TooHeavy as e:
                self._lock_at_limit(trail + path, mv["name"], str(e))
            trail.extend(path)
            if filming:
                if not await self._hold_shot(mv["hold"], mv["name"]):
                    raise Hold("marked subject left the closing shot")
            else:
                await asyncio.sleep(1.0)            # a beat at the end of the move
            seen = len(self.samples) / self.looks if self.looks else None
            self.job["result"]["moves"].append({"name": mv["name"], "done": True, "complete": plan["complete"],
                                                "why": plan["reason"], "adjusted": plan["adjusted"],
                                                "covered": [plan["samples"][0], plan["samples"][-1]],
                                                "poses": len(plan["poses"]),
                                                "in_frame": None if seen is None else round(seen, 2),
                                                "aim_bias": [round(v, 3) for v in bias]})

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
        """Keep in view's look: the tracker decides whether the object is there (plain objects have too few features
        to judge); matched features, when there are some, add the inlier count that shows a hand covering part of it."""
        f = (await self.fresh_frames(1))[0]
        hit = await self.vision(V.track, self.ref, f[2], self._hint())
        if hit is not None and hit.get("inliers") is None:
            sift = await self.vision(V.look, self.ref, f[2], False, self._hint())
            hit = {**hit, "inliers": sift["inliers"] if sift else 0}   # 0 only matters once features were counted
        return hit

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
        if not self.catalogue:
            self._predict_into_catalogue(motion.view_names(), gate)
        shots = [s for s in (self._shot_of(p) for p in motion.view_names()) if s]
        preferred = max(shots, key=lambda s: s["score"], default=None)
        w = self.where()
        here = next((s for s in shots if w.get("where") == "view" and w.get("view") == s["preset"] and not s["toward"]), None)
        if here is None and preferred and gate["moves"] and w.get("where") in ("view", "between"):
            self.job["phase"] = "reposition"
            self.step(f"going to the preferred view ({preferred['preset']})")
            await self.go_shot(preferred["preset"], preferred["toward"], preferred["s"], f"{preferred['preset']}/watch")
            here = preferred
        current = here or {"preset": "current framing", "toward": None}
        self.job["phase"] = "occluded"
        looks: deque = deque(maxlen=4)
        arrived, left = time.monotonic(), {}
        base, tried, clear_box = None, False, None
        framed_at, off_count = 0.0, 0
        self.job["result"] = {"preferred": current["preset"], "moves": []}

        def covered_by(hit):
            return not hit or bool(base and hit.get("inliers") is not None and hit["inliers"] < base * V.COVERED_RATIO)

        while True:
            await asyncio.sleep(WATCH_LOOK_S)
            hit = await self._look()
            covered = covered_by(hit)
            looks.append(covered)
            now = time.monotonic()
            label = current["preset"] + (f" ({current['s']:.0%} toward {current['toward']})" if current["toward"] else "")
            ratio = None if not hit or not base or hit.get("inliers") is None else min(1.0, hit["inliers"] / base)
            self.job["progress"].update(preset=current["preset"], visible_share=ratio if ratio is not None else float(not covered))
            if len(looks) >= 3 and not any(list(looks)[-3:]):
                self.job["phase"] = "watch"
                base = max(base or 0, hit.get("inliers") or 0)
                clear_box = hit["box"]
                tried = False
                b = hit["box"]
                off = max(abs((b[0] + b[2]) / 2 - 0.5), abs((b[1] + b[3]) / 2 - 0.5)) > 1.5 * CENTER_DEAD
                edge = min(b[0], b[1], 1 - b[2], 1 - b[3]) < 0.02
                clear = not base or (hit.get("inliers") or 0) >= 0.8 * base
                off_count = off_count + 1 if (off or edge) and clear else 0
                if off_count >= 2 and now - framed_at > WATCH_FRAME_S:
                    framed_at = now           # keep the whole object in the picture, centred
                    if await self._center_on_object():
                        base, clear_box = None, None          # a new pose: count features afresh
                        looks.clear()
                        continue
                    framed_at = now + WATCH_STUCK_S           # could not get closer: try again later
            if len(looks) == 4 and sum(looks) >= 3:
                self.job["phase"] = "occluded"
                self.step(f"subject obscured at {label}. Recording paused.")
                if tried or now - arrived < MIN_DWELL_S:
                    continue
                if not gate["moves"]:
                    continue
                alt = self._alternative(current, shots, left, now) if current["preset"] != "current framing" else None
                path = None
                if alt is None and gate["computed"] and clear_box:
                    tried = True
                    self.step("checking a new angle around the obstruction")
                    joints, _ = await self.settle()
                    path = await self.vision(orbit.occlusion_cut, self.loc["point_m"], joints, self.state.arm.ranges(),
                                             self._aspect(), clear_box, self.calibration.get("shoulder_reach_limit_m"))
                if alt is None and path is None:
                    self.step(f"subject obscured at {label}. Holding for a clear view.")
                    continue
                dest = alt["preset"] if alt else "a new angle"
                self.step(f"subject obscured at {label}. Cutting to {dest} in {ANNOUNCE_S:.0f} s.")
                await asyncio.sleep(ANNOUNCE_S)
                if not covered_by(await self._look()):
                    looks.clear()
                    continue
                self.job["phase"] = "reposition"
                left[current["preset"]] = time.monotonic()
                if path is not None:
                    try:
                        await self.move_path(path, "occlusion cut")
                    except TooHeavy as e:
                        self._lock_at_limit([{m: joints[m] for m in motion.MOVABLE}] + path, "occlusion cut", str(e))
                    joints, stamp = await self.settle()
                    obs = await self.observe("watch/new-angle", joints, stamp)
                else:
                    obs = await self.go_shot(alt["preset"], alt["toward"], alt["s"], f"{alt['preset']}/watch")
                self.job["result"]["moves"].append({"from": current["preset"], "to": dest, "why": "covered",
                                                    "seen": obs["valid"], "t": round(time.time(), 1)})
                current = alt or {"preset": "current framing", "toward": None}
                arrived, base, tried, clear_box = time.monotonic(), None, True, None
                self.job["phase"] = "occluded"
                looks.clear()
                continue
            if self.job["phase"] == "watch":
                self.step(f"holding {label}" + (" (partly obscured)" if covered else ""))
            else:
                self.step(f"waiting for a clear view at {label}")

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
            if best is None or (s["status"] == "VALID", s["score"]) > (best["status"] == "VALID", best["score"]):
                best = s
        return best

    async def _follow(self, req: dict, gate: dict):
        """Follow the object (Keep in frame): from a saved view the camera keeps the tracked object centred (base yaw
        and wrist) and at the distance it had at the start (a short dolly along the line of sight when its size in
        the picture changes), in small checked steps until STOP or `seconds`. When the object is not seen the arm
        holds still: it never searches."""
        if not gate["moves"]:
            raise Hold("physical moves not approved")
        self.job["phase"] = "reposition"
        view = await self.ensure_view()
        if not view or view == motion.TRANSIT:
            raise Hold("follow starts from a saved view")
        vpose = {m: motion.load_views()[view][m] for m in motion.MOVABLE}
        bias = predict.bias_for({"where": "view", "view": view}, self.calibration)
        ranges = self.state.arm.ranges()
        self.trail = [vpose]                  # the way back for the next take (ensure_view retraces it)
        commanded = dict(vpose)
        size0 = depth0 = anchor = offset = None
        far_count = moved_count = 0
        smooth, correcting = None, False
        self._save_trail(self.trail)
        end, corrections, since = time.monotonic() + float(req.get("seconds", 300)), 0, -1
        self.job["phase"] = "cinematic"
        self.step(f"following the object from {view}")
        try:
            while time.monotonic() < end:
                await asyncio.sleep(0.1)
                f, obs = self.frame(), self.obs
                if obs is None or f is None or obs["frame_id"] <= since or f[0] - obs["frame_id"] > 8:
                    continue                  # not seen (or not since the last correction): hold still
                if obs.get("method") == "vit" and obs["confidence"] < V.VIT_MIN:
                    continue                  # a weak box may have slid onto something else: never steer by it
                b = obs["box"]
                raw = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                # low-pass the measured centre (box jitter), but jump straight to a real move
                if smooth is None or max(abs(raw[0] - smooth[0]), abs(raw[1] - smooth[1])) > FOLLOW_JUMP:
                    smooth = raw
                else:
                    smooth = tuple(s + FOLLOW_SMOOTH * (r - s) for s, r in zip(smooth, raw))
                centre = smooth
                off = max(abs(centre[0] - 0.5), abs(centre[1] - 0.5))
                # dead band with hysteresis: start correcting past FOLLOW_START, stop once inside FOLLOW_STOP
                correcting = off > FOLLOW_START or (correcting and off > FOLLOW_STOP)
                size = (b[2] - b[0] + b[3] - b[1]) / 2
                cam = np.array(framing.camera_pose({**commanded, motion.GRIPPER: 1279}, ranges)[0])
                fused = self.obj.x is not None and self.obj.state()["sigma_m"] < 0.08
                dist = float(np.linalg.norm(self.obj.x - cam)) if fused else None
                if size0 is None:             # the distance to keep: as it is now
                    size0 = size
                    depth0 = dist or await self.vision(orbit.follow_depth, {**commanded, motion.GRIPPER: 1279}, ranges,
                                                       centre, bias, self._aspect())
                # how far it is now: the fused estimate (every frame, all camera poses), but only when the object's
                # size in the picture agrees that the distance changed (the model drifts a little off the views)
                ratio = size / size0
                if dist:
                    fused_ratio = depth0 / dist
                    dead = orbit.FOLLOW_DIST_DEAD
                    agree = (ratio > 1 + dead and fused_ratio > 1 + dead) or (ratio < 1 - dead and fused_ratio < 1 - dead)
                    ratio = fused_ratio if agree else 1.0
                off = abs(ratio - 1) > orbit.FOLLOW_DIST_DEAD
                far_count = far_count + 1 if off else 0
                if off and far_count < 3:     # a real move toward/away holds; one odd box size does not
                    ratio = 1.0
                if fused and anchor is None:  # 3-D follow keeps this offset between camera and object
                    anchor, offset = self.obj.x.copy(), cam - self.obj.x
                # moved sideways or up/down: from the ray directions (precise); along the line of sight the size
                # estimate is too rough (and differs between tracker and YOLOE boxes): that is the dolly's job
                moved = False
                if fused and anchor is not None:
                    shift, ray = self.obj.x - anchor, (anchor - cam) / np.linalg.norm(anchor - cam)
                    moved = float(np.linalg.norm(shift - ray * float(np.dot(shift, ray)))) > FOLLOW_MOVE_M
                    moved_count = moved_count + 1 if moved else 0
                    moved = moved_count >= 3          # a real move holds; one odd fused reading does not
                    moved = moved and correcting      # ...and shows in the picture: the object left the centre
                    if moved:
                        self.note(f"follow: object moved {np.round(shift, 3).tolist()} m (sigma {self.obj.state()['sigma_m']})")
                if not moved and not correcting and abs(ratio - 1) <= orbit.FOLLOW_DIST_DEAD:
                    continue                  # centred well enough: hold still, no micro-corrections
                # from the last commanded pose, not the measured one (that sags under load, step after step)
                now = {**commanded, motion.GRIPPER: (await self._joints())[motion.GRIPPER]}
                step = await self.vision(orbit.follow_step, vpose, now, ranges, centre, bias, self._aspect(),
                                         depth0, ratio, *((self.obj.x.tolist(), offset.tolist()) if moved else ()))
                if moved and step["ok"]:
                    anchor = self.obj.x.copy()
                elif moved:                   # cannot move with it from here: at least keep it centred
                    self._note_once(f"follow: cannot move with the object ({step['reason'][:100]}); turning only")
                    step = await self.vision(orbit.follow_step, vpose, now, ranges, centre, bias, self._aspect())
                if not step["ok"]:
                    self._note_once(f"follow: cannot turn further ({step['reason'][:100]})")
                    self.step(f"following (cannot turn further: {step['reason'][:80]})")
                    continue
                await self.move_path([step["pose"]], "follow", speed=FOLLOW_SPEED)
                commanded = dict(step["pose"])
                self.trail.append(step["pose"])
                since = (self.frame() or (obs["frame_id"],))[0]
                corrections += 1
                if corrections % 10 == 0:
                    self._save_trail(self.trail)
                self.step(f"following the object ({corrections} corrections)")
        finally:
            self._save_trail(self.trail)
            self.job["result"] = {"view": view, "corrections": corrections}

    async def _center(self, req: dict, gate: dict):
        """Keep in frame: what a centring correction would be (framing.plan 'center'). Preview only:
        moves to computed poses are not enabled in this system."""
        f = self.frame()
        obs = self.obs
        if obs is None or f is None or f[0] - obs["frame_id"] > 10:
            raise Hold("the target is not being tracked right now")
        joints, ranges = (await self._joints()), self.state.arm.ranges()
        b = obs["box"]
        wh = framing.jpeg_size(f[2]) or (4, 3)
        bbox = {"cx": (b[0] + b[2]) / 2, "cy": (b[1] + b[3]) / 2, "w": b[2] - b[0], "h": b[3] - b[1]}
        try:
            plan = await self.vision(framing.plan, joints, bbox, "center", 0.1, wh[0] / wh[1], ranges)
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
                "object": self.obj.state(),
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
        if method == "GET" and name == "twin":             # the dashboard's 3-D view
            try:
                joints, ranges = st.arm.joints(max_age_s=1.0), st.arm.ranges()
            except RuntimeError as e:
                return 503, {"ok": False, "error": str(e)}, "application/json"
            obs = self.obs
            return 200, {"ok": True, "links": twin.links(joints, ranges), "camera": twin.camera(joints, ranges),
                         "object": self.obj.state(), "target": self.target and self.target["label"],
                         "tracking": obs is not None and self.tracker is not None and not self.tracker.lost(),
                         "job": self.job and {k: self.job.get(k) for k in ("kind", "status", "step")},
                         "moving": bool(st.moving)}, "application/json"
        if method == "GET" and name == "skills":
            return 200, {"ok": True, "skills": [{k: s[k] for k in ("name", "brief", "view", "created")} | {"poses": len(s["poses"])}
                                                 for s in self._skills()]}, "application/json"
        if method == "GET" and name.startswith("mesh/"):
            data = twin.mesh_bytes(name[len("mesh/"):])
            return (200, data, "model/stl") if data else (404, {"ok": False, "error": "no such mesh"}, "application/json")
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
            if name == "skills/delete":
                kept = [s for s in self._skills() if s["name"] != req.get("name")]
                self._skills_path().write_text(json.dumps(kept, indent=1))
                return 200, {"ok": True, "skills": len(kept)}, "application/json"
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
