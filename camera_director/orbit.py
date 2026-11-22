"""Camera moves generated around the localized object, not tied to saved views: arc (orbit) shots.

A shot is a camera path on a sphere around the object's desk point: azimuth theta (0 = seen from the
arm's base side, +90 = from the right, -90 = from the left), elevation above the desk and distance.
Each pose is solved with framing.solve_pose (damped least squares on fk.py's model, joint limits
enforced), warm-started from its neighbour so the joint path is smooth. The aim is offset by the
camera correction (image fractions, predict.bias_for / a measured look) so the real camera centres the
object. Safety is checked by motion.path_problem when the path is run (motion.go_through).

Runs in the director's vision worker process: IK is pure Python and takes a second or two per arc.
"""
from __future__ import annotations

import math

import numpy as np

import framing
import motion

POS_TOL = 0.015     # m: a solved camera position must be this close to the asked one
AXIS_TOL = 4.0      # deg: and its viewing direction this close
STEP_DEG = 10.0     # azimuth between solved poses
MAX_JUMP = 250      # steps: neighbouring poses further apart get a pose solved half-way between


def camera_goal(point, theta: float, elev: float, dist: float, bias=(0.0, 0.0), aspect: float = 16 / 9):
    """(camera position, viewing axis, image right) to see `point` from (theta, elev, dist), with the aim
    shifted so that a camera whose image is offset by `bias` (real minus model) still centres it."""
    p = np.array(point, float)
    t, e = math.radians(theta), math.radians(elev)
    c = p + dist * np.array([math.sin(t) * math.cos(e), -math.cos(t) * math.cos(e), math.sin(e)])
    axis = (p - c) / np.linalg.norm(p - c)
    right = np.cross(axis, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(axis, right)
    th, tv = framing._tans(aspect)
    q = p + right * bias[0] * 2 * th * dist + down * bias[1] * 2 * tv * dist
    axis = (q - c) / np.linalg.norm(q - c)
    right = np.cross(axis, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    return tuple(c), tuple(axis), tuple(right)


LIMIT_MARGIN = 40   # steps the solver keeps inside each envelope, so the smooth curve through the poses has room


def _limits(ranges) -> dict:
    return {m: (lo + LIMIT_MARGIN, hi - LIMIT_MARGIN) for m, (lo, hi) in motion.envelope(ranges).items()}


def _solve(guess: dict, goal, ranges, limits) -> tuple[dict, bool]:
    q, pe, ae = framing.solve_pose(guess, *goal, ranges, iterations=200, limits=limits)
    return q, pe <= POS_TOL and ae <= AXIS_TOL


def plan_arc(point, start: dict, ranges, *, elev: float, dist: float, theta_from: float, theta_to: float,
             bias=(0.0, 0.0), aspect: float = 16 / 9, seeds: tuple = ()) -> dict:
    """Solve an arc of camera poses from theta_from to theta_to. Stops early at the first azimuth the model
    cannot reach. -> {ok, poses: [{motor: steps}], thetas, span_deg, reason}"""
    ranges = {int(m): tuple(r) for m, r in ranges.items()}
    limits = _limits(ranges)
    start = {int(m): int(v) for m, v in start.items()}
    grip = start.get(motion.GRIPPER, 1279)
    n = max(1, int(round(abs(theta_to - theta_from) / STEP_DEG)))
    thetas = [theta_from + (theta_to - theta_from) * i / n for i in range(n + 1)]
    best = None
    for seed in (start, *seeds):                      # the first pose: the seed that reaches it best
        q, ok = _solve({**{int(m): int(v) for m, v in seed.items()}, motion.GRIPPER: grip},
                       camera_goal(point, thetas[0], elev, dist, bias, aspect), ranges, limits)
        if ok:
            travel = max(abs(q[m] - start[m]) for m in motion.MOVABLE)
            if best is None or travel < best[1]:
                best = (q, travel)
    if best is None:
        return {"ok": False, "poses": [], "thetas": [], "span_deg": 0.0,
                "reason": f"the model cannot place the camera at {thetas[0]:.0f} deg, {elev:.0f} deg up, {dist * 100:.0f} cm away"}
    poses, done, reason = [best[0]], [thetas[0]], None
    for th in thetas[1:]:
        q, ok = _solve(poses[-1], camera_goal(point, th, elev, dist, bias, aspect), ranges, limits)
        if not ok:
            reason = f"the arc ends at {done[-1]:.0f} deg: {th:.0f} deg is out of reach"
            break
        if max(abs(q[m] - poses[-1][m]) for m in motion.MOVABLE) > MAX_JUMP:
            mid, ok = _solve(poses[-1], camera_goal(point, (done[-1] + th) / 2, elev, dist, bias, aspect), ranges, limits)
            if not ok or max(abs(q[m] - mid[m]) for m in motion.MOVABLE) > MAX_JUMP:
                reason = f"the arc ends at {done[-1]:.0f} deg: the arm would have to swing too far around {th:.0f} deg"
                break
            poses.append(mid)
            done.append((done[-1] + th) / 2)
        poses.append(q)
        done.append(th)
    poses = [{m: int(q[m]) for m in motion.MOVABLE} for q in poses]
    return {"ok": len(poses) >= 2, "poses": poses, "thetas": [round(t, 1) for t in done],
            "span_deg": round(abs(done[-1] - done[0]), 1), "reason": reason}


def reach_table(point, start: dict, ranges, elevs=(40, 50, 60), dists=(0.18, 0.22, 0.26), seeds: tuple = ()) -> list[dict]:
    """For the planner: the widest reachable arc (within -90..90 deg) at each elevation and distance."""
    out = []
    for e in elevs:
        for d in dists:
            left = plan_arc(point, start, ranges, elev=e, dist=d, theta_from=0, theta_to=-90, seeds=seeds)
            right = plan_arc(point, start, ranges, elev=e, dist=d, theta_from=0, theta_to=90, seeds=seeds)
            lo = left["thetas"][-1] if left["ok"] else 0.0
            hi = right["thetas"][-1] if right["ok"] else 0.0
            out.append({"elev": e, "dist": d, "from": lo, "to": hi, "span": round(hi - lo, 1)})
    return out


# ---------------------------------------------------------------- free camera paths through keyframes
#
# Any move is a list of keyframes {theta, elev, dist} around the object: an arc changes theta, a crane
# changes elev, a push-in / pull-out changes dist, and mixes give diagonals, spirals and reveals. The path
# is interpolated densely, solved as one warm IK chain and run as one continuous move.

SAMPLE_DEG = 6.0          # angular spacing of solved poses along a path
SAMPLE_M = 0.015          # ...or this much change in distance


def _interpolate(keyframes: list[dict]) -> list[tuple[float, float, float]]:
    pts = []
    for a, b in zip(keyframes, keyframes[1:]):
        span = max(abs(b["theta"] - a["theta"]) / SAMPLE_DEG, abs(b["elev"] - a["elev"]) / SAMPLE_DEG,
                   abs(b["dist"] - a["dist"]) / SAMPLE_M, 1)
        n = int(math.ceil(span))
        for i in range(n):
            f = i / n
            pts.append(tuple(a[k] + (b[k] - a[k]) * f for k in ("theta", "elev", "dist")))
    k = keyframes[-1]
    pts.append((k["theta"], k["elev"], k["dist"]))
    return pts


def plan_path(point, start: dict, ranges, keyframes: list[dict], bias=(0.0, 0.0), aspect: float = 16 / 9,
              seeds: tuple = ()) -> dict:
    """Solve a camera path through keyframes. Stops at the first unreachable sample (the rest is reported)."""
    if len(keyframes) < 2:
        return {"ok": False, "poses": [], "samples": [], "reason": "a move needs at least two keyframes"}
    ranges = {int(m): tuple(r) for m, r in ranges.items()}
    limits = _limits(ranges)
    start = {int(m): int(v) for m, v in start.items()}
    grip = start.get(motion.GRIPPER, 1279)
    samples = _interpolate(keyframes)
    best = None
    for seed in (start, *seeds):
        q, ok = _solve({**{int(m): int(v) for m, v in seed.items()}, motion.GRIPPER: grip},
                       camera_goal(point, *samples[0], bias, aspect), ranges, limits)
        if ok:
            travel = max(abs(q[m] - start[m]) for m in motion.MOVABLE)
            if best is None or travel < best[1]:
                best = (q, travel)
    if best is None:
        return {"ok": False, "poses": [], "samples": [], "reason": f"the first keyframe {samples[0]} is out of reach"}
    poses, done, reason = [best[0]], [samples[0]], None
    for s in samples[1:]:
        q, ok = _solve(poses[-1], camera_goal(point, *s, bias, aspect), ranges, limits)
        if not ok or max(abs(q[m] - poses[-1][m]) for m in motion.MOVABLE) > MAX_JUMP:
            reason = f"the move ends early: theta {s[0]:.0f}, elev {s[1]:.0f}, {s[2] * 100:.0f} cm is out of reach"
            break
        poses.append(q)
        done.append(s)
    return {"ok": len(poses) >= 2, "poses": [{m: int(q[m]) for m in motion.MOVABLE} for q in poses],
            "samples": [[round(v, 3) for v in s] for s in done], "complete": reason is None, "reason": reason}


def reach_map(point, start: dict, ranges, seeds: tuple = (), elevs=(30, 45, 60, 75), dists=(0.16, 0.2, 0.25),
              thetas=tuple(range(-150, 151, 30))) -> dict:
    """Which (theta, elev, dist) the camera can reach, chained along theta from the front at each elev/dist.
    -> {"thetas", "elevs", "dists", "ok": {"elev|dist": [theta, ...]}}"""
    ranges = {int(m): tuple(r) for m, r in ranges.items()}
    limits = _limits(ranges)
    start = {int(m): int(v) for m, v in start.items()}
    grip = start.get(motion.GRIPPER, 1279)
    seeds = [start, *[{**{int(m): int(v) for m, v in s.items()}, motion.GRIPPER: grip} for s in seeds]]
    ok = {}
    order = sorted(thetas, key=abs)
    for e in elevs:
        for d in dists:
            reach, solved = [], {}
            for th in order:
                near = min((t for t in solved if abs(t - th) <= 31), key=lambda t: abs(t - th), default=None)
                tries = ([solved[near]] if near is not None else []) + seeds
                for sd in tries:
                    q, good = _solve({**sd, motion.GRIPPER: grip}, camera_goal(point, th, e, d), ranges, limits)
                    if good:
                        solved[th] = q
                        reach.append(th)
                        break
            ok[f"{e}|{d}"] = sorted(reach)
    return {"thetas": list(thetas), "elevs": list(elevs), "dists": list(dists), "ok": ok}


ADJUST = ((0, 0.0), (5, 0.0), (0, 0.02), (5, 0.02), (10, 0.0), (10, 0.02), (15, 0.03), (20, 0.04),
          (5, -0.02), (10, -0.02), (15, -0.02), (10, -0.03))
REACH_LIMIT_VIEW = "top"   # no computed pose may hold the wrist further from the shoulder than this view does:
                           # the ElRobot shoulder holds `top` at ~8 % load, its servo trips above 25 % (2 s)


def shoulder_reach(q: dict, ranges, grip: int = 1279) -> float:
    """Horizontal distance (m) of the wrist and camera from the shoulder axis: what the shoulder must hold up."""
    import fk
    pts, _, _ = fk._key_points(fk.steps_to_angles({**{int(m): int(v) for m, v in q.items()}, motion.GRIPPER: grip}, ranges))
    sh = np.array(pts["j2_shoulder"])
    return max(float(np.hypot(*(np.array(pts[n])[:2] - sh[:2]))) for n in ("j6_wrist_pitch", "camera"))


def _bridge(a: dict, b: dict, step: int = 200) -> list[dict]:
    n = max(1, int(math.ceil(max(abs(b[m] - a[m]) for m in motion.MOVABLE) / step)))
    return [{m: int(round(a[m] + (b[m] - a[m]) * k / n)) for m in motion.MOVABLE} for k in range(1, n + 1)]


def plan_shot(point, starts: list[dict], ranges, keyframes: list[dict], bias=(0.0, 0.0), aspect: float = 16 / 9,
              seeds: tuple = ()) -> dict:
    """plan_path, made safe: the approach from a start pose and the move itself must pass motion.path_problem.
    If not, the whole move is raised and pulled back in small steps (keeping its shape). Starts are tried in
    order (where the arm is, then transit). -> plan_path result + {start_index, adjusted, approach}"""
    ranges = {int(m): tuple(r) for m, r in ranges.items()}
    env = motion.envelope(ranges)
    first_reason = None
    raw = motion._read_views_file()
    limit = shoulder_reach({int(k): v for k, v in raw[REACH_LIMIT_VIEW]["joints"].items()}, ranges) \
        if REACH_LIMIT_VIEW in raw else None
    for si, start in enumerate(starts):
        start = {int(m): int(v) for m, v in start.items()}
        grip = start.get(motion.GRIPPER, 1279)
        best = None
        for de, dd in ADJUST:
            frames = [{"theta": k["theta"], "elev": min(80.0, k["elev"] + de), "dist": min(0.3, k["dist"] + dd)} for k in keyframes]
            plan = plan_path(point, start, ranges, frames, bias, aspect, seeds)
            if not plan["ok"]:
                first_reason = first_reason or plan["reason"]
                continue
            here = {m: start[m] for m in motion.MOVABLE}
            approach = _bridge(here, plan["poses"][0])
            problem = (motion.path_problem([here] + approach, ranges, env, grip)
                       or motion.path_problem([plan["poses"][0]] + plan["poses"][1:], ranges, env, grip, absolute=True))
            if problem:
                first_reason = first_reason or problem
                continue
            reach = max(shoulder_reach(q, ranges, grip) for q in plan["poses"])
            if limit is not None and reach > limit:
                first_reason = first_reason or (f"the arm would reach {reach * 100:.0f} cm from the shoulder, further than "
                                                f"at {REACH_LIMIT_VIEW} ({limit * 100:.0f} cm)")
                continue
            span = abs(plan["samples"][-1][0] - plan["samples"][0][0]) + abs(plan["samples"][-1][1] - plan["samples"][0][1])
            key = (plan["complete"], round(span), -reach)          # the full move first, then the lightest version
            if best is None or key > best[0]:
                best = (key, {**plan, "start_index": si, "adjusted": {"elev": de, "dist": dd}, "approach": approach,
                              "keyframes": frames, "shoulder_reach_m": round(reach, 3)})
        if best:
            return best[1]
    return {"ok": False, "poses": [], "reason": first_reason or "no safe version of this move"}
