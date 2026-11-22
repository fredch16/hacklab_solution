"""Where the marked object is, fused over every tracked frame (a 3-D Kalman filter).

Each detection gives a ray from the camera (pose from the joint readings through fk/framing and the self-calibrated
camera model) through the object's image position: its direction is good to about a degree, its length (from the
object's size in the picture) only to ~30 %. Rays from different camera poses (follow, gimbal takes) triangulate;
the object may be moved by hand, so the estimate keeps a random-walk uncertainty. No SLAM: the joint encoders
already give the camera pose more precisely than vision would.
"""
from __future__ import annotations

import numpy as np

MOVE_SPEED = 0.15          # m/s: how fast the object may be moved by hand (random-walk process noise)
BEARING_STILL = 0.02       # rad: ray direction noise with the arm still (model + image error)
BEARING_MOVING = 0.08      # rad: ...while it moves (the frame and the joint reading are not from the same instant)
RANGE_REL = 0.3            # range from apparent size: relative noise


class ObjectFilter:
    def __init__(self):
        self.x: np.ndarray | None = None      # object position (m, fk frame)
        self.P = np.eye(3)
        self.t = 0.0
        self.size_m: float | None = None      # apparent size x distance: the object's size, learned once

    def reset(self):
        self.__init__()

    def start(self, point, cam, size: float, t: float):
        """First sighting, the object resting on the table at `point` (the ray's table intersection)."""
        self.x = np.asarray(point, float)
        self.P = np.eye(3) * 0.03 ** 2
        self.t = t
        self.size_m = size * float(np.linalg.norm(self.x - np.asarray(cam, float)))

    def update(self, cam, direction, size: float, t: float, moving: bool = False):
        """One detection: `direction` the unit ray from camera position `cam`, `size` the box size (0..1)."""
        cam, d = np.asarray(cam, float), np.asarray(direction, float)
        dt = max(0.0, t - self.t)
        self.t = t
        self.P = self.P + np.eye(3) * (MOVE_SPEED * dt) ** 2
        rng = self.size_m / max(size, 1e-3)
        z = cam + d * rng
        dd = np.outer(d, d)
        sb = (BEARING_MOVING if moving else BEARING_STILL) * rng
        R = (RANGE_REL * rng) ** 2 * dd + sb ** 2 * (np.eye(3) - dd)
        K = self.P @ np.linalg.inv(self.P + R)
        self.x = self.x + K @ (z - self.x)
        self.x[2] = max(self.x[2], 0.0)       # never below the table (fk frame: the desk is z = 0)
        self.P = (np.eye(3) - K) @ self.P

    def state(self) -> dict | None:
        if self.x is None:
            return None
        return {"point_m": [round(float(v), 4) for v in self.x],
                "sigma_m": round(float(np.sqrt(np.trace(self.P) / 3)), 4)}
