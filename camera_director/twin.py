"""The arm's digital twin for the dashboard: every URDF link's mesh pose from the live joint readings (fk.py, the
same model motion planning uses), the camera's position and viewing direction, and the fused object position."""
from __future__ import annotations

import re

import fk
import framing

MESH_DIR = fk.URDF_PATH.parent
MESH_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.stl$")


def links(joints: dict, ranges: dict) -> list[dict]:
    """[{mesh, matrix}] with matrix a column-major 4x4 (three.js Matrix4.elements): link pose x visual origin x scale."""
    mdl = fk.model()
    poses, _ = mdl.frames(fk.steps_to_angles(joints, ranges))
    out = []
    for name, (rot, pos) in poses.items():
        info = mdl.links.get(name) or {}
        if not info.get("mesh"):
            continue
        r = fk._mat_mul(rot, fk._rpy(*info["rpy"]))
        p = fk._add(pos, fk._mat_vec(rot, info["xyz"]))
        s = info["scale"]
        m = [r[row][col] * s[col] for col in range(3) for row in range(3)]   # column-major 3x3, scaled
        out.append({"mesh": info["mesh"].name,
                    "matrix": [m[0], m[1], m[2], 0.0, m[3], m[4], m[5], 0.0, m[6], m[7], m[8], 0.0,
                               p[0], p[1], p[2], 1.0]})
    return out


def camera(joints: dict, ranges: dict) -> dict:
    cam, axis, right, down = framing.camera_pose(joints, ranges)
    return {"position": list(cam), "axis": list(axis), "up": [-v for v in down]}


def mesh_bytes(name: str) -> bytes | None:
    """An STL from the URDF's asset folder, by plain file name only (no paths)."""
    if not MESH_NAME.match(name):
        return None
    path = MESH_DIR / "assets" / name
    return path.read_bytes() if path.is_file() else None
