"""Targetless self-calibration of the arm's camera model, from the arm's own joint readings and camera images.

No tape measure, no calibration board: the arm looks at one static object (anything the tracker follows)
from several poses. One least-squares fit (bundle adjustment) estimates, together with the object's unknown
desk position, the corrections the fk/framing camera model needs: camera tilt, camera offset on the gripper,
and the zero offsets of the pitch joints (2, 3, 4, 6). After that, any new object is localized from images
alone. Weak priors keep the corrections near the hand-measured model when the views do not pin them down.

Same idea as targetless hand-eye calibration with bundle adjustment (structure from motion with the robot's
poses as constraints); the parameter set is chosen for what this model gets wrong (fk.py: joint angles off
by several degrees, camera offset/tilt fitted to +-2 cm / +-5 deg).

The fitted model is applied through framing.CAMERA_CAL (framing.camera_pose reads it).
"""
from __future__ import annotations

import math

import numpy as np

import framing

JOINTS = (2, 3, 4, 6)                       # zero offsets fitted for these motors (steps)
NAMES = ["tilt_deg", "cam_x", "cam_y", "cam_z"] + [f"q{m}" for m in JOINTS]
PRIOR_SIGMA = [8.0, 0.03, 0.03, 0.03] + [60.0] * len(JOINTS)   # how far each may move from the model, softly
MIN_VIEWS = 5


def as_cal(p) -> dict:
    return {"tilt_deg": float(p[0]), "cam": [float(p[1]), float(p[2]), float(p[3])],
            "joint_offsets": {m: float(p[4 + i]) for i, m in enumerate(JOINTS)}}


def _project(cal: dict, joints: dict, point, ranges, aspect: float):
    old = framing.CAMERA_CAL
    framing.CAMERA_CAL = cal
    try:
        return framing.project(tuple(point), framing.camera_pose(joints, ranges), aspect)
    finally:
        framing.CAMERA_CAL = old


def _residuals(x, obs, ranges, aspect, n_params):
    cal = as_cal(x[:n_params])
    point = x[n_params:n_params + 3]
    r = []
    for joints, uv in obs:
        pr = _project(cal, joints, point, ranges, aspect)
        if pr is None:
            r += [1.0, 1.0]
        else:
            r += [pr[0] - uv[0], pr[1] - uv[1]]
    r += [x[i] / PRIOR_SIGMA[i] * 0.01 for i in range(n_params)]     # soft priors (0.01 of the frame per sigma)
    r += [0.0]
    return np.array(r)


def fit(observations: list[tuple[dict, tuple[float, float]]], ranges, aspect: float, point0, on_desk: bool = True) -> dict:
    """observations: [(measured joints, observed image centre (u, v))] of ONE static object from different poses.
    -> {ok, cal, point_m, rms_before, rms_after (image fractions), views}"""
    if len(observations) < MIN_VIEWS:
        return {"ok": False, "reason": f"need at least {MIN_VIEWS} views, have {len(observations)}"}
    ranges = {int(m): tuple(r) for m, r in ranges.items()}
    obs = [({int(m): int(v) for m, v in j.items()}, (float(uv[0]), float(uv[1]))) for j, uv in observations]
    n = len(NAMES)
    x = np.concatenate([np.zeros(n), np.array(point0, float)])
    r = _residuals(x, obs, ranges, aspect, n)
    rms_before = float(np.sqrt(np.mean(r[:2 * len(obs)] ** 2)))
    lam = 1e-3
    steps = [0.05] + [0.0005] * 3 + [0.5] * len(JOINTS) + [0.0005] * 3   # finite-difference steps
    for _ in range(60):
        J = np.zeros((len(r), len(x)))
        for k in range(len(x)):
            if on_desk and k == n + 2:
                continue                                   # the object lies on the desk: its height stays put
            xx = x.copy()
            xx[k] += steps[k]
            J[:, k] = (_residuals(xx, obs, ranges, aspect, n) - r) / steps[k]
        A = J.T @ J + lam * np.diag(np.maximum(np.diag(J.T @ J), 1e-9))
        dx = np.linalg.lstsq(A, -J.T @ r, rcond=None)[0]
        x2 = x + dx
        r2 = _residuals(x2, obs, ranges, aspect, n)
        if r2 @ r2 < r @ r:
            x, r, lam = x2, r2, lam * 0.3
            if np.linalg.norm(dx) < 1e-6:
                break
        else:
            lam *= 10
            if lam > 1e6:
                break
    rms_after = float(np.sqrt(np.mean(r[:2 * len(obs)] ** 2)))
    return {"ok": rms_after < rms_before, "cal": as_cal(x[:n]), "point_m": [round(float(v), 4) for v in x[n:n + 3]],
            "rms_before": round(rms_before, 4), "rms_after": round(rms_after, 4), "views": len(obs),
            "params": {k: round(float(v), 4) for k, v in zip(NAMES, x[:n])}}


def call(cal: dict | None, fn, *args):
    """Run fn with this camera calibration active (for the director's vision worker process)."""
    framing.CAMERA_CAL = cal
    return fn(*args)
