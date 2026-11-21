"""Forward kinematics for the NormaCore ElRobot follower, for an advisory "does this path
dip below the table?" check. Standard library only.

    python3 fk.py                 # table for every view in views.json, then the planned routes
    python3 fk.py --path home top # lowest point along a straight joint-space move
    python3 fk.py --live          # read the real arm (read-only) and print its pose

World frame = the URDF's base_link frame, in metres:
  +z is up, z = 0 is the plane the base stands on, +y is "forward" (where the arm points at
  zero angles), +x is to the right when you stand behind the arm.
  Why: Station's viewer rotates the robot root by Euler XYZ (-90deg, 0, -90deg), which maps
  URDF +z onto three.js +Y (its up axis, the grid is at Y = 0), and the lowest vertex of
  base_link.stl is at z = 0.0000. Both are checked in tests/test_fk.py.

Steps -> angle mapping (derived from station-viewer v0.1.0-beta.10, BaseRobotRenderer.tsx
`Arm.setMotorPosition` / `updateRender`, elrobot/config.ts, urdf-loader 0.12.7):
  p     = (pos - range_min) / (range_max - range_min)        (wrap-aware, see normalise())
  M1-M7:  angle = lower + p * (upper - lower)                 URDF <limit> of rev_motor_0N
  M8:     rev_motor_08   = upper * (1 - p)                    ("upper - jointPosition")
          rev_motor_08_1 = -0.0255 * (1 - p)   rev_motor_08_2 = +0.0255 * (1 - p)   (jaws, m)
The `upper - jointPosition` inversion in config.ts only runs for the gripper's joint-name
array; motors 1-7 are a plain linear map. The viewer also clamps p to [0, 1]; we do not by
default, because the real arm does sit a few steps outside its calibrated range.

How far to trust it: the mapping reproduces the viewer's code exactly, and the signs of yaw and
forearm roll agree with the saved camera snapshots. Absolute heights are NOT verified by any
measurement. The viewer stretches each calibrated arc onto the full URDF range, and for M2
(shoulder, the long lever) the calibrated arc is 7% shorter than that range, so shoulder angles
can be off by several degrees; the snapshots of `top` and `close` suggest the real gripper is
2-3 cm lower than this mapping says. mapping="true_scale" / "worst" and RECOMMENDED_MARGIN_M
exist for that reason. Advisory only: never the sole protection for the hardware.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent
URDF_PATH = REPO / "vendor" / "normacore" / "hardware" / "elrobot" / "simulation" / "elrobot_follower.urdf"
VIEWS_PATH = REPO / "views.json"

# Calibrated (min, max) encoder steps of the follower 5B79016329, snapshot from docs/HARDWARE.md
# (21 Nov 13:20). Prefer live `Arm.ranges()`: recalibrating changes these and with them every angle.
FOLLOWER_RANGES: dict[int, tuple[int, int]] = {
    1: (1105, 2991), 2: (1071, 3025), 3: (924, 3172), 4: (897, 3199),
    5: (141, 3955), 6: (1017, 3079), 7: (164, 3932), 8: (1263, 2833),
}

STEPS_PER_TURN = 4096
_VIEWER_MAX_STEP = 4095            # the viewer's wrap arithmetic uses 4095, not 4096; kept as is
MOTOR_JOINTS = {m: f"rev_motor_{m:02d}" for m in range(1, 9)}
GRIPPER_MOTOR = 8
JAW_JOINTS = ("rev_motor_08_1", "rev_motor_08_2")

# Key points reported by fk(). Names say which motor's axis the point sits on.
JOINT_POINTS = {
    1: "j1_base_yaw", 2: "j2_shoulder", 3: "j3_elbow", 4: "j4_pitch",
    5: "j5_forearm_roll", 6: "j6_wrist_pitch", 7: "j7_wrist_roll", 8: "j8_gripper",
}
# j1 and j2 cannot change height (M1 turns about the vertical), so they are never "the lowest point".
STATIC_POINTS = ("j1_base_yaw", "j2_shoulder")
GRIPPER_LINK = "Gripper_Base_v1_1"
FIRST_MOVING_JOINT = "rev_motor_02"   # links below this joint are the ones whose height changes

# Gripper frame (x = jaw opening axis, y = forward along the jaws, z = the side the camera is on),
# built from the URDF axes of rev_motor_07 and the jaw slides; about 1 degree off the link frame.
# Offsets below are in that frame, from the Gripper_Base origin (= the M7 axis point), in metres.
JAW_TIP_OFFSET = (0.0, 0.104, 0.0)      # from the jaw meshes: they end 104 mm ahead (checked in tests)
# ESTIMATED, not measured: the assembly manual (steps 67-69) shows the camera board on a stalk on
# top of the gripper base, tilted down towards the jaws. Offset and tilt were fitted by rendering
# the jaw meshes from candidate camera poses and comparing with how the jaws look in views/top.jpg
# (tip just below the image centre, two fins, base band at the bottom edge). That fit only involves
# the gripper, not the arm's joint angles. Good to roughly +-2 cm and +-5 degrees.
CAMERA_OFFSET = (0.0, 0.03, 0.07)
CAMERA_TILT_DEG = 30.0
CAMERA_RADIUS = 0.02                    # min_height() treats the camera as a ball this size (board ~4 cm wide, guessed)

# Suggested clearance for a "path dips below the table" check with mapping="worst", meshes=True:
# treat anything lower than this as too close. It covers what the model does not know: a few
# degrees of pitch (2-3 cm at the gripper), the camera stalk, cables, shields, and things lying
# on the table. Four taught views are themselves closer than this, hence check_path().
RECOMMENDED_MARGIN_M = 0.04

Vec = tuple[float, float, float]
Mat = tuple[Vec, Vec, Vec]
_I3: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


# ---------------------------------------------------------------- small vector helpers

def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a))
    if n == 0:
        raise ValueError("zero-length vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def _mat_vec(m: Mat, v: Vec) -> Vec:
    return (_dot(m[0], v), _dot(m[1], v), _dot(m[2], v))


def _mat_mul(a: Mat, b: Mat) -> Mat:
    cols = tuple((b[0][j], b[1][j], b[2][j]) for j in range(3))
    return tuple(tuple(_dot(a[i], cols[j]) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _axis_angle(axis: Vec, angle: float) -> Mat:
    """Rotation by `angle` (rad) about unit `axis` (Rodrigues), same as three.js setFromAxisAngle."""
    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    k = 1.0 - c
    return ((c + x * x * k, x * y * k - z * s, x * z * k + y * s),
            (y * x * k + z * s, c + y * y * k, y * z * k - x * s),
            (z * x * k - y * s, z * y * k + x * s, c + z * z * k))


def _rpy(roll: float, pitch: float, yaw: float) -> Mat:
    """URDF fixed-axis roll-pitch-yaw: R = Rz(yaw) Ry(pitch) Rx(roll)."""
    if roll == 0 and pitch == 0 and yaw == 0:
        return _I3
    rx = _axis_angle((1.0, 0.0, 0.0), roll)
    ry = _axis_angle((0.0, 1.0, 0.0), pitch)
    rz = _axis_angle((0.0, 0.0, 1.0), yaw)
    return _mat_mul(rz, _mat_mul(ry, rx))


def _floats(text: str | None, default: Vec = (0.0, 0.0, 0.0)) -> Vec:
    if not text:
        return default
    parts = [float(x) for x in text.split()]
    if len(parts) != 3:
        raise ValueError(f"expected 3 numbers, got {text!r}")
    return (parts[0], parts[1], parts[2])


# ---------------------------------------------------------------- URDF model

class Model:
    """The kinematic tree of a URDF: joints, links, and which link is the root."""

    def __init__(self, urdf_path: Path = URDF_PATH):
        self.path = Path(urdf_path)
        root = ET.parse(self.path).getroot()
        self.joints: dict[str, dict] = {}
        self.links: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}       # parent link -> joint names
        for link in root.findall("link"):
            mesh = link.find("visual/geometry/mesh")
            origin = link.find("visual/origin")
            self.links[link.get("name")] = {
                "mesh": (self.path.parent / mesh.get("filename")) if mesh is not None else None,
                "scale": _floats(mesh.get("scale"), (1.0, 1.0, 1.0)) if mesh is not None else (1.0, 1.0, 1.0),
                "xyz": _floats(origin.get("xyz")) if origin is not None else (0.0, 0.0, 0.0),
                "rpy": _floats(origin.get("rpy")) if origin is not None else (0.0, 0.0, 0.0),
            }
        child_links = set()
        for j in root.findall("joint"):
            origin, axis, limit, mimic = j.find("origin"), j.find("axis"), j.find("limit"), j.find("mimic")
            info = {
                "type": j.get("type"),
                "parent": j.find("parent").get("link"),
                "child": j.find("child").get("link"),
                "xyz": _floats(origin.get("xyz")) if origin is not None else (0.0, 0.0, 0.0),
                "rot": _rpy(*_floats(origin.get("rpy"))) if origin is not None else _I3,
                # urdf-loader normalises the axis; default axis is +x
                "axis": _unit(_floats(axis.get("xyz"))) if axis is not None else (1.0, 0.0, 0.0),
                "lower": float(limit.get("lower", 0.0)) if limit is not None else 0.0,
                "upper": float(limit.get("upper", 0.0)) if limit is not None else 0.0,
                "mimic": mimic.get("joint") if mimic is not None else None,
            }
            self.joints[j.get("name")] = info
            self.children.setdefault(info["parent"], []).append(j.get("name"))
            child_links.add(info["child"])
        roots = [name for name in self.links if name not in child_links]
        if len(roots) != 1:
            raise ValueError(f"expected one root link, found {roots}")
        self.root = roots[0]
        for m, name in MOTOR_JOINTS.items():
            if name not in self.joints:
                raise ValueError(f"URDF has no joint {name} for motor {m}")

    def frames(self, angles: dict[str, float]) -> tuple[dict[str, tuple[Mat, Vec]], dict[str, Vec]]:
        """Pose (rotation, position) of every link and position of every joint origin, in the base
        frame. `angles` maps joint name -> value (rad, or m for the jaw slides); missing = 0."""
        link_pose: dict[str, tuple[Mat, Vec]] = {self.root: (_I3, (0.0, 0.0, 0.0))}
        joint_pos: dict[str, Vec] = {}
        todo = [self.root]
        while todo:
            parent = todo.pop()
            rot_p, pos_p = link_pose[parent]
            for name in self.children.get(parent, []):
                j = self.joints[name]
                pos = _add(pos_p, _mat_vec(rot_p, j["xyz"]))
                rot = _mat_mul(rot_p, j["rot"])
                joint_pos[name] = pos
                q = angles.get(name, 0.0)
                if j["type"] in ("revolute", "continuous"):
                    rot = _mat_mul(rot, _axis_angle(j["axis"], q))
                elif j["type"] == "prismatic":
                    pos = _add(pos, _scale(_mat_vec(rot, j["axis"]), q))
                elif j["type"] != "fixed":
                    raise ValueError(f"joint {name}: type {j['type']!r} not supported")
                link_pose[j["child"]] = (rot, pos)
                todo.append(j["child"])
        return link_pose, joint_pos

    def gripper_axes(self) -> tuple[Vec, Vec, Vec]:
        """Unit axes of the gripper frame, expressed in the Gripper_Base link frame:
        (x = jaw slide direction, y = forward along the jaws, z = camera side)."""
        forward = _scale(self.joints[MOTOR_JOINTS[7]]["axis"], -1.0)     # M7 rolls about the jaw axis
        slide = self.joints[JAW_JOINTS[0]]["axis"]
        x = _unit(_cross(forward, _cross(_scale(slide, -1.0), forward)))  # slide axis made exactly perpendicular
        return x, forward, _cross(x, forward)

    def moving_links(self) -> list[str]:
        """Links whose height can change: everything below the shoulder joint."""
        out, todo = [], [self.joints[FIRST_MOVING_JOINT]["child"]]
        while todo:
            link = todo.pop()
            out.append(link)
            todo.extend(self.joints[n]["child"] for n in self.children.get(link, []))
        return out


_MODEL: Model | None = None


def model() -> Model:
    global _MODEL
    if _MODEL is None:
        _MODEL = Model()
    return _MODEL


# ---------------------------------------------------------------- steps -> angles

def _int_keys(d: dict) -> dict[int, object]:
    return {int(k): v for k, v in d.items()}


def normalise(pos: float, range_min: int, range_max: int, clamp: bool = False) -> float:
    """Position within the calibrated arc as 0..1, exactly as the viewer computes it
    (including `rangeMax || 4095` and its 4095-step wrap). With clamp=False positions outside
    the arc extrapolate linearly (negative or > 1) instead of being cut off as in the viewer."""
    range_min = range_min or 0
    range_max = range_max or _VIEWER_MAX_STEP
    p = 0.0
    if range_min > range_max:                          # arc crosses the 4095 -> 0 seam
        total = (_VIEWER_MAX_STEP - range_min) + range_max
        if total != 0:
            if pos >= range_min:
                p = (pos - range_min) / total
            else:
                p = (_VIEWER_MAX_STEP - range_min + pos) / total
            if p > 1.0 and not clamp:
                # in the gap between max and min: measure from whichever end is nearer
                below_min = p - STEPS_PER_TURN / total
                if -below_min < p - 1.0:
                    p = below_min
    elif range_max != range_min:
        p = (pos - range_min) / (range_max - range_min)
    return max(0.0, min(1.0, p)) if clamp else p


def _arc_steps(range_min: int, range_max: int) -> int:
    """Length of the calibrated arc in encoder steps (same arithmetic as normalise())."""
    range_min = range_min or 0
    range_max = range_max or _VIEWER_MAX_STEP
    return (_VIEWER_MAX_STEP - range_min) + range_max if range_min > range_max else range_max - range_min


def _normalised(joints: dict, ranges: dict | None, clamp: bool) -> tuple[dict[int, float], dict[int, int]]:
    """({motor: position 0..1 in its calibrated arc}, {motor: arc length in steps})."""
    joints, ranges = _int_keys(joints), _int_keys(ranges if ranges is not None else FOLLOWER_RANGES)
    out, arcs = {}, {}
    for m in MOTOR_JOINTS:
        if m not in ranges:
            raise ValueError(f"no calibrated range for motor {m}; got motors {sorted(ranges)}")
        lo, hi = ranges[m]
        arcs[m] = _arc_steps(lo, hi)
        if m not in joints:
            if m == GRIPPER_MOTOR:
                out[m] = 0.0                           # no gripper reading: jaws closed, as in every saved view
                continue
            raise ValueError(f"no position for motor {m}; got motors {sorted(joints)}")
        out[m] = normalise(joints[m], lo, hi, clamp)
    return out, arcs


def _angles_from_normalised(p: dict[int, float], arcs: dict[int, int], mapping: str = "viewer") -> dict[str, float]:
    if mapping not in ("viewer", "true_scale"):
        raise ValueError(f"mapping must be 'viewer' or 'true_scale', got {mapping!r}")
    joints = model().joints
    out = {}
    for m, name in MOTOR_JOINTS.items():
        j = joints[name]
        if m == GRIPPER_MOTOR:
            # config.ts resolveElrobotJointValue, called with each joint's own URDF limits
            out[name] = j["upper"] - (j["lower"] + p[m] * (j["upper"] - j["lower"]))
            for jaw in JAW_JOINTS:
                jj = joints[jaw]
                pos = jj["lower"] + p[m] * (jj["upper"] - jj["lower"])
                out[jaw] = pos if jaw.endswith("_1") else jj["upper"] - pos
        elif mapping == "viewer":
            out[name] = j["lower"] + p[m] * (j["upper"] - j["lower"])
        else:
            out[name] = j["upper"] - (1.0 - p[m]) * arcs[m] * 2.0 * math.pi / STEPS_PER_TURN
    return out


def steps_to_angles(joints: dict, ranges: dict | None = None, clamp: bool = False,
                    mapping: str = "viewer") -> dict[str, float]:
    """URDF joint values for raw encoder steps, the way Station's 3D viewer maps them.

    joints: {motor id: steps}, ranges: {motor id: (min, max)} (default FOLLOWER_RANGES).
    Returns {"rev_motor_01": rad, ..., "rev_motor_08": rad, "rev_motor_08_1": m, "rev_motor_08_2": m}.
    clamp=True reproduces the viewer bit for bit (it cuts positions off at the ends of the
    calibrated arc); the default extrapolates, which is what the physical joint does.

    mapping="true_scale" is an alternative for M1-M7, not what the viewer does: one step is
    360/4096 degrees (the servo drives the joint directly), anchored so that the calibrated
    maximum sits on the URDF upper limit. The viewer instead stretches the calibrated arc onto
    the whole URDF range, which overstates every M1 / M2 movement by about 7% with the current
    calibration. Which of the two is closer to the real arm is not settled, see the report.
    """
    p, arcs = _normalised(joints, ranges, clamp)
    return _angles_from_normalised(p, arcs, mapping)


def angles_to_steps(angles: dict[str, float], ranges: dict | None = None) -> dict[int, int]:
    """Inverse of steps_to_angles for arcs that do not cross the encoder seam (rounded to steps)."""
    ranges = _int_keys(ranges if ranges is not None else FOLLOWER_RANGES)
    joints = model().joints
    out = {}
    for m, name in MOTOR_JOINTS.items():
        if name not in angles:
            continue
        j, (lo, hi) = joints[name], ranges[m]
        if lo > hi:
            raise ValueError(f"motor {m}: arc crosses the encoder seam, not supported here")
        p = (angles[name] - j["lower"]) / (j["upper"] - j["lower"])
        if m == GRIPPER_MOTOR:
            p = 1.0 - p
        out[m] = round(lo + p * (hi - lo))
    return out


# ---------------------------------------------------------------- forward kinematics

def _key_points(angles: dict[str, float]) -> tuple[dict[str, Vec], dict[str, tuple[Mat, Vec]], tuple[Vec, Vec, Vec]]:
    mdl = model()
    link_pose, joint_pos = mdl.frames(angles)
    points = {JOINT_POINTS[m]: joint_pos[name] for m, name in MOTOR_JOINTS.items()}
    rot, pos = link_pose[GRIPPER_LINK]
    axes = tuple(_mat_vec(rot, a) for a in mdl.gripper_axes())          # gripper x, forward, camera side (world)
    for name, off in (("jaw_tip", JAW_TIP_OFFSET), ("camera", CAMERA_OFFSET)):
        p = pos
        for axis, d in zip(axes, off):
            p = _add(p, _scale(axis, d))
        points[name] = p
    return points, link_pose, axes  # type: ignore[return-value]


def fk(joints: dict, ranges: dict | None = None, mapping: str = "viewer") -> dict[str, Vec]:
    """World positions (m, +z up, z = 0 is the plane the base stands on) of the key points:
    the axis point of every motor (j1_base_yaw ... j8_gripper), `jaw_tip` (far end of the closed
    jaws, on the gripper centreline) and `camera` (estimated, see CAMERA_OFFSET)."""
    return _key_points(steps_to_angles(joints, ranges, mapping=mapping))[0]


def gripper_frame(joints: dict, ranges: dict | None = None, mapping: str = "viewer") -> tuple[Vec, Vec, Vec]:
    """World unit vectors of the gripper: (jaw slide axis, forward along the jaws, camera side)."""
    return _key_points(steps_to_angles(joints, ranges, mapping=mapping))[2]


def camera_direction(joints: dict, ranges: dict | None = None, mapping: str = "viewer") -> Vec:
    """Unit vector of the gripper's forward axis (along the jaws) in the world frame.
    The camera itself looks about CAMERA_TILT_DEG further "down" than this, see camera_optical_axis()."""
    return gripper_frame(joints, ranges, mapping)[1]


def camera_optical_axis(joints: dict, ranges: dict | None = None, tilt_deg: float = CAMERA_TILT_DEG,
                        mapping: str = "viewer") -> Vec:
    """Estimated viewing direction of the camera: the gripper's forward axis tilted by `tilt_deg`
    towards the jaws (away from the side the camera is mounted on). The tilt is a fitted guess."""
    _, forward, up = gripper_frame(joints, ranges, mapping)
    t = math.radians(tilt_deg)
    return _unit(_add(_scale(forward, math.cos(t)), _scale(up, -math.sin(t))))


# ---------------------------------------------------------------- link meshes (lowest real point)

_HULL_DIRECTIONS = 96
_MESH_POINTS: dict[str, list[Vec]] | None = None


def _stl_vertices(path: Path) -> set[Vec]:
    data = path.read_bytes()
    if len(data) < 84:
        raise ValueError(f"{path}: too short for an STL file")
    (count,) = struct.unpack_from("<I", data, 80)
    if len(data) != 84 + 50 * count:
        raise ValueError(f"{path}: not a binary STL ({len(data)} bytes, header says {count} triangles)")
    verts = set()
    for tri in struct.iter_unpack("<12fH", data[84:]):
        verts.add(tri[3:6])
        verts.add(tri[6:9])
        verts.add(tri[9:12])
    return verts


def _sphere_directions(n: int) -> list[Vec]:
    out: list[Vec] = [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)]
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(n):
        z = 1.0 - 2.0 * (i + 0.5) / n
        r = math.sqrt(1.0 - z * z)
        out.append((r * math.cos(golden * i), r * math.sin(golden * i), z))
    return out


def mesh_points() -> dict[str, list[Vec]]:
    """For every moving link, the outermost vertices of its STL mesh in ~100 directions, in the
    link frame (m). Enough to find the lowest point of the link in any pose to within ~2 mm.
    Parsed once per process (takes a second or two)."""
    global _MESH_POINTS
    if _MESH_POINTS is None:
        mdl = model()
        dirs = _sphere_directions(_HULL_DIRECTIONS)
        out = {}
        for link in mdl.moving_links():
            info = mdl.links[link]
            if info["mesh"] is None:
                continue
            sx, sy, sz = info["scale"]
            rot, off = _rpy(*info["rpy"]), info["xyz"]
            verts = [_add(_mat_vec(rot, (x * sx, y * sy, z * sz)), off) if rot is not _I3
                     else (x * sx + off[0], y * sy + off[1], z * sz + off[2])
                     for x, y, z in _stl_vertices(info["mesh"])]
            keep = {max(verts, key=lambda v, d=d: v[0] * d[0] + v[1] * d[1] + v[2] * d[2]) for d in dirs}
            out[link] = sorted(keep)
        _MESH_POINTS = out
    return _MESH_POINTS


def _lowest(angles: dict[str, float], meshes: bool) -> tuple[float, str]:
    points, link_pose, _ = _key_points(angles)
    best_h, best_name = math.inf, ""
    for name, p in points.items():
        h = p[2] - CAMERA_RADIUS if name == "camera" else p[2]
        if name not in STATIC_POINTS and h < best_h:
            best_h, best_name = h, name
    if meshes:
        for link, verts in mesh_points().items():
            rot, pos = link_pose[link]
            rz, pz = rot[2], pos[2]
            h = min(rz[0] * v[0] + rz[1] * v[1] + rz[2] * v[2] for v in verts) + pz
            if h < best_h:
                best_h, best_name = h, f"mesh:{link}"
    return best_h, best_name


def _mappings(mapping: str) -> tuple[str, ...]:
    if mapping == "worst":
        return ("viewer", "true_scale")
    if mapping in ("viewer", "true_scale"):
        return (mapping,)
    raise ValueError(f"mapping must be 'viewer', 'true_scale' or 'worst', got {mapping!r}")


def min_height(joints: dict, ranges: dict | None = None, meshes: bool = True,
               mapping: str = "viewer") -> tuple[float, str]:
    """(height in m, point name) of the lowest moving point of the arm in this pose.

    Looks at the key points from fk() except the two that cannot change height, and, with
    meshes=True, at the URDF meshes of every link below the shoulder (name "mesh:<link>").
    The camera is an estimated ball of CAMERA_RADIUS; its stalk, the cables and the joint shields
    are not in the URDF and not checked.
    A negative height means "below the plane the base stands on" according to this model.
    mapping="worst" evaluates both steps->angle mappings (see steps_to_angles) and returns the lower.
    """
    p, arcs = _normalised(joints, ranges, clamp=False)
    return min(_lowest(_angles_from_normalised(p, arcs, mp), meshes) for mp in _mappings(mapping))


def path_min_height(start: dict, end: dict, ranges: dict | None = None, moving_ids=None,
                    samples: int = 20, meshes: bool = True, mapping: str = "viewer") -> tuple[float, str, float]:
    """Lowest point along a straight joint-space move from `start` to `end`.

    Motors in `moving_ids` (default: all) go linearly from their start to their end position,
    along the calibrated arc; the others stay at `start`. The path is checked at samples + 1
    evenly spaced poses including both ends.
    Returns (height in m, point name, fraction of the path 0..1 where it happens).
    """
    if samples < 1:
        raise ValueError("samples must be >= 1")
    p0, arcs = _normalised(start, ranges, clamp=False)
    p1, _ = _normalised({**_int_keys(start), **_int_keys(end)}, ranges, clamp=False)
    ids = set(MOTOR_JOINTS) if moving_ids is None else {int(m) for m in moving_ids}
    unknown = ids - set(MOTOR_JOINTS)
    if unknown:
        raise ValueError(f"unknown motor ids {sorted(unknown)}")
    best = (math.inf, "", 0.0)
    for i in range(samples + 1):
        t = i / samples
        p = {m: p0[m] + (p1[m] - p0[m]) * t if m in ids else p0[m] for m in p0}
        for mp in _mappings(mapping):
            h, name = _lowest(_angles_from_normalised(p, arcs, mp), meshes)
            if h < best[0]:
                best = (h, name, t)
    return best


def route_min_height(present: dict, route, ranges: dict | None = None, samples: int = 20,
                     meshes: bool = True, mapping: str = "viewer") -> list[tuple[str, float, str, float]]:
    """Lowest point of every segment of a route from motion.plan_route().

    `route` is [(segment name, {motor: goal steps})]. Starting at `present`, each segment moves
    only its own motors in a straight line to their goals while all others hold, and the next
    segment starts where the previous one ended. Returns one (segment name, height in m,
    point name, fraction of that segment 0..1) per segment, in order.
    """
    pose = dict(_int_keys(present))
    out = []
    for name, goals in route:
        goals = _int_keys(goals)
        h, point, t = path_min_height(pose, goals, ranges, moving_ids=list(goals), samples=samples,
                                      meshes=meshes, mapping=mapping)
        out.append((name, h, point, t))
        pose.update(goals)
    return out


def check_path(start: dict, end: dict, ranges: dict | None = None, moving_ids=None, samples: int = 20,
               margin: float = RECOMMENDED_MARGIN_M, dip_tolerance: float = 0.005) -> dict:
    """Advisory verdict for one straight joint-space move, using the lower of both mappings.

    ok when the lowest point of the path stays above `margin`, or when it never gets more than
    `dip_tolerance` below the lower of the two end poses. The second rule is only meaningful
    when both ends are poses the real arm has been in (a taught view, the present position):
    several taught views sit closer to the table than the margin, and comparing against them
    cancels most of the model's systematic error. Returns a dict with ok, height, point, t,
    start_height, end_height and a one-line reason.
    """
    kw = dict(ranges=ranges, mapping="worst")
    if moving_ids is None:
        end_pose = {**_int_keys(start), **_int_keys(end)}
    else:
        end_i = _int_keys(end)
        end_pose = {**_int_keys(start), **{int(m): end_i[int(m)] for m in moving_ids}}
    h, point, t = path_min_height(start, end, moving_ids=moving_ids, samples=samples, **kw)
    h0, h1 = min_height(start, **kw)[0], min_height(end_pose, **kw)[0]
    floor = min(h0, h1) - dip_tolerance
    if h >= margin:
        ok, reason = True, f"lowest point {point} stays {h * 100:.1f} cm above the base plane"
    elif h >= floor:
        ok, reason = True, (f"lowest point {point} at {h * 100:.1f} cm is inside the {margin * 100:.0f} cm margin, "
                            f"but no lower than the end poses ({h0 * 100:.1f} / {h1 * 100:.1f} cm)")
    else:
        ok, reason = False, (f"{point} dips to {h * 100:.1f} cm at {t:.0%} of the move, below both end poses "
                             f"({h0 * 100:.1f} / {h1 * 100:.1f} cm) and the {margin * 100:.0f} cm margin")
    return {"ok": ok, "height": h, "point": point, "t": t, "start_height": h0, "end_height": h1, "reason": reason}


# ---------------------------------------------------------------- command line

def load_views(path: Path = VIEWS_PATH) -> dict[str, dict[int, int]]:
    views = json.loads(Path(path).read_text())["views"]
    return {name: {int(k): int(v) for k, v in view["joints"].items()} for name, view in views.items()}


def _vec(v: Vec) -> str:
    return f"({v[0]:+.2f} {v[1]:+.2f} {v[2]:+.2f})"


def describe(name: str, joints: dict, ranges: dict | None = None) -> list[str]:
    angles = steps_to_angles(joints, ranges)
    alt = steps_to_angles(joints, ranges, mapping="true_scale")
    points = fk(joints, ranges)
    h_keys, n_keys = min_height(joints, ranges, meshes=False)
    h_mesh, n_mesh = min_height(joints, ranges)
    h_alt, n_alt = min_height(joints, ranges, mapping="true_scale")
    flags = [f"{n} at {p[2]:+.3f} m" for n, p in points.items() if p[2] < 0]
    if h_mesh < 0:
        flags.append(f"{n_mesh} at {h_mesh:+.3f} m")
    if h_alt < 0:
        flags.append(f"{n_alt} at {h_alt:+.3f} m (true-scale mapping)")
    motors = [m for m in MOTOR_JOINTS if m != GRIPPER_MOTOR]
    lines = [
        f"{name}",
        "  angles (deg)   " + "  ".join(f"M{m} {math.degrees(angles[MOTOR_JOINTS[m]]):+6.1f}" for m in motors),
        "    true-scale   " + "  ".join(f"M{m} {math.degrees(alt[MOTOR_JOINTS[m]]):+6.1f}" for m in motors),
        "  heights (m)    " + "  ".join(f"{n} {p[2]:+.3f}" for n, p in points.items() if n not in STATIC_POINTS),
        f"  lowest         key point {n_keys} {h_keys:+.3f} | with meshes {n_mesh} {h_mesh:+.3f}"
        f" | true-scale mapping {n_alt} {h_alt:+.3f}",
        f"  gripper axis   {_vec(camera_direction(joints, ranges))}   camera axis (tilt {CAMERA_TILT_DEG:.0f} deg, estimated) "
        f"{_vec(camera_optical_axis(joints, ranges))}   camera at {_vec(points['camera'])}",
    ]
    lines.append("  BELOW 0: " + "; ".join(flags) if flags else "  nothing below 0")
    return lines


def describe_routes(ranges: dict | None = None) -> list[str]:
    """transit -> every view and back, along motion.plan_route(), lowest point per segment."""
    try:
        import motion
    except Exception as e:                                    # motion.py is being edited by others
        return [f"routes: could not import motion.py ({type(e).__name__}: {e})"]
    ranges = ranges if ranges is not None else FOLLOWER_RANGES
    try:
        raw = motion.load_views()
        env = motion.envelope(ranges)
        views = {n: motion.clamp_pose(p, env) for n, p in raw.items()}
        hub = motion.TRANSIT
        if hub not in views:
            return [f"routes: views.json has no {hub!r} pose"]
        lines = ["routes from motion.plan_route (views clamped to the allowed envelope), lowest point per segment in cm,",
                 "lower of both mappings; '!' = below 0, '~' = inside the margin and lower than both ends of the route"]
        for name in views:
            if name == hub:
                continue
            for a, b in ((hub, name), (name, hub)):
                start = {**views[a], **({GRIPPER_MOTOR: raw[a][GRIPPER_MOTOR]} if GRIPPER_MOTOR in raw[a] else {})}
                route = motion.plan_route(start, b, views)
                segs = route_min_height(start, route, ranges, mapping="worst")
                ends = min(min_height(start, ranges, mapping="worst")[0],
                           min_height({**start, **views[b]}, ranges, mapping="worst")[0])
                cells = []
                for seg, h, point, t in segs:
                    mark = "!" if h < 0 else "~" if h < RECOMMENDED_MARGIN_M and h < ends - 0.005 else ""
                    cells.append(f"{seg} {h * 100:+.1f}{mark} ({point.replace('mesh:', '')} @{t:.0%})")
                lines.append(f"  {a:>8s} -> {b:<8s} " + " | ".join(cells))
        return lines
    except Exception as e:
        return [f"routes: could not plan ({type(e).__name__}: {e})"]


def _read_live(bus: str) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    """Present positions and calibrated ranges from Station. Reads only; never sends a command."""
    import asyncio
    from station_arm import Arm

    async def read():
        arm = await Arm.connect(bus=bus)
        try:
            return arm.joints(), arm.ranges()
        finally:
            arm.close()

    return asyncio.run(read())


def main(argv: list[str]) -> int:
    ranges = FOLLOWER_RANGES
    if "--live" in argv:
        i = argv.index("--live")
        bus = argv[i + 1] if len(argv) > i + 1 else "5B79016329"
        try:
            joints, ranges = _read_live(bus)
        except (RuntimeError, OSError) as e:
            print(f"could not read the arm: {e}", file=sys.stderr)
            return 1
        print(f"live ranges: {ranges}")
        print("\n".join(describe(f"live pose on {bus}: {joints}", joints, ranges)))
        return 0
    views = load_views()
    if "--path" in argv:
        i = argv.index("--path")
        a, b = argv[i + 1], argv[i + 2]
        for mp in ("viewer", "true_scale"):
            h, name, t = path_min_height(views[a], views[b], ranges, mapping=mp)
            print(f"{a} -> {b}, all joints together, {mp} mapping: lowest point {name} at {h:+.3f} m,"
                  f" {t:.0%} of the way{'  BELOW 0' if h < 0 else ''}")
        verdict = check_path(views[a], views[b], ranges)
        print(f"check_path: {'ok' if verdict['ok'] else 'REFUSE'}: {verdict['reason']}")
        return 0
    print(f"URDF {URDF_PATH.relative_to(REPO)}, ranges = FOLLOWER_RANGES snapshot, world +z up, z = 0 under the base")
    print("angles / heights use the viewer mapping; 'true-scale' is the alternative mapping (see steps_to_angles)")
    below = 0
    for name, joints in views.items():
        lines = describe(name, joints, ranges)
        below += "BELOW 0" in lines[-1]
        print("\n".join(lines))
    print(f"\n{below} of {len(views)} views have something below 0."
          f" Suggested clearance for path checks: {RECOMMENDED_MARGIN_M * 100:.0f} cm (RECOMMENDED_MARGIN_M).\n")
    print("\n".join(describe_routes(ranges)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
