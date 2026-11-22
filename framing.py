"""Stage 2 preview: "frame this object" -> where the camera would have to go. Standard library only.

Given the box of an object in the current camera image (normalized 0..1, as Claude records it in
the scene) and the arm's present joints, compute

  1. the object's point on the table: a ray through the box centre from the camera, cut with the
     table plane,
  2. a target camera pose: same viewing direction, centred on that point, at the distance where the
     object fills the frame ("fill") or at the present distance ("center"),
  3. target joints for it: damped least squares on fk.py's model, starting from the present joints,
  4. checks: model residual, calibrated joint ranges, how far the joints would travel, and
     fk.check_path for a path dipping below the table.

It never moves anything: capture_server.py returns this as a preview (POST /api/frame, dry_run).

    python3 framing.py top 0.6 0.55 0.2 0.15        # view, box cx cy w h -> preview as JSON

How far to trust it: every number rests on fk.py's model, whose camera offset and tilt are fitted
guesses (+-2 cm, +-5 degrees) and whose absolute heights are not measured, plus the ESTIMATED
field of view and table height below. Left/right in the image is assumed not mirrored. Check on
the real arm with an object at a known spot before relying on any of it.
"""

from __future__ import annotations

import json
import math
import struct
import sys

import fk

# ESTIMATED, not measured -------------------------------------------------------------------
HFOV_DEG = 62.0          # horizontal field of view of the camera on the gripper (typical small USB camera)
DEFAULT_ASPECT = 16 / 9  # width / height when the image size is not known
TABLE_Z = 0.0            # height of the desk in fk's frame: the plane the base stands on
MIRRORED = False         # True if the image's left/right is flipped against the model
# Limits of the preview ----------------------------------------------------------------------
MIN_CAMERA_DIST = 0.12   # m: closest the camera may get to the object along its axis (the jaws reach further down)
MAX_CAMERA_DIST = 0.45   # m
MAX_REACH = 0.45         # m: table points further from the base axis are refused before any solving
MAX_JOINT_TRAVEL = 500   # encoder steps (~44 deg) any joint may move from where it is now
POS_TOL = 0.01           # m: model must reach the target camera position this closely
AXIS_TOL_DEG = 3.0       # and its viewing direction this closely
MOVABLE = (1, 2, 3, 4, 5, 6, 7)   # the gripper (8) carries the camera and is never moved
# Self-calibration (camera_director/selfcal.py) of this arm's camera model, or None for the hand-fitted model:
# {"tilt_deg": extra tilt, "cam": [dx, dforward, dup] m on the gripper, "joint_offsets": {motor: steps}}
CAMERA_CAL: dict | None = None


class FramingError(Exception):
    """The preview found no acceptable target; `code` says why (unreachable, too_low, ...)."""

    def __init__(self, code: str, detail: str, **extra):
        super().__init__(detail)
        self.code, self.detail, self.extra = code, detail, extra

    def body(self) -> dict:
        return {"ok": False, "error": self.code, "detail": self.detail, **self.extra}


# ---------------------------------------------------------------- small vector helpers

def _add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _mul(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def _norm(a): return math.sqrt(_dot(a, a))
def _unit(a):
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n)
def _r(v, n=3): return [round(c, n) for c in v]


# ---------------------------------------------------------------- camera model

def camera_pose(joints: dict, ranges: dict | None = None) -> tuple[tuple, tuple, tuple, tuple]:
    """(position, optical axis, image right, image down) of the camera in fk's world frame.
    Image down points towards the jaws: in views/top.jpg the jaw tip sits just below the centre."""
    cal = CAMERA_CAL
    if cal:
        offs = cal.get("joint_offsets") or {}
        joints = {m: p + offs.get(m, offs.get(str(m), 0.0)) for m, p in joints.items()}
    points, _, (_x, forward, up) = fk._key_points(fk.steps_to_angles(joints, ranges))
    t = math.radians(fk.CAMERA_TILT_DEG + (cal.get("tilt_deg", 0.0) if cal else 0.0))
    axis = _unit(_add(_mul(forward, math.cos(t)), _mul(up, -math.sin(t))))
    down = _unit(_sub(forward, _mul(axis, _dot(forward, axis))))
    right = _cross(down, axis)                      # x = y cross z for (x right, y down, z ahead)
    if MIRRORED:
        right = _mul(right, -1.0)
    cam = points["camera"]
    if cal and cal.get("cam"):
        dx, df, du = cal["cam"]
        cam = _add(cam, _add(_mul(_x, dx), _add(_mul(forward, df), _mul(up, du))))
    return cam, axis, right, down


def _tans(aspect: float) -> tuple[float, float]:
    th = math.tan(math.radians(HFOV_DEG) / 2)
    return th, th / aspect


def ray(u: float, v: float, pose, aspect: float) -> tuple:
    """Unit direction of the ray through image point (u, v), 0..1 from the top left."""
    _, axis, right, down = pose
    th, tv = _tans(aspect)
    return _unit(_add(axis, _add(_mul(right, (2 * u - 1) * th), _mul(down, (2 * v - 1) * tv))))


def project(point, pose, aspect: float) -> tuple[float, float] | None:
    """Image point (u, v) of a world point, or None when it is behind the camera."""
    cam, axis, right, down = pose
    d = _sub(point, cam)
    z = _dot(d, axis)
    if z <= 1e-6:
        return None
    th, tv = _tans(aspect)
    return 0.5 + _dot(d, right) / z / th / 2, 0.5 + _dot(d, down) / z / tv / 2


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker, without an image library."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


# ---------------------------------------------------------------- inverse kinematics

def _residual(steps: dict, target_pos, target_axis, target_right, ranges, roll_weight: float) -> list[float]:
    pos, axis, right, _ = camera_pose(steps, ranges)
    # 1 cm of position weighs like ~2 degrees of viewing direction; image roll matters least
    return [*_sub(pos, target_pos), *_mul(_sub(axis, target_axis), 0.3), *_mul(_sub(right, target_right), roll_weight)]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for a small square system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / m[r][r] if abs(m[r][r]) > 1e-12 else 0.0
    return x


def solve_pose(start: dict, target_pos, target_axis, target_right, ranges=None,
               iterations: int = 150, limits: dict | None = None, roll_weight: float = 0.05) -> tuple[dict, float, float]:
    """Joints (steps, motors 1-7 varied) whose camera reaches the target pose; damped least
    squares from `start`. Returns (joints, position error m, direction error deg).
    `limits` {motor: (lo, hi)}: every step is clamped inside them (otherwise the solver may leave a range)."""
    q = {m: float(p) for m, p in start.items()}
    lam, h = 1e-6, 2.0
    err = _residual(q, target_pos, target_axis, target_right, ranges, roll_weight)
    cost = _dot3(err)
    for _ in range(iterations):
        jac = []
        for m in MOVABLE:
            qq = dict(q); qq[m] += h
            e2 = _residual(qq, target_pos, target_axis, target_right, ranges, roll_weight)
            jac.append([(e2[i] - err[i]) / h for i in range(len(err))])
        # (J^T J + lam I) dq = -J^T e, with a small pull towards the start pose
        n = len(MOVABLE)
        jtj = [[sum(jac[a][i] * jac[b][i] for i in range(len(err))) + (lam if a == b else 0.0) for b in range(n)]
               for a in range(n)]
        jte = [-sum(jac[a][i] * err[i] for i in range(len(err))) for a in range(n)]
        dq = _solve(jtj, jte)
        trial = dict(q)
        for a, m in enumerate(MOVABLE):
            trial[m] += max(-150.0, min(150.0, dq[a]))
            if limits and m in limits:
                trial[m] = min(max(trial[m], limits[m][0]), limits[m][1])
        e_new = _residual(trial, target_pos, target_axis, target_right, ranges, roll_weight)
        c_new = _dot3(e_new)
        if c_new < cost:
            q, err, cost, lam = trial, e_new, c_new, lam * 0.3
            if cost < 1e-9:
                break
        else:
            lam *= 10
            if lam > 1e3:
                break
    pos, axis, _, _ = camera_pose(q, ranges)
    pos_err = _norm(_sub(pos, target_pos))
    ang_err = math.degrees(math.acos(max(-1.0, min(1.0, _dot(axis, target_axis)))))
    return {m: int(round(p)) for m, p in q.items()}, pos_err, ang_err


def _dot3(v: list[float]) -> float:
    return sum(c * c for c in v)


# ---------------------------------------------------------------- the preview

def _in_range(pos: int, lo: int, hi: int) -> bool:
    if lo <= hi:
        return lo <= pos <= hi
    return pos >= lo or pos <= hi          # arc across the encoder seam


def plan(joints: dict, bbox: dict, framing: str = "fill", margin: float = 0.1,
         aspect: float = DEFAULT_ASPECT, ranges: dict | None = None) -> dict:
    """Preview of framing the object in `bbox` (cx, cy, w, h; 0..1) seen from `joints`.
    Raises FramingError with a code when there is no acceptable target."""
    joints = {int(m): int(p) for m, p in joints.items()}
    ranges = {int(m): tuple(r) for m, r in (ranges or fk.FOLLOWER_RANGES).items()}
    if framing not in ("fill", "center"):
        raise FramingError("invalid_request", f"framing must be 'fill' or 'center', not {framing!r}")
    if not 0.0 <= margin < 0.8:
        raise FramingError("invalid_request", "margin must be between 0 and 0.8")
    try:
        cx, cy, bw, bh = (float(bbox[k]) for k in ("cx", "cy", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise FramingError("invalid_request", "bbox needs cx, cy, w, h as numbers")
    if not (0 <= cx <= 1 and 0 <= cy <= 1 and 0 < bw <= 1 and 0 < bh <= 1):
        raise FramingError("invalid_request", "bbox values must lie in 0..1 (w and h above 0)")

    pose = camera_pose(joints, ranges)
    cam, axis, right, _ = pose
    d = ray(cx, cy, pose, aspect)
    if d[2] > -0.1:
        raise FramingError("not_on_table", "the box centre does not look down onto the table from here")
    dist = (TABLE_Z - cam[2]) / d[2]
    if not 0 < dist < 1.5:
        raise FramingError("not_on_table", f"the table point would be {dist:.2f} m away; not plausible")
    point = _add(cam, _mul(d, dist))
    reach = math.hypot(point[0], point[1])
    if reach > MAX_REACH:
        raise FramingError("out_of_reach", f"the object is {reach:.2f} m from the base axis; the camera reaches "
                           f"about {MAX_REACH:.2f} m", table_point_m=_r(point))

    depth = dist * _dot(d, axis)                     # distance along the optical axis
    th, tv = _tans(aspect)
    size_w, size_h = bw * 2 * th * depth, bh * 2 * tv * depth
    notes = []
    if framing == "fill":
        need = max(size_w / (2 * th), size_h / (2 * tv)) / (1 - margin)
    else:
        need = depth
    want = max(MIN_CAMERA_DIST, min(MAX_CAMERA_DIST, need))
    if want != need:
        notes.append(f"distance limited to {want:.2f} m (wanted {need:.2f} m)")
    base = {
        "framing": framing, "margin": margin,
        "table_point_m": _r(point), "object_size_m": _r((size_w, size_h)),
        "camera_from_m": _r(cam), "camera_distance_now_m": round(depth, 3),
        "estimates": {"hfov_deg": HFOV_DEG, "table_z_m": TABLE_Z, "camera_offset_m": list(fk.CAMERA_OFFSET),
                      "camera_tilt_deg": fk.CAMERA_TILT_DEG, "mirrored": MIRRORED},
    }
    # Try the wanted distance first, then back off towards (and beyond) the present one until a
    # target passes every check: "fill" becomes "as close as is safe".
    dists = [want]
    far_end = max(want, min(MAX_CAMERA_DIST, depth + 0.06))
    while dists[-1] + 0.015 <= far_end:
        dists.append(round(dists[-1] + 0.015, 3))
    h_now = fk.min_height(joints, ranges, mapping="worst")[0]
    floor = min(fk.RECOMMENDED_MARGIN_M, h_now - 0.005)
    guess, first_error = joints, None
    for dist_try in dists:
        try:
            result = _evaluate(joints, guess, point, axis, right, dist_try, ranges, floor, h_now)
        except FramingError as e:
            first_error = first_error or e
            if "target_joints" in e.extra.get("preview", {}):
                guess = e.extra["preview"]["target_joints"]
            continue
        if dist_try > want + 1e-6:
            notes.append(f"backed off to {dist_try:.2f} m: closer failed ({first_error.code}: {first_error.detail})")
        return {**base, **result, "notes": notes, "ok": True}
    first_error.extra["preview"] = {**base, **first_error.extra.get("preview", {}), "notes": notes}
    raise first_error


def _evaluate(joints, guess, point, axis, right, dist, ranges, floor, h_now) -> dict:
    """Target joints for the camera `dist` before `point` along `axis`, and every check on them."""
    target_cam = _sub(point, _mul(axis, dist))
    target, pos_err, ang_err = solve_pose(guess, target_cam, axis, right, ranges)
    result = {
        "camera_to_m": _r(target_cam), "camera_distance_m": round(dist, 3),
        "target_joints": target,
        "joint_travel": {m: target[m] - joints[m] for m in MOVABLE},
        "model_error": {"position_cm": round(pos_err * 100, 2), "direction_deg": round(ang_err, 2)},
    }
    if pos_err > POS_TOL or ang_err > AXIS_TOL_DEG:
        raise FramingError("unreachable", f"the arm model gets only within {pos_err * 100:.1f} cm / {ang_err:.1f} deg "
                           "of that camera pose", preview=result)
    out = [m for m in MOVABLE if m in ranges and not _in_range(target[m], *ranges[m])]
    if out:
        raise FramingError("at_limit", f"motors {out} would leave their calibrated range", preview=result)
    far = {m: t for m, t in result["joint_travel"].items() if abs(t) > MAX_JOINT_TRAVEL}
    if far:
        raise FramingError("outside_envelope", f"joints {sorted(far)} would travel more than {MAX_JOINT_TRAVEL} "
                           "steps; start from a view closer to the object", preview=result)
    # fk.check_path also accepts a path that is no lower than its end poses, which is only meaningful
    # for poses the real arm has been in. A computed target is not one, so: the target and the whole
    # path must stay above the margin, or at least no lower than where the arm is now.
    end = {**joints, **target}
    h_end, low_point = fk.min_height(end, ranges, mapping="worst")
    h_path, path_point, _t = fk.path_min_height(joints, end, ranges=ranges, mapping="worst")
    result["min_height_m"] = {"now": round(h_now, 3), "target": round(h_end, 3), "path": round(h_path, 3),
                              "floor": round(floor, 3)}
    if h_end < floor:
        raise FramingError("too_low", f"{low_point} would be {h_end * 100:.1f} cm above the table in the model "
                           f"(floor {floor * 100:.1f} cm)", preview=result)
    if h_path < floor:
        raise FramingError("too_low", f"{path_point} would dip to {h_path * 100:.1f} cm on the way "
                           f"(floor {floor * 100:.1f} cm)", preview=result)
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__)
        return 2
    view, *box = argv
    joints = fk.load_views()[view]
    try:
        out = plan(joints, dict(zip(("cx", "cy", "w", "h"), map(float, box))))
    except FramingError as e:
        out = e.body()
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
