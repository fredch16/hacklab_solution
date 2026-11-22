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


# ------------------------------------------------------------------ vision (synthetic images only)

import cv2
import numpy as np
from camera_director import vision as V


def tag_image(text=("HACK", "LAB"), angle=0.0, scale=1.0, at=(320, 240), seed=1):
    rng = np.random.default_rng(seed)
    img = np.clip(140 + rng.normal(0, 12, (480, 640)), 0, 255).astype(np.uint8)
    for _ in range(25):
        cv2.line(img, tuple(int(v) for v in rng.integers(0, 640, 2)), tuple(int(v) for v in rng.integers(0, 480, 2)), 30, 3)
    side = 120
    tag = np.full((side, side), 245, np.uint8)
    cv2.putText(tag, text[0], (12, 52), cv2.FONT_HERSHEY_DUPLEX, 1.2, 40, 2)
    cv2.putText(tag, text[1], (28, 98), cv2.FONT_HERSHEY_DUPLEX, 1.2, 40, 2)
    m = cv2.getRotationMatrix2D((side / 2, side / 2), angle, scale)
    m[:, 2] += (at[0] - side / 2, at[1] - side / 2)
    mask = cv2.warpAffine(np.full_like(tag, 255), m, (640, 480))
    warped = cv2.warpAffine(tag, m, (640, 480))
    img[mask > 0] = warped[mask > 0]
    return img


REF_BOX = [(320 - 60) / 640, (240 - 60) / 480, (320 + 60) / 640, (240 + 60) / 480]


class Vision(unittest.TestCase):
    def setUp(self):
        self.ref = V.Reference.from_frame(tag_image(), REF_BOX, "tag", "manual")

    def test_finds_the_reference_moved_rotated_and_scaled(self):
        hit = V.detect(self.ref, tag_image(angle=30, scale=1.4, at=(420, 200), seed=2))
        self.assertIsNotNone(hit)
        cx, cy = (hit["box"][0] + hit["box"][2]) / 2, (hit["box"][1] + hit["box"][3]) / 2
        self.assertAlmostEqual(cx, 420 / 640, delta=0.02)
        self.assertAlmostEqual(cy, 200 / 480, delta=0.02)
        self.assertEqual(hit["method"], "features")

    def test_rejects_absent_target_and_a_similar_looking_label(self):
        self.assertIsNone(V.detect(self.ref, tag_image(text=("", ""), seed=3)))
        self.assertIsNone(V.detect(self.ref, tag_image(text=("MAKE", "BAR"), seed=4)))

    def test_flat_crop_matches_nothing(self):
        flat = V.Reference(np.full((40, 40), 200, np.uint8), "flat", "manual")
        self.assertIsNone(V.detect(flat, tag_image(seed=5)))

    def test_score_needs_the_target_in_enough_settled_frames(self):
        g = tag_image()
        hit = V.detect(self.ref, g)
        bad = V.score_view([hit, None, None, None, hit], [g] * 5)
        self.assertFalse(bad["valid"])
        self.assertIsNone(bad["score"])
        good = V.score_view([hit] * 5, [g] * 5)
        self.assertTrue(good["valid"])
        self.assertTrue(0 < good["score"] <= 1)
        off = V.score_view([V.detect(self.ref, tag_image(at=(560, 400)))] * 5, [tag_image(at=(560, 400))] * 5)
        self.assertLess(off["score"], good["score"])      # off-centre, near the edge: worse

    def test_tracker_reports_lost_after_misses(self):
        tr = V.Tracker(self.ref)
        self.assertEqual(tr.status(), "SEARCHING")
        self.assertIsNotNone(tr.update(tag_image(), 1, 0.0))
        self.assertEqual(tr.status(), "TRACKING")
        blank = tag_image(text=("", ""))
        for i in range(V.LOST_AFTER):
            self.assertIsNone(tr.update(blank, 2 + i, 0.0))
        self.assertEqual(tr.status(), "TARGET_LOST")


# ------------------------------------------------------------------ director on the simulated arm

import tempfile
import camera_director.jobs as J
import framing
from camera_director import predict


def white_box(jpeg: bytes):
    g = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    _, m = cv2.threshold(g, 235, 255, cv2.THRESH_BINARY)
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    x, y, w, h = cv2.boundingRect(max(cs, key=cv2.contourArea))
    return [x / g.shape[1], y / g.shape[0], (x + w) / g.shape[1], (y + h) / g.shape[0]]


class SimDirector(unittest.TestCase):
    """One server for the class; tests run in name order and share the arm."""

    @classmethod
    def setUpClass(cls):
        cls.state, cls.httpd, cls.base = start_server(director=True)
        cls.state.director.data = Path(tempfile.mkdtemp())
        wait(lambda: http(cls.base, "/api/director/state")[1]["camera"]["frame_id"], what="frames")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self):
        return http(self.base, "/api/director/state")[1]

    def goto(self, view):
        gen = http(self.base, "/api/state")[1]["generation"]
        status, body = http(self.base, "/api/goto", {"view": view, "generation": gen})
        self.assertEqual(status, 200, body)
        wait(lambda: not self.state.moving, what="move")
        self.assertTrue(self.state.move_outcome["ok"], self.state.move_outcome)
        time.sleep(1.2)

    def run_job(self, body, timeout=240):
        status, out = http(self.base, "/api/director/job", body)
        self.assertEqual(status, 200, out)
        wait(lambda: self.get()["job"]["status"] != "RUNNING", timeout=timeout, what=f"{body} job")
        return self.get()

    def test_1_selection_validates_box_and_frame(self):
        st = self.get()
        self.assertEqual(st["gate"]["mode"], "SIMULATED")
        self.assertTrue(st["camera"]["live"])
        self.assertEqual(http(self.base, "/api/director/autofind", {})[1]["error"], "no_reference")
        status, body = http(self.base, "/api/director/job", {"kind": "survey"})
        self.assertEqual(status, 409)                                     # no target yet
        fid = st["camera"]["frame_id"]
        for box, code in [([0.5, 0.5, 0.4, 0.6], 400), ([0, 0, 1.2, 1], 400), ("x", 400), ([0.5, 0.5, 0.505, 0.505], 400)]:
            self.assertEqual(http(self.base, "/api/director/select", {"frame_id": fid, "box": box})[0], code, box)
        self.assertEqual(http(self.base, "/api/director/select", {"frame_id": -5, "box": [0.1, 0.1, 0.5, 0.5]})[0], 409)
        self.assertEqual(http(self.base, "/api/director/frame.jpg?id=-5")[0], 404)
        frozen = self.get()["camera"]["frame_id"]
        self.assertEqual(http(self.base, f"/api/director/frame.jpg?id={frozen}")[0], 200)   # the dashboard freezes it
        wait(lambda: self.get()["camera"]["frame_id"] > frozen + J.FRAME_CACHE + 5, timeout=30, what="cache turnover")
        status, body = http(self.base, "/api/director/select", {"frame_id": frozen, "box": [0.3, 0.3, 0.7, 0.7]})
        self.assertNotEqual(status, 409, body)          # drawing slowly must not lose the frozen frame

    def test_2_sticker_crop_then_autofind_and_tracking(self):
        self.goto("top")
        fid = self.get()["camera"]["frame_id"]
        jpeg = http(self.base, f"/api/director/frame.jpg?id={fid}")[1]
        status, body = http(self.base, "/api/director/select", {"frame_id": fid, "box": white_box(jpeg), "as_sticker": True})
        self.assertEqual(status, 200, body)
        wait(lambda: (self.get()["target"] or {}).get("status") == "TRACKING", timeout=10, what="tracking")
        self.assertTrue(self.get()["sticker_reference"])
        self.assertEqual(http(self.base, "/api/director/clear", {})[0], 200)
        status, body = http(self.base, "/api/director/autofind", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.get()["target"]["kind"], "sticker")
        self.assertGreater(self.get()["target"]["observation"]["confidence"], 0.5)

    def test_3_autofind_where_the_sticker_is_not_visible(self):
        self.goto("left")
        status, body = http(self.base, "/api/director/autofind", {})
        self.assertEqual((status, body["error"]), (404, "not_found"))
        wait(lambda: self.get()["target"]["status"] == "TARGET_LOST", timeout=10, what="target lost")

    def test_4_survey_estimates_then_moves_only_to_verify(self):
        self.goto("top")
        moves_before = len(self.state.history)
        st = self.run_job({"kind": "survey"})
        job, cat = st["job"], {p["preset"]: p for p in st["catalogue"]}
        self.assertEqual(job["status"], "DONE", job)
        self.assertEqual(set(cat), set(motion.view_names()))
        loc = st["localization"]
        self.assertTrue(loc["ok"] and loc["views"] >= 2 and "warning" not in loc, loc)   # the sim matches its own model
        self.assertLess(abs(loc["check"]["triangulated_height_m"]), 0.01, loc)
        for p in ("close", "side_low"):
            self.assertEqual(cat[p]["status"], "BLOCKED")
        valid = [p for p in cat.values() if p["status"] == "VALID"]
        self.assertGreaterEqual(len(valid), 2)
        self.assertTrue(all(p["status"] in ("VALID", "BLOCKED", "SKIPPED", "PREDICTED", "NOT_CONFIRMED") for p in cat.values()))
        for p in cat.values():
            if p["status"] == "PREDICTED":                     # predicted but not visited: no image, no measured pose
                self.assertIsNone(p["best"])
                self.assertIn("not worth a move", p["reason"])
        for p in valid:
            b = p["best"]
            self.assertEqual(set(b["measured_joints"]), {str(m) for m in range(1, 9)})
            self.assertEqual(http(self.base, f"/api/director/thumb/{b['thumb']}.jpg")[0], 200)
            self.assertIsNotNone(b["predicted_score"])
            self.assertLessEqual(b["s"], motion.ALONG_MAX)
        # a director, not a search: at most a preset move and one stop per verified or refuted preset, plus the final hold
        visited = len(valid) + sum(p["status"] == "NOT_CONFIRMED" for p in cat.values())
        self.assertLessEqual(len(self.state.history) - moves_before, 2 * visited + 2, list(self.state.history)[moves_before:])

    def test_5_recall_rechecks_the_saved_view(self):
        best = self.get()["job"]["result"]["best"]
        self.goto("wide" if best != "wide" else "top")
        st = self.run_job({"kind": "recall", "preset": best})
        self.assertEqual(st["job"]["status"], "DONE", st["job"])
        self.assertTrue(st["job"]["result"]["verified"])

    def test_6_cinematic_visits_the_verified_shots(self):
        st = self.run_job({"kind": "cinematic"})
        self.assertEqual(st["job"]["status"], "DONE", st["job"])
        shots = st["job"]["result"]["shots"]
        self.assertEqual([s["preset"] for s in shots], [s["preset"] for s in st["job"]["result"]["storyboard"]])
        self.assertGreaterEqual(len(shots), 2)

    def test_7_stop_preempts_a_running_survey_and_later_moves_are_refused(self):
        status, _ = http(self.base, "/api/director/job", {"kind": "survey"})
        self.assertEqual(status, 200)
        wait(lambda: self.state.moving, timeout=20, what="a director move")
        status, body = http(self.base, "/api/stop", {})
        self.assertEqual(status, 200, body)
        self.assertIn(body["hold"], ("confirmed", "limp"))
        job = self.get()["job"]
        self.assertEqual((job["status"], job["reason"]), ("STOPPED", "STOP pressed"))
        time.sleep(1.0)
        self.assertFalse(self.state.moving)

    def test_8_manual_command_takes_over_from_the_director(self):
        self.goto("top")
        status, _ = http(self.base, "/api/director/job", {"kind": "survey", "presets": ["top", "wide"]})
        self.assertEqual(status, 200)
        wait(lambda: self.state.moving, timeout=20, what="a director move")
        gen = http(self.base, "/api/state")[1]["generation"]
        http(self.base, "/api/goto", {"view": "wide", "generation": gen})   # a person clicks a view
        wait(lambda: self.get()["job"]["status"] != "RUNNING", timeout=10, what="job end")
        self.assertEqual(self.get()["job"]["reason"], "manual control took over")
        wait(lambda: not self.state.moving and not self.state.stopping, timeout=10, what="arm idle")

    def test_9a_keep_in_view_cuts_away_from_a_hand_and_pulls_back(self):
        saved = (J.MIN_DWELL_S, J.NO_RETURN_S, J.PULL_BACK_S, J.ANNOUNCE_S)
        J.MIN_DWELL_S, J.NO_RETURN_S, J.PULL_BACK_S, J.ANNOUNCE_S = 3.0, 8.0, 6.0, 0.3
        try:
            self.assertEqual(http(self.base, "/api/director/job", {"kind": "watch"})[0], 200)
            wait(lambda: self.get()["job"]["phase"] == "watch" and "holding" in self.get()["job"]["step"], timeout=60, what="watching")
            preferred = self.get()["job"]["result"]["preferred"]
            time.sleep(J.MIN_DWELL_S)
            http(self.base, "/api/director/sim/hand", {"on": True, "dy": 0.04, "z": 0.10})
            wait(lambda: self.get()["job"]["result"]["moves"], timeout=40, what="a cut away from the hand")
            cut = self.get()["job"]["result"]["moves"][0]
            self.assertEqual((cut["from"], cut["why"], cut["seen"]), (preferred, "covered", True), cut)
            self.assertNotEqual(cut["to"], preferred)
            http(self.base, "/api/director/sim/hand", {"on": False})
            wait(lambda: len(self.get()["job"]["result"]["moves"]) >= 2, timeout=40, what="pull back")
            back = self.get()["job"]["result"]["moves"][1]
            self.assertEqual((back["to"], back["why"], back["seen"]), (preferred, "pull back", True), back)
            self.assertEqual(http(self.base, "/api/stop", {})[0], 200)
            self.assertEqual(self.get()["job"]["status"], "STOPPED")
        finally:
            J.MIN_DWELL_S, J.NO_RETURN_S, J.PULL_BACK_S, J.ANNOUNCE_S = saved
            http(self.base, "/api/director/sim/hand", {"on": False})

    def test_9b_calibration_tour_records_near_zero_corrections_in_the_sim(self):
        x, y = self.state.director.sim_camera.label_at
        st = self.run_job({"kind": "calibrate", "point_m": [x, y], "presets": ["top", "wide"]})
        self.assertEqual(st["job"]["status"], "DONE", st["job"])
        res = st["job"]["result"]["presets"]
        for p in ("top", "wide"):
            self.assertIsInstance(res[p], list, res)
            # the sim camera is the model, so only the box-centre-vs-projected-centre perspective offset remains
            self.assertLess(max(abs(v) for v in res[p]), 0.08, res)
        self.assertEqual(st["calibration"]["presets"], ["top", "wide"])

    def test_9c_scene_glides_around_the_object_and_returns(self):
        self.goto("top")
        http(self.base, "/api/director/autofind", {})
        moves = [{"name": "arc", "keyframes": [{"theta": -60, "elev": 50, "dist": 0.18},
                                               {"theta": -20, "elev": 55, "dist": 0.18}]}]
        st = self.run_job({"kind": "scene", "moves": moves}, timeout=300)
        self.assertEqual(st["job"]["status"], "DONE", st["job"])
        done = st["job"]["result"]["moves"][0]
        self.assertTrue(done["done"], done)
        # computer vision looked during the move (a lower bound: SIFT can miss a strongly rotated frame)
        self.assertIsNotNone(done["in_frame"], done)
        wait(lambda: self.get()["arm"]["where"] == {"where": "view", "view": "top"}, timeout=10, what="back at top")

    def test_9d_stop_during_a_computed_path_holds(self):
        moves = [{"name": "arc", "keyframes": [{"theta": -60, "elev": 50, "dist": 0.18},
                                               {"theta": 0, "elev": 60, "dist": 0.17}]}]
        self.assertEqual(http(self.base, "/api/director/job", {"kind": "scene", "moves": moves})[0], 200)
        wait(lambda: self.state.moving, timeout=60, what="a scene move")
        status, body = http(self.base, "/api/stop", {})
        self.assertEqual((status, body["hold"]), (200, "confirmed"), body)
        self.assertEqual(self.get()["job"]["status"], "STOPPED")
        st = self.run_job({"kind": "return"}, timeout=120)              # retrace back to the start
        self.assertEqual(st["job"]["status"], "DONE", st["job"])
        wait(lambda: self.get()["arm"]["where"] == {"where": "view", "view": "top"}, timeout=10, what="back at top")

    def test_9e_shoulder_load_guard_backs_off_instead_of_faulting(self):
        self.goto("top")
        saved = dict(motion.PATH_TRIP_GUARD)
        motion.PATH_TRIP_GUARD.clear()
        motion.PATH_TRIP_GUARD[2] = (-1, 0.1)             # any load counts: the guard must fire on the first move
        try:
            moves = [{"name": "arc", "keyframes": [{"theta": -60, "elev": 50, "dist": 0.18},
                                                   {"theta": -20, "elev": 55, "dist": 0.18}]}]
            st = self.run_job({"kind": "scene", "moves": moves}, timeout=300)
        finally:
            motion.PATH_TRIP_GUARD.clear()
            motion.PATH_TRIP_GUARD.update(saved)
        self.assertEqual(st["job"]["status"], "DONE", st["job"])
        mv = st["job"]["result"]["moves"][0]
        self.assertFalse(mv["done"])
        self.assertIn("too heavy for the shoulder", mv["why"])
        wait(lambda: self.get()["arm"]["where"] == {"where": "view", "view": "top"}, timeout=10, what="back at top")
        self.assertEqual(http(self.base, "/api/state")[1]["blockers"], [])

    def test_9_keep_in_frame_is_a_preview_and_never_moves(self):
        self.goto("top")
        http(self.base, "/api/director/autofind", {})
        before = self.state.arm.joints()
        st = self.run_job({"kind": "center"}, timeout=30)
        self.assertEqual(st["job"]["status"], "DRY_RUN", st["job"])
        self.assertIn("center_error", st["job"]["result"])
        after = self.state.arm.joints()
        self.assertLessEqual(max(abs(after[m] - before[m]) for m in motion.MOVABLE), 5)


class RealArmGate(unittest.TestCase):
    """The gate on a real (non-sim) arm: no approval file -> DRY RUN, and an approval must match."""

    def director(self, approval):
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        arm = type("Arm", (), {"bus_serial": "5B79016329"})()
        state = type("S", (), {"sim": False, "loop": loop, "arm": arm, "cam_queue": J.GRIPPER_CAMERA, "gen": 0, "cams": {}})()
        path = Path(tempfile.mkdtemp()) / "director_safety.json"
        if approval is not None:
            path.write_text(json.dumps(approval))
        return J.Director(state, data_dir=Path(tempfile.mkdtemp()), safety_path=path)

    def test_no_approval_means_dry_run(self):
        g = self.director(None).gate()
        self.assertEqual((g["mode"], g["moves"]), ("DRY RUN", False))
        self.assertIn("director_safety.json", g["reasons"][0])

    def test_approval_must_name_this_camera_and_arm(self):
        ok = {"physical_motion": True, "approved_by": "owner", "approved_at": "2026-11-23", "bus": "5B79016329",
              "camera_queue": J.GRIPPER_CAMERA, "lines": ["top-wide"], "max_fraction": 0.12}
        g = self.director(ok).gate()
        self.assertEqual((g["mode"], g["moves"], g["lines"], g["max_fraction"]), ("PHYSICAL", True, ["top-wide"], 0.12))
        self.assertFalse(g["computed"])                                    # computed poses need their own opt-in
        self.assertTrue(self.director({**ok, "computed_paths": True}).gate()["computed"])
        for change in ({"camera_queue": "usbvideo/other"}, {"bus": "X"}, {"physical_motion": False}, {"approved_by": ""}):
            self.assertFalse(self.director({**ok, **change}).gate()["moves"], change)

    def test_physical_candidates_only_on_approved_lines(self):
        ranges = {int(k): tuple(r) for k, r in motion._read_views_file()["transit"]["ranges"].items()}
        cands = predict.candidate_poses(ranges, 1279, {"lines": ["top-wide"], "max_fraction": 0.12})
        self.assertNotIn("close", cands)
        stops = [c for cs in cands.values() for c in cs if c["toward"]]
        self.assertTrue(stops and all(c["line"] == "top-wide" and c["s"] <= 0.12 for c in stops), stops)

    def test_pullback_only_near_the_destination_on_a_checked_line(self):
        d = self.director(None)
        views = motion.load_views()
        gate = {"lines": None, "max_fraction": motion.ALONG_MAX}
        at = lambda s: {m: round(views["wide"][m] + (views["top"][m] - views["wide"][m]) * s) for m in views["top"]}
        d.samples = [{"joints": at(0.5), "score": 0.6}, {"joints": at(0.9), "score": 0.8}]
        pb = d.pullback("top", "wide", gate, than=0.5)
        self.assertEqual((pb["toward"], round(pb["s"], 2)), ("wide", 0.1))
        self.assertIsNone(d.pullback("top", "wide", gate, than=0.78))           # not better enough
        d.samples = [{"joints": at(0.3), "score": 0.9}]
        self.assertIsNone(d.pullback("top", "wide", gate, than=0.2))            # too far back along the line
        self.assertIsNone(d.pullback("top", "home", gate, than=None))           # home has no direct line
        self.assertIsNone(d.pullback("top", "wide", {"lines": ["left-top"], "max_fraction": 0.35}, than=None))


class ComputedPaths(unittest.TestCase):
    ranges = {int(k): tuple(r) for k, r in motion._read_views_file()["transit"]["ranges"].items()}

    def test_path_check_refuses_the_desk_jumps_and_ranges(self):
        env = motion.envelope(self.ranges)
        top = {m: motion.load_views()["top"][m] for m in motion.MOVABLE}
        self.assertIsNone(motion.path_problem([top, {**top, 1: top[1] + 100}], self.ranges, env, 1279))
        self.assertIn("jump", motion.path_problem([top, {**top, 1: top[1] + 600}], self.ranges, env, 1279))
        self.assertIn("envelope", motion.path_problem([top, {**top, 6: env[6][1] + 50}], self.ranges, env, 1279))
        low = {**top, 2: top[2] + 380, 3: top[3] - 250}                   # shoulder down: towards the desk
        steps = [top]
        while max(abs(low[m] - steps[-1][m]) for m in motion.MOVABLE) > 0:
            steps.append({m: steps[-1][m] + max(-50, min(50, low[m] - steps[-1][m])) for m in motion.MOVABLE})
        why = motion.path_problem(steps, self.ranges, env, 1279)
        self.assertTrue(why and ("desk" in why or "itself" in why or "envelope" in why), why)

    def test_path_curve_never_overshoots_its_waypoints(self):
        import random
        rnd = random.Random(1)
        for _ in range(100):
            path = [{m: rnd.randint(1000, 3000) for m in motion.MOVABLE} for _ in range(rnd.randint(2, 8))]
            f, _total = motion.path_curve(path)
            cum = [0.0]
            for a, b in zip(path, path[1:]):
                cum.append(cum[-1] + max(abs(b[m] - a[m]) for m in motion.MOVABLE))
            for j in range(201):
                x = j / 200 * cum[-1]
                i = max(0, min(len(path) - 2, next((q for q in range(len(cum) - 1) if cum[q + 1] >= x), len(path) - 2)))
                q = f(j / 200)
                for m in motion.MOVABLE:
                    lo, hi = sorted((path[i][m], path[i + 1][m]))
                    self.assertTrue(lo - 1e-6 <= q[m] <= hi + 1e-6)

    def test_arc_poses_look_at_the_object(self):
        from camera_director import orbit
        top = motion.load_views()["top"]
        arc = orbit.plan_path([0.0, 0.31, 0.0], top, self.ranges, [{"theta": -60, "elev": 50, "dist": 0.18},
                                                                     {"theta": -30, "elev": 50, "dist": 0.18}])
        self.assertTrue(arc["ok"], arc)
        for q in arc["poses"]:
            uv = framing.project((0.0, 0.31, 0.0), framing.camera_pose(q, self.ranges), 16 / 9)
            self.assertLess(abs(uv[0] - 0.5) + abs(uv[1] - 0.5), 0.1, uv)
        self.assertIsNone(motion.path_problem(arc["poses"], self.ranges, motion.envelope(self.ranges), 1279))


class Prediction(unittest.TestCase):
    ranges = {int(k): tuple(r) for k, r in motion._read_views_file()["transit"]["ranges"].items()}
    target = {"point_m": [-0.03, 0.35, 0.0], "size_m": 0.08}

    def sighting(self, view):
        joints = motion.load_views()[view]
        return joints, predict.predict_view(joints, self.target, self.ranges, 4 / 3)["box"]

    def test_combines_views_on_the_desk_and_checks_them_against_each_other(self):
        two = predict.localize([self.sighting("top"), self.sighting("wide")], self.ranges, 4 / 3)
        self.assertEqual(two["method"], "desk plane, 2 views weighted by viewing angle")
        self.assertLess(np.linalg.norm(np.array(two["point_m"]) - self.target["point_m"]), 0.005)
        self.assertLess(abs(two["check"]["triangulated_height_m"]), 0.005)
        self.assertNotIn("warning", two)
        # a view whose camera model is off (box shifted as by a tilt error) is flagged, and the vertical view wins
        joints, box = self.sighting("wide")
        bad = (joints, [box[0], box[1] - 0.15, box[2], box[3] - 0.15])
        off = predict.localize([self.sighting("top"), bad], self.ranges, 4 / 3)
        self.assertIn("disagree", off["warning"])
        self.assertLess(np.linalg.norm(np.array(off["point_m"]) - self.target["point_m"]), 0.03)
        one = predict.localize([self.sighting("top")], self.ranges, 4 / 3)
        self.assertEqual(one["method"], "desk plane, one view")
        self.assertLess(np.linalg.norm(np.array(one["point_m"]) - self.target["point_m"]), 0.005)
        self.assertAlmostEqual(one["size_m"], 0.08, delta=0.01)

    def test_per_preset_correction_from_a_measured_point(self):
        joints, box = self.sighting("top")
        shifted = [box[0], box[1] + 0.1, box[2], box[3] + 0.1]            # the camera sees it lower than the model says
        raw = predict.localize([(joints, shifted)], self.ranges, 4 / 3)
        self.assertGreater(np.linalg.norm(np.array(raw["point_m"]) - self.target["point_m"]), 0.01)
        bias = predict.measured_bias(joints, shifted, self.target["point_m"], self.ranges, 4 / 3)
        self.assertAlmostEqual(bias[1], 0.1, delta=0.01)
        cal = {"bias": {"top": list(bias), "wide": [0.0, 0.0]}}
        fixed = predict.localize([(joints, shifted, predict.bias_for({"where": "view", "view": "top"}, cal))], self.ranges, 4 / 3)
        self.assertLess(np.linalg.norm(np.array(fixed["point_m"]) - self.target["point_m"]), 0.003)
        half = predict.bias_for({"preset": "top", "toward": "wide", "s": 0.5}, cal)
        self.assertAlmostEqual(half[1], bias[1] / 2, places=4)
        self.assertEqual(predict.bias_for({"where": "view", "view": "left"}, cal), (0.0, 0.0))
        pv = predict.predict_view(joints, self.target, self.ranges, 4 / 3, bias=bias)
        self.assertAlmostEqual((pv["box"][1] + pv["box"][3]) / 2, (shifted[1] + shifted[3]) / 2, delta=0.01)

    def test_prediction_refuses_unrecognisable_views(self):
        views = motion.load_views()
        self.assertTrue(predict.predict_view(views["top"], self.target, self.ranges, 4 / 3)["visible"])
        far = {"point_m": [0.6, -0.4, 0.0], "size_m": 0.08}
        self.assertFalse(predict.predict_view(views["top"], far, self.ranges, 4 / 3)["visible"])
        tiny = {**self.target, "size_m": 0.005}
        self.assertIn("too small", predict.predict_view(views["top"], tiny, self.ranges, 4 / 3)["reason"])


if __name__ == "__main__":
    unittest.main()
