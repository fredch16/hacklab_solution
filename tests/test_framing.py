"""framing.py: camera model round trips, framing results and refusals. No hardware.

    python3 tests/test_framing.py        # about 20 s, ends with ALL OK

These check that the preview is consistent with fk.py's model (the target really looks at the
object, the checks refuse what they should). They cannot check that the model matches the real
arm: camera offset, tilt, field of view and table height are estimates (see framing.py).
"""
import math, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fk
import framing
from framing import FramingError, camera_pose, plan, project, ray

views = fk.load_views()
ranges = fk.FOLLOWER_RANGES
passed = 0


def check(cond, what):
    global passed
    assert cond, what
    passed += 1


def table_point(pose, u, v, aspect):
    cam = pose[0]
    d = ray(u, v, pose, aspect)
    t = (framing.TABLE_Z - cam[2]) / d[2]
    return tuple(cam[i] + d[i] * t for i in range(3))


# 1. image size straight from the JPEG
size = framing.jpeg_size((ROOT / "views" / "top.jpg").read_bytes())
check(size and size[0] > size[1] > 0, f"jpeg_size of views/top.jpg: {size}")
aspect = size[0] / size[1]
print(f"ok  jpeg_size: views/top.jpg is {size[0]}x{size[1]}")

# 2. ray and project are inverse to each other
pose = camera_pose(views["top"], ranges)
worst = 0.0
for u in (0.1, 0.3, 0.5, 0.7, 0.9):
    for v in (0.1, 0.5, 0.9):
        p = table_point(pose, u, v, aspect)
        uv = project(p, pose, aspect)
        worst = max(worst, abs(uv[0] - u), abs(uv[1] - v))
check(worst < 1e-9, f"ray/project round trip off by {worst}")
check(pose[1][2] < -0.9, "top looks down")
print("ok  ray -> table -> project returns the same image point; top looks straight down")

# 3. 'center' on a box right of the middle: the target looks at the same table point, from about as far
box = {"cx": 0.62, "cy": 0.55, "w": 0.3, "h": 0.25}
r = plan(views["top"], box, framing="center", aspect=aspect, ranges=ranges)
p = tuple(r["table_point_m"])
tpose = camera_pose({**views["top"], **r["target_joints"]}, ranges)
uv = project(p, tpose, aspect)
check(abs(uv[0] - 0.5) < 0.03 and abs(uv[1] - 0.5) < 0.03, f"centred object projects to {uv}")  # solver tolerance: ~4 mm, ~0.3 deg
check(abs(r["camera_distance_m"] - r["camera_distance_now_m"]) < 0.05, "center keeps the distance (roughly)")
print(f"ok  center: object moves to image point ({uv[0]:.3f}, {uv[1]:.3f}), distance "
      f"{r['camera_distance_now_m']} -> {r['camera_distance_m']} m")

# 4. 'fill' brings the camera closer, the object gets bigger but stays inside the margin
r = plan(views["top"], {"cx": 0.5, "cy": 0.5, "w": 0.45, "h": 0.4}, framing="fill", aspect=aspect, ranges=ranges)
check(r["ok"] and r["camera_distance_m"] < r["camera_distance_now_m"], "fill moves closer")
grow = r["camera_distance_now_m"] / r["camera_distance_m"]
check(0.45 * grow <= 1 - r["margin"] + 1e-6, "fill keeps the margin")
check(r["min_height_m"]["target"] >= r["min_height_m"]["floor"], "target above the height floor")
print(f"ok  fill: {r['camera_distance_now_m']} -> {r['camera_distance_m']} m (object {grow:.2f}x larger), "
      f"lowest point {r['min_height_m']['target'] * 100:.1f} cm >= floor {r['min_height_m']['floor'] * 100:.1f} cm")

# 5. a tiny object: fill backs off to what is safe instead of failing
r = plan(views["top"], {"cx": 0.5, "cy": 0.5, "w": 0.05, "h": 0.05}, framing="fill", aspect=aspect, ranges=ranges)
check(r["ok"] and any("backed off" in n or "limited" in n for n in r["notes"]), r["notes"])
print("ok  tiny object: limited / backed off ->", r["camera_distance_m"], "m")

# 6. every accepted target respects joint travel and calibrated ranges
for name in ("top", "wide", "close"):
    for cx, cy in ((0.4, 0.5), (0.6, 0.6)):
        try:
            r = plan(views[name], {"cx": cx, "cy": cy, "w": 0.3, "h": 0.3}, aspect=aspect, ranges=ranges)
        except FramingError as e:
            check(e.code in ("unreachable", "at_limit", "outside_envelope", "too_low", "out_of_reach", "not_on_table"),
                  e.code)
            continue
        check(all(abs(t) <= framing.MAX_JOINT_TRAVEL for t in r["joint_travel"].values()), "travel")
        check(all(framing._in_range(r["target_joints"][m], *ranges[m]) for m in framing.MOVABLE), "ranges")
        check(r["min_height_m"]["target"] >= r["min_height_m"]["floor"], "height")
print("ok  accepted targets stay within travel, calibrated ranges and the height floor")

# 7. refusals
for joints, box, codes in [
    (views["right"], {"cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.3}, ("out_of_reach", "not_on_table")),
    (views["wide"], {"cx": 0.5, "cy": 0.0, "w": 0.1, "h": 0.1}, ("out_of_reach", "not_on_table", "at_limit",
                                                                 "outside_envelope", "unreachable")),
    (views["top"], {"cx": 1.5, "cy": 0.5, "w": 0.1, "h": 0.1}, ("invalid_request",)),
    (views["top"], {"cx": 0.5}, ("invalid_request",)),
]:
    try:
        plan(joints, box, aspect=aspect, ranges=ranges)
        raise AssertionError(f"expected a refusal for {box}")
    except FramingError as e:
        check(e.code in codes, f"{box}: {e.code} not in {codes}")
try:
    plan(views["top"], {"cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}, framing="zoom")
    raise AssertionError("framing 'zoom' accepted")
except FramingError as e:
    check(e.code == "invalid_request", e.code)
print("ok  refusals: sideways view out of reach, sky, box outside 0..1, missing values, unknown framing")

print(f"ALL OK ({passed} checks)")
