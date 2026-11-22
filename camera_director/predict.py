"""Where the target is on the desk, and how it would look from each approved pose, without moving.

Localization uses what the arm really reports: measured servo positions (Station, raw steps) and the
live calibration ranges, mapped through fk.py's ElRobot model to the camera pose (framing.camera_pose).
Each settled sighting gives a ray through the target's box centre, cut with the desk plane
(framing.TABLE_Z); views are combined weighted by how much a camera tilt error would move their point
(near-vertical views count most). Free triangulation of the rays is reported as a calibration check:
on the real arm (22 Nov) home and top disagreed by 28 cm, the triangulated "desk" point floating 11 cm
up, so the model's camera pose is off at the folded home pose.

The prediction projects a disc of the estimated size at that point into each candidate pose and scores
centring, edge clearance, size and viewing angle. It inherits fk.py's and framing.py's estimates
(camera offset/tilt +-2 cm/+-5 deg, guessed field of view): predictions choose where to look; only a
camera observation after a move counts as a verified view.
"""
from __future__ import annotations

import math

import numpy as np

import framing
import motion

MIN_BASELINE_DEG = 8.0          # views closer in direction than this do not triangulate usefully
JAW_ZONE = (0.18, 0.88, 0.62)   # x1, x2, y1 of the gripper jaws in the real camera image (views/top.jpg)
PREDICT_FRACTIONS = (0.08, 0.16, 0.24, 0.32)
MAX_TILT_DEG = 75.0             # flatter than this from straight down, a flat target is too foreshortened to recognise
                                # (the real gripper camera tracked the sticker at 69 deg, 22 Nov)
TILT_UNCERTAINTY_DEG = 5.0      # fk.py's camera tilt estimate; drives the stated single-view distance uncertainty
MIN_BOX_PX = 40                 # smaller than this on its long side, too few features to recognise
IMAGE_W = 960                   # default pixels across a frame (for MIN_BOX_PX); callers pass the real width
W_PRED = {"composition": 0.30, "edge_clearance": 0.25, "target_size": 0.20, "legibility": 0.15, "stability": 0.10}


# ---------------------------------------------------------------- per-preset camera correction
#
# fk.py's absolute joint angles are off by pose-dependent amounts (on the real arm, 22 Nov: no single
# camera tilt/offset explained both home and top). With an object at a tape-measured desk point, each
# preset gets the image offset between where the model puts that point and where the camera sees it;
# localization removes it from a sighting, prediction adds it back. Stops along a line blend the two ends.

def bias_for(where: dict | None, calibration: dict | None) -> tuple[float, float]:
    """Image offset (du, dv) for a pose described like motion.locate(): a view or a stop between two views."""
    b = (calibration or {}).get("bias") or {}
    if not where:
        return (0.0, 0.0)
    if where.get("toward") is not None or where.get("where") == "between":
        a, q = where.get("view") or where.get("preset"), where.get("to") or where.get("toward")
        s = where.get("s", 0.0)
        ba, bq = b.get(a), b.get(q)
        if ba and bq:
            return (ba[0] + (bq[0] - ba[0]) * s, ba[1] + (bq[1] - ba[1]) * s)
        return tuple(ba or bq or (0.0, 0.0))
    return tuple(b.get(where.get("view") or where.get("preset"), (0.0, 0.0)))


def measured_bias(joints: dict, box: list, point_m: list, ranges, aspect: float) -> tuple[float, float]:
    """Observed box centre minus where the model projects the tape-measured point, as image fractions."""
    pose = tuple(map(tuple, _pose(joints, ranges)))
    uv = framing.project(tuple(point_m), pose, aspect)
    if uv is None:
        raise ValueError("the measured point is behind the camera in the model")
    cu, cv = _centre(box)
    return (round(cu - uv[0], 4), round(cv - uv[1], 4))


def _pose(joints: dict, ranges) -> tuple:
    return tuple(np.array(v) for v in framing.camera_pose({int(m): int(p) for m, p in joints.items()}, ranges))


def _centre(box) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _desk_hit(c, d) -> tuple[np.ndarray, float] | None:
    """Where a ray meets the desk, and how far that point moves if the camera tilt is off by
    TILT_UNCERTAINTY_DEG (small looking straight down, large for grazing views)."""
    if d[2] >= -0.05:
        return None
    theta = math.acos(min(1.0, -d[2]))
    height = max(0.0, c[2] - framing.TABLE_Z)
    lo = height * math.tan(max(0.0, theta - math.radians(TILT_UNCERTAINTY_DEG)))
    hi = height * math.tan(min(math.radians(89), theta + math.radians(TILT_UNCERTAINTY_DEG)))
    return c + d * (framing.TABLE_Z - c[2]) / d[2], max(0.005, (hi - lo) / 2)


def localize(sightings: list[tuple[dict, list]], ranges, aspect: float) -> dict:
    """sightings: [(measured joints, box)] from settled views of an object lying on the desk.
    -> {ok, point_m, size_m, method, uncertainty_m, views, check: {...}, warning?}

    Each view's ray is cut with the desk plane and the points are averaged with weights 1/uncertainty^2,
    so a near-vertical view outweighs a grazing one. With views from different directions the rays are
    also triangulated freely, as a calibration check only: for an object on the desk the triangulated
    height should be ~0, and the per-view desk points should agree within their uncertainties."""
    rays = []
    for sighting in sightings:
        joints, box = sighting[0], sighting[1]
        du, dv = sighting[2] if len(sighting) > 2 else (0.0, 0.0)     # per-preset correction, see bias_for
        cam, axis, right, down = _pose(joints, ranges)
        cu, cv = _centre(box)
        d = np.array(framing.ray(cu - du, cv - dv, (tuple(cam), tuple(axis), tuple(right), tuple(down)), aspect))
        rays.append((cam, d, axis, box))
    if not rays:
        return {"ok": False, "reason": "no sightings"}
    hits = [h for h in (_desk_hit(c, d) for c, d, *_ in rays) if h]
    if not hits:
        return {"ok": False, "reason": "no sighting looks down onto the desk"}
    w = np.array([1 / u ** 2 for _, u in hits])
    point = sum(wi * p for wi, (p, _) in zip(w, hits)) / w.sum()
    uncertainty = float(1 / math.sqrt(w.sum()))
    th, tv = framing._tans(aspect)
    c, d, axis, box = min(rays, key=lambda r: (_desk_hit(r[0], r[1]) or (None, 9.9))[1])   # the most vertical view
    depth = float((point - c) @ axis)
    size = max((box[2] - box[0]) * 2 * th * depth, (box[3] - box[1]) * 2 * tv * depth)
    out = {"ok": True, "point_m": [round(float(v), 4) for v in point], "size_m": round(size, 4),
           "views": len(rays), "uncertainty_m": round(uncertainty, 3),
           "method": "desk plane, one view" if len(hits) == 1 else f"desk plane, {len(hits)} views weighted by viewing angle"}
    check = {}
    if len(hits) >= 2:
        far = max(float(np.linalg.norm(p - q)) for p, _ in hits for q, _ in hits)
        check["view_disagreement_m"] = round(far, 3)
        dirs = [r[1] for r in rays]
        spread = max(math.degrees(math.acos(np.clip(a @ b, -1, 1))) for a in dirs for b in dirs)
        if spread >= MIN_BASELINE_DEG:
            A, b = np.zeros((3, 3)), np.zeros(3)
            for c, d, *_ in rays:
                P = np.eye(3) - np.outer(d, d)
                A += P
                b += P @ c
            tri = np.linalg.solve(A, b)
            check.update(triangulated_m=[round(float(v), 4) for v in tri], triangulated_height_m=round(float(tri[2] - framing.TABLE_Z), 3))
        allowed = 2 * math.sqrt(sum(u ** 2 for _, u in hits[:2]))
        if far > allowed or abs(check.get("triangulated_height_m", 0.0)) > 0.04:
            out["warning"] = (f"the views disagree by {far * 100:.0f} cm (triangulated height "
                              f"{check.get('triangulated_height_m', 0) * 100:.0f} cm): the camera model is off at some poses; "
                              "the estimate leans on the most vertical view")
    elif uncertainty > 0.05:
        out["warning"] = (f"one grazing view: distance uncertain by about {uncertainty * 100:.0f} cm; "
                          "a second view from above (e.g. top) pins it down")
    out["check"] = check
    return out


def predict_view(joints: dict, target: dict, ranges, aspect: float, image_w: int = 960,
                 bias: tuple[float, float] = (0.0, 0.0)) -> dict:
    """How the localized target would appear from `joints`: box, components, score (0..1) or not visible.
    `bias`: the pose's camera correction (bias_for), added to the model's projection."""
    cam, axis, right, down = pose = _pose(joints, ranges)
    p, r = np.array(target["point_m"]), target["size_m"] / 2
    ring = [p + r * np.array([math.cos(a), math.sin(a), 0.0]) for a in np.linspace(0, 2 * math.pi, 16, endpoint=False)]
    uv = [framing.project(tuple(q), tuple(map(tuple, pose)), aspect) for q in ring]
    if any(v is None for v in uv):
        return {"visible": False, "score": None, "reason": "the target is behind the camera"}
    us, vs = [v[0] + bias[0] for v in uv], [v[1] + bias[1] for v in uv]
    box = [min(us), min(vs), max(us), max(vs)]
    cx, cy = _centre(box)
    if box[2] < 0 or box[0] > 1 or box[3] < 0 or box[1] > 1:
        return {"visible": False, "score": None, "box": box, "reason": "the target would be out of the picture"}
    x1, x2, y1 = JAW_ZONE
    if x1 < cx < x2 and cy > y1:
        return {"visible": False, "score": None, "box": box, "reason": "the target would be behind the gripper jaws"}
    ray = (p - cam) / np.linalg.norm(p - cam)
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, float(-ray[2])))))
    if tilt > MAX_TILT_DEG:
        return {"visible": False, "score": None, "box": box, "reason": f"seen {tilt:.0f} deg from straight down: too oblique to recognise"}
    if max(box[2] - box[0], box[3] - box[1]) * image_w < MIN_BOX_PX:
        return {"visible": False, "score": None, "box": box, "reason": "the target would be too small in the picture"}
    comp = 1.0 - min(1.0, math.hypot(cx - 0.5, cy - 0.5) / 0.5)
    edge = max(0.0, min(1.0, min(box[0], box[1], 1 - box[2], 1 - box[3]) / 0.12))
    occ = max(1e-6, (box[2] - box[0]) * (box[3] - box[1]))
    from .vision import DESIRED_OCCUPANCY
    size = math.exp(-(math.log(occ / DESIRED_OCCUPANCY) ** 2) / (2 * 0.8 ** 2))
    legible = max(0.0, float(-ray[2]))       # cos of the angle from straight down: grazing views are hard to read
    comps = {"composition": comp, "edge_clearance": edge, "target_size": size, "legibility": legible, "stability": 1.0}
    score = sum(W_PRED[k] * comps[k] for k in W_PRED)
    return {"visible": True, "score": round(score, 4), "box": [round(v, 4) for v in box],
            "components": {k: round(v, 3) for k, v in comps.items()}, "direction": [round(float(v), 3) for v in ray]}


def candidate_poses(ranges, grip: int, gate: dict) -> dict[str, list[dict]]:
    """For every checked preset: the preset itself and stops along its checked lines (in PHYSICAL mode only
    approved lines and fractions), with the joints of each, from views.json clamped to the live calibration."""
    raw = motion._read_views_file()
    env = motion.envelope(ranges)
    views = {n: motion.clamp_pose({int(k): v for k, v in raw[n]["joints"].items()}, env) for n in raw}
    links = motion._Links(views, ranges, grip)
    out = {}
    for p in motion.view_names():
        if not motion.view_checked(p) or p not in views:
            continue
        cands = [{"toward": None, "s": 0.0, "route": "preset", "joints": {**views[p], motion.GRIPPER: grip}}]
        for q in motion.view_names():
            if q == p or q not in views:
                continue
            plan = links.plan(p, q)
            line = "-".join(sorted((p, q)))
            if not plan["ok"] or (gate.get("lines") is not None and line not in gate["lines"]):
                continue
            for s in PREDICT_FRACTIONS:
                if s <= min(gate.get("max_fraction", motion.ALONG_MAX), motion.ALONG_MAX) + 1e-9:
                    j = motion._path_pose(views[p], views[q], views[motion.TRANSIT], plan["lift"], s)
                    cands.append({"toward": q, "s": s, "route": "arc" if plan["lift"] else "direct", "line": line,
                                  "joints": {**{m: int(round(v)) for m, v in j.items()}, motion.GRIPPER: grip}})
        out[p] = cands
    return out


def line_fraction(joints: dict, a: dict, b: dict) -> float:
    """Where measured joints are along the joint-space line a -> b (0..1), from the joints that travel most."""
    ids = [m for m in motion.MOVABLE if abs(b[m] - a[m]) > 20] or list(motion.MOVABLE)
    num = sum((joints[m] - a[m]) * (b[m] - a[m]) for m in ids)
    den = sum((b[m] - a[m]) ** 2 for m in ids) or 1
    return max(0.0, min(1.0, num / den))
