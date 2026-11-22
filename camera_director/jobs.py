"""Camera director: target selection, per-preset best-view survey, recall and cinematic, inside
capture_server.py (started with --director). Local only: frames come from the capture server's own
Station camera reader, all vision is camera_director/vision.py, Claude is not involved.

Who moves the arm: only State.start_move (the capture server's single command owner), with the
stop generation from when the job started, so a STOP from anywhere refuses every later move of the
job; STOP and any person's command also cancel the job (State.command / State._stop_cleanup).
Every move is to a saved view or to a stop part-way along a checked line between two checked views
(motion.plan_along): no computed poses.

Physical motion needs the operator's approval file director_safety.json (ignored by git). Without
it the real arm only gets a plan and a score of the current view (DRY RUN). In --sim the same jobs
run on the simulated arm with a rendered camera, labelled SIMULATED.
"""
from __future__ import annotations

import asyncio
import json
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

from . import vision as V

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "director_data"            # sticker reference, catalogue, thumbnails (ignored by git)
SAFETY_PATH = ROOT / "director_safety.json"  # the operator's approval for physical moves (ignored by git)
GRIPPER_CAMERA = "usbvideo/74e4d17bc903e40ebc78117539fca31f"   # docs/HARDWARE.md: the follower's gripper camera

TICK_S = 0.2              # frame sampling / live tracking period
FRAME_CACHE = 60          # frames kept for selection by id (~12 s)
SELECT_MAX_AGE_S = 30.0   # a box drawn on a frame older than this is refused
FRAME_FRESH_S = 1.0       # a frame older than this is not used for decisions
SETTLE_S = 0.6            # wait after a move before checking that the joints stand still
SETTLE_TOL = 4            # steps between two reads 0.15 s apart
OBSERVE_FRAMES = 5        # frames scored per view after settling
FRACTIONS = (0.12, 0.25)  # stops along each checked line from a preset (<= motion.ALONG_MAX)
MIN_TRAVEL = 100          # a stop moving no joint this far is too close to the preset to be a new view
CANDIDATES = 6
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
        self.target: dict | None = None
        self.ref: V.Reference | None = None
        self.tracker: V.Tracker | None = None
        self.obs: dict | None = None
        self.job: dict | None = None
        self.job_task: asyncio.Task | None = None
        self.job_gen = 0
        self._own_move = False
        self.catalogue: dict[str, dict] = {}
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
        if not frames:
            return None
        if frame_id is None:
            return frames[-1]
        return next((f for f in frames if f[0] == frame_id), None)

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
                "gripper_camera": self.state.sim or queue == GRIPPER_CAMERA}

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

    def candidates(self, preset: str, gate: dict, limit: int = CANDIDATES) -> list[dict]:
        """Stops along checked lines from `preset`; in PHYSICAL mode only the approved lines and fractions."""
        views, out = motion.load_views(), []
        for other in motion.view_names():
            if other == preset or not motion.view_checked(other):
                continue
            kind, why = motion.route_kind(preset, other)
            if kind not in ("direct", "arc"):
                continue
            line = "-".join(sorted((preset, other)))
            if gate["lines"] is not None and line not in gate["lines"]:
                continue
            for s in FRACTIONS:
                if s > gate["max_fraction"] + 1e-9:
                    continue
                travel = max(abs(views[other][m] - views[preset][m]) * s for m in motion.MOVABLE)
                if travel >= MIN_TRAVEL:
                    out.append({"toward": other, "s": s, "route": kind, "line": line, "travel_steps": round(travel)})
        return out[:limit]

    # ------------------------------------------------------------ target

    def _set_target(self, ref: V.Reference, frame_id: int, how: str):
        self.ref, self.tracker, self.obs = ref, V.Tracker(ref), None
        self.target = {"id": uuid.uuid4().hex[:8], "label": ref.label, "kind": ref.kind, "frame_id": frame_id,
                       "how": how, "set_at": round(time.time(), 2)}
        self.catalogue.clear()   # results for another target say nothing about this one
        self.thumbs.clear()
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

    async def move(self, view: str, along: tuple[str, float] | None = None) -> bool:
        """One move through the capture server's command owner. True when it arrived, False when the
        motion layer refused before commanding anything; Hold when a move failed part-way."""
        self.job["moving_to"] = view if not along else f"{view} → {along[0]} {along[1]:.0%}"
        self._own_move = True
        try:
            try:
                await self.state.start_move(view, MOVE_SPEED, gen=self.job_gen, along=along)
            except asyncio.CancelledError:
                raise
            except Exception as e:     # refused by the command owner (STOP since the job began, busy, no data)
                raise Hold(f"move refused: {e}")
            task = self.state.move_task
            await asyncio.shield(task)
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

    def where(self) -> dict:
        w = (self.state.where or {}).get("where") or {}
        return w

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
                   "tour": self._tour, "center": self._center}
        if kind not in runners:
            return 400, {"ok": False, "error": f"kind must be one of {sorted(runners)}"}
        if self.busy():
            return 409, {"ok": False, "error": f"a {self.job['kind']} job is already running"}
        if self.state.stopping:
            return 409, {"ok": False, "error": "STOP is still being handled"}
        if kind != "tour" and self.ref is None:
            return 409, {"ok": False, "error": "no target: use Auto-find sticker or Select object first"}
        gate = self.gate()
        self.job_gen = self.state.gen
        self.job = {"id": uuid.uuid4().hex[:8], "kind": kind, "mode": gate["mode"], "status": "RUNNING",
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
            self.job["ended_at"] = round(time.time(), 2)
            self.job["step"] = self.job["status"].lower()
            self.note(f"{self.job['kind']} job {self.job['status']}" + (f": {self.job['reason']}" if self.job["reason"] else ""))
            self._save()

    def step(self, text: str, **progress):
        self.job["step"] = text
        self.job["progress"].update(progress)

    async def _survey(self, req: dict, gate: dict):
        presets = req.get("presets") or motion.view_names()
        known = motion.view_names()
        bad = [p for p in presets if p not in known]
        if bad or not presets:
            raise Hold(f"unknown presets {bad}; known: {known}")
        for i, p in enumerate(presets, 1):
            self.step(f"preset {p}", preset=p, preset_index=i, preset_count=len(presets), candidate_index=0, candidate_count=0)
            self.catalogue[p] = await self._survey_preset(p, gate)
        valid = [r for r in self.catalogue.values() if r["status"] == "VALID"]
        self.job["result"] = {"valid": len(valid), "presets": len(presets),
                              "best": max(valid, key=lambda r: r["best"]["score"])["preset"] if valid else None}
        if gate["moves"] and self.job["result"]["best"]:
            self.step(f"returning to the best view ({self.job['result']['best']})")
            await self._go_best(self.catalogue[self.job["result"]["best"]])

    async def _survey_preset(self, p: str, gate: dict) -> dict:
        base = {"preset": p, "target_id": self.target["id"], "target": self.target["label"],
                "camera": self.camera_state()["queue"], "mode": gate["mode"], "baseline": None, "best": None,
                "candidates": [], "reason": None, "surveyed_at": round(time.time(), 2)}
        if not motion.view_checked(p):
            return {**base, "status": "BLOCKED", "reason": "not checked on the real arm, so it has no approved route"}
        cands = self.candidates(p, gate)
        if not gate["moves"]:
            out = {**base, "status": "DRY_RUN", "plan": cands,
                   "reason": "physical moves not approved: plan only" + ("" if cands else " (no checked lines from here)")}
            w = self.where()
            if w.get("where") == "view" and w.get("view") == p:
                joints, t = await self.settle()
                out["baseline"] = await self.observe(f"{p}/baseline", joints, t)
            return out
        if not await self.move(p):
            return {**base, "status": "BLOCKED", "reason": "the motion layer refused the route to this preset"}
        joints, t = await self.settle()
        b = await self.observe(f"{p}/baseline", joints, t)
        b.update(toward=None, s=0.0, label="preset")
        base["baseline"] = b
        if not b["valid"]:
            return {**base, "status": "SKIPPED", "reason": f"target not visible ({b['reason']})"}
        tried, line = [b], None
        for k, c in enumerate(cands, 1):
            self.step(f"preset {p}: candidate {k} of {len(cands)}", candidate_index=k, candidate_count=len(cands))
            if line not in (None, c["toward"]):
                await self.move(p)                     # back along the last line to the preset
            line = c["toward"]
            if not await self.move(p, (c["toward"], c["s"])):
                base["candidates"].append({**c, "valid": False, "reason": "refused by the motion layer"})
                line = None if self.where().get("where") == "view" else line
                continue
            joints, t = await self.settle()
            obs = await self.observe(f"{p}/c{k}", joints, t)
            obs.update(c, label=f"{c['s']:.0%} toward {c['toward']}")
            base["candidates"].append(obs)
            tried.append(obs)
        best = max((o for o in tried if o["valid"]), key=lambda o: o["score"])
        self.step(f"preset {p}: rechecking the best view")
        result = {**base, "status": "VALID", "best": dict(best)}
        recheck = await self._go_best(result)
        result["best"]["recheck_score"] = recheck["score"]
        result["best"]["measured_joints"] = recheck["measured_joints"]
        if not recheck["valid"] or recheck["score"] < RECHECK_RATIO * best["score"]:
            result["status"] = "UNVERIFIED"
            result["reason"] = f"a fresh look scored {recheck['score']} against {best['score']}"
        return result

    async def _go_best(self, result: dict) -> dict:
        """Move to a result's best view along approved routes and look again."""
        p, best = result["preset"], result["best"]
        w = self.where()
        on_line = w.get("where") == "between" and {w.get("view"), w.get("to")} == {p, best.get("toward")}
        if not (best.get("toward") and on_line):
            if not (w.get("where") == "view" and w.get("view") == p):
                if not await self.move(p):
                    raise Hold(f"the route back to {p} was refused")
        if best.get("toward"):
            if not await self.move(p, (best["toward"], best["s"])):
                raise Hold(f"the stop {best['s']:.0%} toward {best['toward']} was refused")
        joints, t = await self.settle()
        return await self.observe(f"{p}/best", joints, t)

    def _shots(self, req: dict) -> list[dict]:
        order = req.get("order") or motion.view_names()
        return [self.catalogue[p] for p in order if self.catalogue.get(p, {}).get("status") == "VALID"]

    async def _recall(self, req: dict, gate: dict):
        r = self.catalogue.get(str(req.get("preset")))
        if not r or r["status"] not in ("VALID", "UNVERIFIED"):
            raise Hold("no saved best view for that preset; run Optimize all views first")
        if r["target_id"] != self.target["id"]:
            raise Hold("that view was found for another target; survey again")
        if not gate["moves"]:
            self.job.update(status="BLOCKED", reason="physical moves not approved", result={"preset": r["preset"]})
            return
        self.step(f"recalling {r['preset']}", preset=r["preset"])
        look = await self._go_best(r)
        ok = look["valid"] and look["score"] >= RECHECK_RATIO * r["best"]["score"]
        self.job["result"] = {"preset": r["preset"], "saved_score": r["best"]["score"], "score_now": look["score"],
                              "verified": ok, "measured_joints": look["measured_joints"]}
        if not ok:
            raise Hold(f"recalled view does not match: score now {look['score']} against {r['best']['score']}")

    async def _cinematic(self, req: dict, gate: dict):
        shots = self._shots(req)
        board = [{"preset": s["preset"], "toward": s["best"].get("toward"), "s": s["best"].get("s"),
                  "score": s["best"]["score"], "thumb": s["best"]["thumb"]} for s in shots]
        self.job["result"] = {"storyboard": board, "shots": []}
        if not shots:
            raise Hold("no verified best shots yet: run Optimize all views first")
        if not gate["moves"]:
            self.job.update(status="BLOCKED", reason="physical moves not approved: storyboard only (no moving take)")
            return
        for i, s in enumerate(shots, 1):
            self.step(f"shot {i} of {len(shots)}: {s['preset']}", shot_index=i, shot_count=len(shots), preset=s["preset"])
            look = await self._go_best(s)
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
            self.step("auto-finding the sticker")
            status, body = await self.autofind()
            if status != 200:
                raise Hold(body.get("detail") or body.get("error"))
        self.step("optimizing all views")
        await self._survey(req, gate)
        survey = self.job["result"]
        if gate["moves"] and survey["valid"]:
            await self._cinematic(req, gate)
            self.job["result"] = {**self.job["result"], "survey": survey}
        else:
            self.job["result"] = {"survey": survey, "storyboard": [], "shots": []}
            if not gate["moves"]:
                self.job.update(status="DRY_RUN", reason="physical moves not approved: plan and current view only")

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
        return {"ok": True, "gate": self.gate(), "camera": cam, "target": target,
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
            return (200, f[2], "image/jpeg") if f else (404, {"ok": False, "error": "no such frame"}, "")
        if method == "GET" and name.startswith("thumb/"):
            jpeg = self.thumbs.get(name[len("thumb/"):].removesuffix(".jpg"))
            return (200, jpeg, "image/jpeg") if jpeg else (404, {"ok": False, "error": "no such thumbnail"}, "")
        if method == "POST":
            handlers = {"select": lambda: self.select(req), "autofind": self.autofind,
                        "clear": lambda: self._on_loop(self.clear), "job": lambda: self._on_loop(lambda: self.start(req))}
            if name in handlers:
                status, body = st.call(handlers[name](), timeout=20)
                return status, body, "application/json"
        return 404, {"ok": False, "error": f"unknown director call {method} {name}"}, ""

    @staticmethod
    async def _on_loop(fn):
        return fn()
