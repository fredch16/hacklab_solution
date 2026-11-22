"""SIMULATED robot camera for `capture_server.py --sim --director`: renders the desk plane seen from
the simulated arm's pose with framing.py's camera model, so director jobs can run end to end
without hardware. The desk texture and the label on it are synthetic test art, NOT the real
sticker's geometry; nothing rendered here is evidence about the real robot.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

import framing

W, H = 640, 480
MM_PER_PX = 2.0
X0, X1, Y0, Y1 = -0.7, 0.7, -0.3, 1.1     # desk area that has texture, metres in fk's frame


def _desk(label_at: tuple[float, float], label_m: float = 0.09, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    w, h = int((X1 - X0) * 1000 / MM_PER_PX), int((Y1 - Y0) * 1000 / MM_PER_PX)
    grain = cv2.resize(rng.normal(0, 1, (h // 8, w // 64)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    desk = np.clip(150 + 25 * grain + rng.normal(0, 6, (h, w)), 60, 220).astype(np.uint8)
    for _ in range(60):                     # clutter: dark blobs and lines, so features have distractors
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        if rng.random() < 0.5:
            cv2.circle(desk, (x, y), int(rng.integers(5, 25)), int(rng.integers(10, 60)), -1)
        else:
            cv2.line(desk, (x, y), (x + int(rng.integers(-200, 200)), y + int(rng.integers(-200, 200))), 20, 3)
    side = int(label_m * 1000 / MM_PER_PX)
    tag = np.full((side, side), 245, np.uint8)
    cv2.rectangle(tag, (3, 3), (side - 4, side - 4), 200, 2)
    cv2.putText(tag, "HACK", (int(side * .12), int(side * .45)), cv2.FONT_HERSHEY_DUPLEX, side / 90, 40, 2)
    cv2.putText(tag, "LAB", (int(side * .25), int(side * .80)), cv2.FONT_HERSHEY_DUPLEX, side / 90, 40, 2)
    cv2.circle(tag, (int(side * .82), int(side * .2)), max(2, side // 14), 90, -1)
    tag = np.ascontiguousarray(tag[:, ::-1])   # desk x runs against image right in this view: un-mirror the text
    cx, cy = int((label_at[0] - X0) * 1000 / MM_PER_PX), int((label_at[1] - Y0) * 1000 / MM_PER_PX)
    desk[cy - side // 2:cy - side // 2 + side, cx - side // 2:cx - side // 2 + side] = tag
    return desk


class SimCamera:
    def __init__(self, label_view: dict, ranges=None, label_uv=(0.6, 0.45)):
        """label_view: joints of the view (top) from which the label is seen at image point label_uv."""
        pose = framing.camera_pose(label_view, ranges)
        d = framing.ray(*label_uv, pose, W / H)
        cam = pose[0]
        t = -cam[2] / d[2]
        self.label_at = (cam[0] + d[0] * t, cam[1] + d[1] * t)
        self.desk = _desk(self.label_at)
        th = math.tan(math.radians(framing.HFOV_DEG) / 2)
        u = (np.arange(W) + 0.5) / W * 2 - 1
        v = (np.arange(H) + 0.5) / H * 2 - 1
        self.uu, self.vv = np.meshgrid(u * th, v * th * H / W)

    def render(self, joints: dict, ranges=None) -> bytes:
        cam, axis, right, down = (np.array(x) for x in framing.camera_pose(joints, ranges))
        d = axis[None, None, :] + self.uu[..., None] * right + self.vv[..., None] * down
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(d[..., 2] < -1e-6, -cam[2] / d[..., 2], np.nan)
        x, y = cam[0] + d[..., 0] * t, cam[1] + d[..., 1] * t
        mx = ((x - X0) * 1000 / MM_PER_PX).astype(np.float32)
        my = ((y - Y0) * 1000 / MM_PER_PX).astype(np.float32)
        img = cv2.remap(self.desk, np.nan_to_num(mx, nan=-1), np.nan_to_num(my, nan=-1), cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=105)
        img[np.isnan(t)] = 190                                   # not looking at the desk: wall
        cv2.putText(img, "SIMULATED", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 255, 2)
        ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
        return jpeg.tobytes()
