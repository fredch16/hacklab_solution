"""M2: move the follower arm to a saved view, smoothly and inside its joint limits.

    python go.py top             # CLI wrapper
    await go_to_view(arm, "top") # from code: {"ok": bool, "view": str, "error": str | None}

How a move works:
  1. Hold: goal = present position, then torque on (no jump to a stale goal).
  2. Interpolate every joint from present to target with a smoothstep ease,
     streaming goals at 30 Hz. Duration = max(min_duration, slowest joint at max_speed).
  3. Interpolation happens inside each joint's calibrated arc, so a move never
     sweeps through the part of the circle the joint can't physically reach.
  4. Ctrl-C / cancel: hold position where the arm is.
The gripper (last motor: 6 on SO-101, 8 on ElRobot) is left alone by default in case the camera is mounted on it.
"""

import asyncio
import json
import time
from pathlib import Path

from station_arm import Arm, gripper_id, logger

VIEWS_PATH = Path(__file__).parent / "views.json"
FULL = 4096

MAX_SPEED = 500        # encoder steps per second for the joint that travels furthest (~44 deg/s)
MIN_DURATION = 1.5     # seconds
RATE_HZ = 30
MARGIN = 30            # steps kept clear of each calibrated end stop
SERVO_SPEED = 1500     # servo's own speed ceiling while tracking our waypoints
SERVO_ACCEL = 50
ARRIVE_TOLERANCE = 40  # steps; a joint further off than this after the move counts as not arrived

_move_lock = asyncio.Lock()


def load_views(path: Path | None = None) -> dict[str, dict[int, int]]:
    path = path or VIEWS_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {name: {int(k): v for k, v in v["joints"].items()} for name, v in data["views"].items()}


def view_names() -> list[str]:
    return list(load_views())


def view_descriptions() -> dict[str, str]:
    if not VIEWS_PATH.exists():
        return {}
    return {n: v.get("description", "") for n, v in json.loads(VIEWS_PATH.read_text())["views"].items()}


class JointArc:
    """Calibrated arc of one joint, possibly wrapping past 4095 -> 0."""

    def __init__(self, range_min: int, range_max: int):
        self.min = range_min
        self.size = (range_max - range_min) % FULL
        self.calibrated = self.size > 2 * MARGIN

    def include(self, pos: int, slack: int = 300):
        """Widen the arc to cover a pose the arm has physically been in (present or saved view),
        if it's only a little outside the calibrated range. Far outside still gets clamped."""
        if not self.calibrated:
            return
        rel = (pos - self.min) % FULL
        if rel <= self.size:
            return
        above, below = rel - self.size, FULL - rel
        if above <= below and above <= slack:
            self.size += above + MARGIN
        elif below < above and below <= slack:
            self.min = (self.min - below - MARGIN) % FULL
            self.size += below + MARGIN

    def to_rel(self, pos: int) -> float:
        if not self.calibrated:
            return float(pos)
        rel = (pos - self.min) % FULL
        if rel > self.size:  # outside the arc: snap to the nearer end
            rel = self.size if rel - self.size < FULL - rel else 0
        return float(min(max(rel, MARGIN), self.size - MARGIN))

    def to_pos(self, rel: float) -> int:
        if not self.calibrated:
            return int(round(rel)) % FULL
        return int(round(self.min + rel)) % FULL


def _ease(s: float) -> float:
    return s * s * (3 - 2 * s)


async def hold(arm: Arm):
    """Freeze the arm where it is right now."""
    await arm.write_goals(arm.joints())


async def move_to(arm: Arm, target: dict[int, int], include_gripper: bool = False,
                  max_speed: float = MAX_SPEED, min_duration: float = MIN_DURATION,
                  on_tick=None) -> dict:
    """on_tick(s) is called with the move's progress 0..1 before each waypoint. If it raises,
    the move ends without writing anything else: the servos keep the last waypoint."""
    present = arm.joints()
    ranges = arm.ranges()
    grip = gripper_id(present)
    ids = [m for m in target if m in present and (include_gripper or m != grip)]
    missing = [m for m in target if m not in present]
    if missing:
        return {"ok": False, "error": f"motors {missing} not on the bus"}

    arcs = {m: JointArc(*ranges.get(m, (0, 0))) for m in ids}
    for m in ids:
        arcs[m].include(present[m])
        arcs[m].include(target[m])
    uncal = [m for m in ids if not arcs[m].calibrated]
    if uncal:
        logger.warning("motors %s have no calibrated range; moving without limit clamping", uncal)
    start = {m: arcs[m].to_rel(present[m]) for m in ids}
    goal = {m: arcs[m].to_rel(target[m]) for m in ids}
    distance = max((abs(goal[m] - start[m]) for m in ids), default=0)
    duration = max(min_duration, distance / max_speed)

    await arm.write_goals(present)          # hold first so enabling torque can't jump
    await arm.set_torque(True, sorted(present))

    t0 = time.monotonic()
    first = True
    try:
        while True:
            s = min(1.0, (time.monotonic() - t0) / duration)
            if on_tick is not None:
                on_tick(s)
            k = _ease(s)
            goals = {m: arcs[m].to_pos(start[m] + (goal[m] - start[m]) * k) for m in ids}
            if first:
                await arm.write_goals(goals, speed=SERVO_SPEED, accel=SERVO_ACCEL)
                first = False
            else:
                await arm.write_goals(goals)
            if s >= 1.0:
                break
            await asyncio.sleep(1 / RATE_HZ)
        await asyncio.sleep(0.4)
    except asyncio.CancelledError:
        await hold(arm)
        raise

    final = arm.joints()
    errors = {m: abs(arcs[m].to_rel(final[m]) - goal[m]) for m in ids}
    worst = max(errors, key=errors.get, default=None)
    if worst is not None and errors[worst] > ARRIVE_TOLERANCE:
        return {"ok": False, "error": f"motor {worst} is {errors[worst]:.0f} steps short (blocked or overloaded?)",
                "duration": round(duration, 2)}
    return {"ok": True, "error": None, "duration": round(duration, 2)}


async def go_to_view(arm: Arm, name: str, **kwargs) -> dict:
    """The interface everyone codes against: {"ok", "view", "error"}."""
    views = load_views()
    if name not in views:
        return {"ok": False, "view": name, "error": f"unknown view; known: {sorted(views)}"}
    async with _move_lock:
        try:
            result = await move_to(arm, views[name], **kwargs)
        except RuntimeError as e:
            result = {"ok": False, "error": str(e)}
    return {"view": name, **result}
