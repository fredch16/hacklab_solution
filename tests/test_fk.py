"""fk.py: steps->angle mapping, kinematic invariants, and what the saved views should look like.

    python3 tests/test_fk.py

The poses below are a copy of views.json as captured on the follower 5B79016329 on 21 Nov
(with the calibrated ranges of that moment), so these checks keep meaning the same thing when
views are re-captured. The current views.json is only reported on at the end, not asserted.
"""
import math, random, struct, sys
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fk

RANGES = {1: (1105, 2991), 2: (1071, 3025), 3: (924, 3172), 4: (897, 3199),
          5: (141, 3955), 6: (1017, 3079), 7: (164, 3932), 8: (1263, 2833)}
VIEWS = {
    "home":     {1: 2095, 2: 1061, 3: 1906, 4: 3196, 5: 1899, 6: 1912, 7: 2303, 8: 1279},
    "left":     {1: 2547, 2: 3032, 3: 1444, 4: 1652, 5: 2949, 6: 3069, 7: 2268, 8: 1279},
    "right":    {1: 1675, 2: 3032, 3: 1381, 4: 1655, 5: 909, 6: 3071, 7: 2251, 8: 1279},
    "top":      {1: 2120, 2: 2555, 3: 1522, 4: 1661, 5: 1899, 6: 2991, 7: 2250, 8: 1279},
    "wide":     {1: 2117, 2: 1780, 3: 1521, 4: 2124, 5: 1900, 6: 2993, 7: 2251, 8: 1279},
    "close":    {1: 2104, 2: 2548, 3: 1522, 4: 2133, 5: 1941, 6: 2553, 7: 2261, 8: 1279},
    "side_low": {1: 2518, 2: 3023, 3: 1668, 4: 1545, 5: 2923, 6: 2957, 7: 2173, 8: 1279},
    "transit":  {1: 2114, 2: 1906, 3: 1585, 4: 1527, 5: 1958, 6: 2064, 7: 2216, 8: 1758},
}
MID = {m: (lo + hi) / 2 for m, (lo, hi) in RANGES.items()}
# URDF <limit lower/upper> as read by eye from elrobot_follower.urdf
LIMITS = {1: (-1.5509, 1.5509), 2: (-1.6122, 1.6122), 3: (-1.7610, 1.7610), 4: (-1.7533, 1.7533),
          5: (-2.62, 3.252), 6: (-1.3775, 1.7641), 7: (-3.2014, 2.7336), 8: (0.0, 2.2028)}
MAPPINGS = ("viewer", "true_scale")


def close_to(a, b, tol=1e-9):
    return abs(a - b) <= tol


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def elevation(v):
    return math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))


def joint(m):
    return f"rev_motor_{m:02d}"


# ---- mapping: what the viewer does -------------------------------------------------------------
for m in range(1, 8):                              # M1-M7: plain linear map min->lower, max->upper
    lo, hi = RANGES[m]
    for pos, want in ((lo, LIMITS[m][0]), (hi, LIMITS[m][1]), ((lo + hi) / 2, sum(LIMITS[m]) / 2)):
        got = fk.steps_to_angles({**MID, m: pos}, RANGES)[joint(m)]
        assert close_to(got, want, 1e-12), (m, pos, got, want)
# a value worked out by hand: home M2 = 1061, range 1071..3025, limits +-1.6122
assert close_to(fk.steps_to_angles(VIEWS["home"], RANGES)[joint(2)], -1.6122 + (1061 - 1071) / 1954 * 3.2244, 1e-12)
# more steps = larger URDF angle for M1-M7 (no inversion; MOTION.md's "upper - jointPosition" is gripper only)
for m in range(1, 8):
    a = fk.steps_to_angles({**MID, m: MID[m] + 100}, RANGES)[joint(m)]
    assert a > fk.steps_to_angles(MID, RANGES)[joint(m)]

# M8 is the inverted one: range minimum = joint at its upper limit = jaws fully travelled (closed)
closed = fk.steps_to_angles({**MID, 8: 1263}, RANGES)
opened = fk.steps_to_angles({**MID, 8: 2833}, RANGES)
assert close_to(closed["rev_motor_08"], 2.2028) and close_to(opened["rev_motor_08"], 0.0)
assert close_to(closed["rev_motor_08_1"], -0.0255) and close_to(closed["rev_motor_08_2"], 0.0255)
assert close_to(opened["rev_motor_08_1"], 0.0) and close_to(opened["rev_motor_08_2"], 0.0)
assert closed == fk.steps_to_angles({m: v for m, v in MID.items() if m != 8} | {8: 1263}, RANGES)
no_gripper = {m: v for m, v in MID.items() if m != 8}
assert fk.steps_to_angles(no_gripper, RANGES)["rev_motor_08"] == closed["rev_motor_08"]   # missing M8 = closed
for bad in ({m: v for m, v in MID.items() if m != 3}, ):
    try:
        fk.steps_to_angles(bad, RANGES); raise AssertionError("missing motor 3 must raise")
    except ValueError:
        pass

# clamping: the viewer cuts off outside the calibrated arc, the default extrapolates
below = {**MID, 2: 1061}
assert close_to(fk.steps_to_angles(below, RANGES, clamp=True)[joint(2)], -1.6122)
assert fk.steps_to_angles(below, RANGES)[joint(2)] < -1.6122
assert abs(fk.steps_to_angles(below, RANGES)[joint(2)] + 1.6122) < math.radians(1.0)

# wrap-aware normalisation, same arithmetic as the viewer (4095, not 4096)
assert fk.normalise(3500, 3500, 500) == 0.0
assert close_to(fk.normalise(4000, 3500, 500), 500 / 1095)
assert close_to(fk.normalise(100, 3500, 500), (4095 - 3500 + 100) / 1095)
assert close_to(fk.normalise(500, 3500, 500), 1.0)
assert fk.normalise(2000, 3500, 500, clamp=True) in (0.0, 1.0)
assert fk.normalise(3490, 3500, 500) < 0 and fk.normalise(510, 3500, 500) > 1          # just outside either end
assert close_to(fk.normalise(1500, 1000, 3000), 0.25) and fk.normalise(5, 7, 7) == 0.0
assert close_to(fk.normalise(2000, 0, 0), 2000 / 4095)                                  # `rangeMax || 4095`

# round trip steps -> angles -> steps
for name, pose in VIEWS.items():
    assert fk.angles_to_steps(fk.steps_to_angles(pose, RANGES), RANGES) == pose, name

# the alternative mapping: one step is 360/4096 degrees, anchored at the calibrated maximum
for m in range(1, 8):
    lo, hi = RANGES[m]
    ts = lambda pos: fk.steps_to_angles({**MID, m: pos}, RANGES, mapping="true_scale")[joint(m)]
    assert close_to(ts(hi), LIMITS[m][1], 1e-12)
    assert close_to(ts(hi) - ts(hi - 1024), math.pi / 2, 1e-12)
assert fk.steps_to_angles(MID, RANGES, mapping="true_scale")["rev_motor_08"] == fk.steps_to_angles(MID, RANGES)["rev_motor_08"]

# ---- kinematics --------------------------------------------------------------------------------
mdl = fk.model()
urdf = ET.parse(fk.URDF_PATH).getroot()

# zero pose: this URDF is a CAD export where every link's visual origin is minus the link's position
zero = {j: 0.0 for j in mdl.joints}
link_pose, joint_pos = mdl.frames(zero)
for link in urdf.findall("link"):
    want = tuple(-float(x) for x in link.find("visual/origin").get("xyz").split())
    rot, pos = link_pose[link.get("name")]
    # (the hand-edited jaw joints are 1.9 mm off that rule for Jaw_01; the viewer draws it the same way)
    assert dist(pos, want) < (0.002 if "Jaw" in link.get("name") else 2e-6), (link.get("name"), pos, want)
    assert rot == fk._I3
assert dist(link_pose[fk.GRIPPER_LINK][1], (0.002925, 0.259566, 0.218892)) < 2e-6        # arm up 22 cm, then 26 cm forward

# one joint turned, against a transform written out independently (vector form of Rodrigues)
def turn(point, pivot, axis, angle):
    n = math.sqrt(sum(a * a for a in axis)); k = [a / n for a in axis]
    v = [p - c for p, c in zip(point, pivot)]
    kxv = (k[1] * v[2] - k[2] * v[1], k[2] * v[0] - k[0] * v[2], k[0] * v[1] - k[1] * v[0])
    kv = sum(a * b for a, b in zip(k, v))
    c, s = math.cos(angle), math.sin(angle)
    return tuple(pivot[i] + v[i] * c + kxv[i] * s + k[i] * kv * (1 - c) for i in range(3))

gb0 = link_pose[fk.GRIPPER_LINK][1]
for jname, axis, angle in (("rev_motor_01", (0.0, -0.008727, 0.999962), 0.7), ("rev_motor_02", (-0.999962, -7.6e-05, 0.008726), -1.1),
                           ("rev_motor_04", (-0.99981, 0.008574, 0.017527), 0.9), ("rev_motor_06", (-0.999697, 0.0173, 0.017525), 1.3)):
    got = mdl.frames({**zero, jname: angle})[0][fk.GRIPPER_LINK][1]
    assert dist(got, turn(gb0, joint_pos[jname], axis, angle)) < 1e-9, jname
# two joints: turn about the outer one first, then carry everything round the inner one
got = mdl.frames({**zero, "rev_motor_02": 0.5, "rev_motor_06": -0.8})[0][fk.GRIPPER_LINK][1]
want = turn(turn(gb0, joint_pos["rev_motor_06"], (-0.999697, 0.0173, 0.017525), -0.8),
            joint_pos["rev_motor_02"], (-0.999962, -7.6e-05, 0.008726), 0.5)
assert dist(got, want) < 1e-9
# directions in plain words: +M1 swings the arm from +y towards -x; +M2 tips the upright arm forward (+y) and down
q1 = mdl.frames({**zero, "rev_motor_01": math.pi / 2})[0][fk.GRIPPER_LINK][1]
assert q1[0] < -0.2 and abs(q1[1]) < 0.06 and abs(q1[2] - gb0[2]) < 0.005
tipped, tipped_joints = mdl.frames({**zero, "rev_motor_02": math.pi / 2})
assert tipped_joints["rev_motor_03"][1] > joint_pos["rev_motor_03"][1] + 0.05          # elbow goes forward
assert abs(tipped_joints["rev_motor_03"][2] - joint_pos["rev_motor_02"][2]) < 0.01     # ... to shoulder height
assert tipped[fk.GRIPPER_LINK][1][2] < gb0[2] - 0.1                                    # and the gripper comes down
# the roll joints turn about the forearm / gripper axis: the gripper origin hardly moves
for jname in ("rev_motor_05", "rev_motor_07"):
    assert dist(mdl.frames({**zero, jname: 1.0})[0][fk.GRIPPER_LINK][1], gb0) < 0.012, jname

# link lengths do not depend on the pose
random.seed(7)
poses = list(VIEWS.values()) + [{m: random.uniform(lo, hi) for m, (lo, hi) in RANGES.items()} for _ in range(40)]
chain = ["j1_base_yaw", "j2_shoulder", "j3_elbow", "j4_pitch", "j5_forearm_roll", "j6_wrist_pitch", "j7_wrist_roll", "j8_gripper"]
pairs = list(zip(chain, chain[1:])) + [("j7_wrist_roll", "jaw_tip"), ("j7_wrist_roll", "camera"), ("j8_gripper", "jaw_tip"), ("jaw_tip", "camera")]
ref = fk.fk(MID, RANGES)
for pose in poses:
    for mp in MAPPINGS:
        pts = fk.fk(pose, RANGES, mapping=mp)
        for a, b in pairs:
            assert close_to(dist(pts[a], pts[b]), dist(ref[a], ref[b]), 1e-9), (a, b)
        assert pts["j1_base_yaw"] == ref["j1_base_yaw"] and close_to(pts["j2_shoulder"][2], ref["j2_shoulder"][2], 5e-4)   # M1's axis is 0.5 deg off vertical in the URDF
        x, f, u = fk.gripper_frame(pose, RANGES, mapping=mp)
        for v in (x, f, u):
            assert close_to(sum(c * c for c in v), 1.0, 1e-9)
        assert abs(sum(a * b for a, b in zip(x, f))) < 1e-9 and abs(sum(a * b for a, b in zip(f, u))) < 1e-9
        assert fk.camera_direction(pose, RANGES, mapping=mp) == f
        ax = fk.camera_optical_axis(pose, RANGES, mapping=mp)
        assert close_to(math.degrees(math.acos(sum(a * b for a, b in zip(ax, f)))), fk.CAMERA_TILT_DEG, 1e-6)
assert close_to(dist(ref["j7_wrist_roll"], ref["jaw_tip"]), 0.104) and 0.06 < dist(ref["j7_wrist_roll"], ref["camera"]) < 0.09
assert 0.11 < dist(ref["j4_pitch"], ref["j6_wrist_pitch"]) < 0.14                       # forearm about 12.6 cm

# ---- up axis and base plane --------------------------------------------------------------------
# the viewer sets robot.rotation = Euler XYZ(-pi/2, 0, -pi/2), i.e. R = Rx(-90) Rz(-90); three.js up is +Y
rx = fk._axis_angle((1.0, 0.0, 0.0), -math.pi / 2)
rz = fk._axis_angle((0.0, 0.0, 1.0), -math.pi / 2)
up_in_viewer = fk._mat_vec(fk._mat_mul(rx, rz), (0.0, 0.0, 1.0))
assert dist(up_in_viewer, (0.0, 1.0, 0.0)) < 1e-12                                       # URDF +z is the viewer's up
base = fk.URDF_PATH.parent / "assets" / "base_link.stl"
zs = [v[2] for v in fk._stl_vertices(base)]
assert abs(min(zs)) * 0.001 < 1e-5 and 0.05 < max(zs) * 0.001 < 0.08                     # base mesh stands on z = 0

# ---- meshes ------------------------------------------------------------------------------------
pts_by_link = fk.mesh_points()
assert "base_link" not in pts_by_link and "Joint_01_1" not in pts_by_link and "ST3215_2_v1_1" not in pts_by_link
assert {"Joint_02_1", "Gripper_Base_v1_1", "Gripper_Jaw_01_v1_1", "Gripper_Jaw_02_v1_1", "ST3215_8_v1_1"} <= set(pts_by_link)
# the reduced point set finds the lowest vertex of a link to within 2 mm, in any orientation
for link in ("Gripper_Base_v1_1", "Gripper_Jaw_02_v1_1", "Joint_04_v1_1"):
    info = mdl.links[link]
    full = [tuple(c * 0.001 + o for c, o in zip(v, info["xyz"])) for v in fk._stl_vertices(info["mesh"])]
    for _ in range(25):
        d = fk._unit((random.gauss(0, 1), random.gauss(0, 1), random.gauss(0, 1)))
        exact = min(sum(a * b for a, b in zip(d, v)) for v in full)
        approx = min(sum(a * b for a, b in zip(d, v)) for v in pts_by_link[link])
        assert 0 <= approx - exact < 0.002, (link, approx - exact)
# JAW_TIP_OFFSET: the closed jaws end 104 mm ahead of the gripper origin, centred on its axis
closed_pose = mdl.frames(fk.steps_to_angles({**MID, 8: 1263}, RANGES) | {joint(m): 0.0 for m in range(1, 8)})[0]
gx, gf, gu = mdl.gripper_axes()
origin = closed_pose[fk.GRIPPER_LINK][1]
reach, side, height = [], [], []
for jaw in ("Gripper_Jaw_01_v1_1", "Gripper_Jaw_02_v1_1"):
    rot, pos = closed_pose[jaw]
    for v in pts_by_link[jaw]:
        rel = [a + b - c for a, b, c in zip(fk._mat_vec(rot, v), pos, origin)]
        reach.append(sum(a * b for a, b in zip(rel, gf)))
        side.append(sum(a * b for a, b in zip(rel, gx)))
        height.append(sum(a * b for a, b in zip(rel, gu)))
assert abs(max(reach) - fk.JAW_TIP_OFFSET[1]) < 0.002
assert abs(max(side) + min(side)) < 0.01 and abs(max(height) + min(height)) < 0.01
assert abs(gf[1] - 1) < 0.001 and abs(gx[0] - 1) < 0.001 and abs(gu[2] - 1) < 0.001      # gripper frame ~ link frame

# ---- heights and paths -------------------------------------------------------------------------
h, name = fk.min_height(VIEWS["close"], RANGES)
assert name.startswith("mesh:Gripper_Jaw") and h <= fk.min_height(VIEWS["close"], RANGES, meshes=False)[0]
assert fk.min_height(VIEWS["close"], RANGES, meshes=False)[1] == "jaw_tip"
assert fk.min_height(VIEWS["top"], RANGES, mapping="worst")[0] == min(fk.min_height(VIEWS["top"], RANGES, mapping=mp)[0] for mp in MAPPINGS)
hp = fk.path_min_height(VIEWS["home"], VIEWS["home"], RANGES)
assert hp[:2] == fk.min_height(VIEWS["home"], RANGES)
assert fk.path_min_height(VIEWS["home"], VIEWS["top"], RANGES, moving_ids=[])[:2] == fk.min_height(VIEWS["home"], RANGES)
both = fk.path_min_height(VIEWS["wide"], VIEWS["close"], RANGES)
assert both[0] <= min(fk.min_height(VIEWS["wide"], RANGES)[0], fk.min_height(VIEWS["close"], RANGES)[0]) + 1e-12
# only the listed motors move: M2 alone from wide towards close ends at wide with close's M2
only2 = fk.path_min_height(VIEWS["wide"], VIEWS["close"], RANGES, moving_ids=[2], samples=1)
ends = [fk.min_height(VIEWS["wide"], RANGES), fk.min_height({**VIEWS["wide"], 2: VIEWS["close"][2]}, RANGES)]
assert only2[:2] == min(ends) and only2[2] in (0.0, 1.0)
assert fk.path_min_height(VIEWS["wide"], VIEWS["close"], RANGES, samples=40)[0] <= both[0] + 0.003       # finer sampling agrees
for bad_kw in ({"samples": 0}, {"moving_ids": [9]}, {"mapping": "nope"}):
    try:
        fk.path_min_height(VIEWS["home"], VIEWS["top"], RANGES, **bad_kw); raise AssertionError(bad_kw)
    except ValueError:
        pass
# the camera counts as a ball: with the gripper rolled over in side_low it is the lowest key point
cam_low = fk.min_height(VIEWS["side_low"], RANGES, meshes=False)
assert cam_low[1] == "camera" and close_to(cam_low[0], fk.fk(VIEWS["side_low"], RANGES)["camera"][2] - fk.CAMERA_RADIUS)
# string motor ids (as in views.json) work the same
assert fk.min_height({str(m): v for m, v in VIEWS["top"].items()}, {str(m): r for m, r in RANGES.items()}) == fk.min_height(VIEWS["top"], RANGES)
assert fk.min_height(VIEWS["top"]) == fk.min_height(VIEWS["top"], RANGES)                # default ranges = the snapshot
assert fk.FOLLOWER_RANGES == RANGES

# route_min_height: segment by segment, each moving only its own motors from where the last one ended
route = [("lift", {2: VIEWS["transit"][2]}), ("fold", {m: VIEWS["transit"][m] for m in (3, 4, 6)}),
         ("turn", {m: VIEWS["left"][m] for m in (1, 5, 7)})]
segs = fk.route_min_height(VIEWS["close"], route, RANGES)
assert [s[0] for s in segs] == ["lift", "fold", "turn"]
lifted = {**VIEWS["close"], 2: VIEWS["transit"][2]}
folded = {**lifted, **{m: VIEWS["transit"][m] for m in (3, 4, 6)}}
assert segs[0][1:] == fk.path_min_height(VIEWS["close"], VIEWS["transit"], RANGES, moving_ids=[2])
assert segs[1][1:] == fk.path_min_height(lifted, VIEWS["transit"], RANGES, moving_ids=[3, 4, 6])
assert segs[2][1:] == fk.path_min_height(folded, VIEWS["left"], RANGES, moving_ids=[1, 5, 7])
assert segs[0][1] == fk.min_height(VIEWS["close"], RANGES)[0] and segs[0][3] == 0.0     # lifting only gains height
assert fk.route_min_height(VIEWS["close"], [], RANGES) == []
assert fk.route_min_height({str(m): v for m, v in VIEWS["close"].items()}, [("lift", {"2": VIEWS["transit"][2]})], RANGES) == segs[:1]
worst = fk.route_min_height(VIEWS["close"], route, RANGES, mapping="worst")
assert all(w[1] <= s[1] for w, s in zip(worst, segs))

# check_path: through the raised transit pose is fine, swinging straight from left to right is not
ok = fk.check_path(VIEWS["home"], VIEWS["transit"], RANGES)
assert ok["ok"] and ok["height"] > fk.RECOMMENDED_MARGIN_M
bad = fk.check_path(VIEWS["left"], VIEWS["right"], RANGES)
assert not bad["ok"] and bad["height"] < 0 and 0.2 < bad["t"] < 0.8, bad
same = fk.check_path(VIEWS["side_low"], VIEWS["side_low"], RANGES)                       # a taught pose inside the margin
assert same["ok"] and same["height"] < fk.RECOMMENDED_MARGIN_M

# ---- the saved views, as loose statements about the real poses ---------------------------------
for mp in MAPPINGS:
    P = {n: fk.fk(p, RANGES, mapping=mp) for n, p in VIEWS.items()}
    G = {n: fk.gripper_frame(p, RANGES, mapping=mp) for n, p in VIEWS.items()}
    CAM = {n: fk.camera_optical_axis(p, RANGES, mapping=mp) for n, p in VIEWS.items()}
    # the arm was physically in every one of these poses, so nothing may be under the table
    for n, p in VIEWS.items():
        assert fk.min_height(p, RANGES, mapping=mp)[0] > 0, (mp, n, fk.min_height(p, RANGES, mapping=mp))
    # top / close: camera looks roughly straight down (the gripper itself points ~60-65 deg down, the camera is tilted)
    for n in ("top", "close"):
        assert elevation(CAM[n]) < -70, (mp, n, elevation(CAM[n]))
        assert -75 < elevation(G[n][1]) < -50, (mp, n, elevation(G[n][1]))
    # close is lower than top; wide is higher and further back than top
    assert P["close"]["camera"][2] < P["top"]["camera"][2] - 0.05 and P["close"]["jaw_tip"][2] < P["top"]["jaw_tip"][2] - 0.05
    assert P["wide"]["camera"][2] > P["top"]["camera"][2] + 0.08 and P["wide"]["camera"][1] < P["top"]["camera"][1] - 0.1
    assert elevation(CAM["top"]) < elevation(CAM["wide"]) < -45                           # wide still looks down, less steeply
    # home: folded close to the base, gripper about level, camera upright (its side of the gripper faces up)
    assert abs(elevation(G["home"][1])) < 15 and G["home"][2][2] > 0.9, (mp, elevation(G["home"][1]))
    assert math.hypot(*P["home"]["j7_wrist_roll"][:2]) < 0.15 < math.hypot(*P["top"]["j7_wrist_roll"][:2])
    assert -45 < elevation(CAM["home"]) < -15                                              # horizon in the upper part of home.jpg
    # left / right: on opposite sides, each looking across towards the other side, roughly level, ~45 deg in yaw
    assert P["left"]["camera"][0] < -0.08 and P["right"]["camera"][0] > 0.05
    assert G["left"][1][0] > 0.6 and G["right"][1][0] < -0.6
    assert abs(elevation(G["left"][1])) < 15 and abs(elevation(G["right"][1])) < 15
    yaw_l, yaw_r = math.degrees(fk.steps_to_angles(VIEWS["left"], RANGES, mapping=mp)[joint(1)]), math.degrees(fk.steps_to_angles(VIEWS["right"], RANGES, mapping=mp)[joint(1)])
    assert 30 < yaw_l < 60 and -50 < yaw_r < -20
    roll_l, roll_r = math.degrees(fk.steps_to_angles(VIEWS["left"], RANGES, mapping=mp)[joint(5)]), math.degrees(fk.steps_to_angles(VIEWS["right"], RANGES, mapping=mp)[joint(5)])
    assert 80 < roll_l < 110 and -100 < roll_r < -70
    # left.jpg has the table on the left of the picture (up = image right), right.jpg the other way round
    right_of_image = lambda n: fk._cross(G[n][1], G[n][2])
    assert right_of_image("left")[2] > 0.9 and right_of_image("right")[2] < -0.9
    assert abs(right_of_image("top")[2]) < 0.2 and right_of_image("wide")[0] > 0.9        # un-rolled views: image right = +x
    # side_low is the lowest camera position, transit the highest point of the gripper, pointing up
    assert P["side_low"]["camera"][2] == min(p["camera"][2] for p in P.values()) and P["side_low"]["camera"][2] < 0.06
    assert P["transit"]["jaw_tip"][2] == max(p["jaw_tip"][2] for p in P.values()) and elevation(G["transit"][1]) > 70
    assert elevation(CAM["transit"]) > 40                                                  # transit.jpg shows the ceiling
# M8: 1279 is closed in every camera view, 1758 (transit.jpg) shows the jaws apart
gap = lambda pose: dist(*(mdl.frames(fk.steps_to_angles(pose, RANGES))[0][j][1] for j in ("Gripper_Jaw_01_v1_1", "Gripper_Jaw_02_v1_1")))
assert gap(VIEWS["transit"]) != gap(VIEWS["top"])
slide = lambda pose: abs(fk.steps_to_angles(pose, RANGES)["rev_motor_08_2"])
assert slide(VIEWS["top"]) > 0.025 and 0.015 < slide(VIEWS["transit"]) < 0.02

# ---- per-part heights and self-clearance (used by motion.direct_plan) ---------------------------
for n, pose in VIEWS.items():
    for mp in ("viewer", "true_scale"):
        parts = fk.part_heights(pose, RANGES, mp)
        assert close_to(min(parts.values()), fk.min_height(pose, RANGES, mapping=mp)[0]), n
        assert close_to(min(fk.part_heights(pose, RANGES, mp, meshes=False).values()),
                        fk.min_height(pose, RANGES, meshes=False, mapping=mp)[0]), n
        assert not any(k in parts for k in fk.STATIC_POINTS) and any(k.startswith("mesh:") for k in parts)
        # the folded rest pose is the only one where the arm comes close to itself
        gap = fk.self_clearance(pose, RANGES, mp)
        assert (gap < 0.05) == (n == "home"), (n, gap)
# M1 turns about the vertical (to half a degree in the URDF): 35 degrees of yaw change heights by
# under 2 mm and do not change how close the arm is to itself
turned = {**VIEWS["top"], 1: VIEWS["top"][1] + 400}
assert all(close_to(a, b, 0.002) for a, b in zip(fk.part_heights(turned, RANGES).values(), fk.part_heights(VIEWS["top"], RANGES).values()))
assert close_to(fk.self_clearance(turned, RANGES), fk.self_clearance(VIEWS["top"], RANGES))

# ---- the views that are in views.json right now: report only -----------------------------------
try:
    current = fk.load_views()
except (OSError, KeyError, ValueError) as e:
    current = {}
    print(f"note: could not read views.json ({e})")
for n, pose in current.items():
    h, where = fk.min_height(pose, mapping="worst")
    note = "" if h > 0 else "   <-- below the base plane in this model"
    changed = "" if VIEWS.get(n) == pose else "   (not one of the poses checked above)"
    print(f"  views.json {n:9s} lowest {where} {h * 100:+5.1f} cm{note}{changed}")

print("fk OK")
