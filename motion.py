"""M2: move the follower arm to a saved view through a taught transit pose, in ordered phases.

    await go_to_view(arm, "top")  ->  {"ok": bool, "view": str, "error": str | None, ...}

Design and rationale: docs/MOTION_PLAN.md. In short:

  * Every move runs on a fixed network: each view has a "spoke" to the hub (the transit pose's
    pitch configuration). Leaving a view: lift (shoulder) -> fold (elbow/pitch/wrist pitch).
    At the hub: turn (base yaw + both rolls). Entering a view: shape -> lower.
  * A move only starts when the arm is somewhere on that network (at a view, on a spoke, at the
    hub). Anywhere else it refuses, unless the caller explicitly asks to recover to transit.
  * The arm must already be holding (torque on). We never switch torque on or off here.
  * The gripper (highest motor id, the camera mount) is never written.
  * While moving: goals are streamed in small steps and only advance while the arm keeps up,
    so the commanded position is never far from the measured one. Any fault (not following,
    stale data, servo error, overcurrent, a held joint pushed away, a failed write, Stop)
    ends the move and holds the arm where it is.
"""

import asyncio
import json
import time
from pathlib import Path

from station_arm import gripper_id, logger

VIEWS_PATH = Path(__file__).parent / "views.json"
LOG_PATH = Path(__file__).parent / "motion_log.jsonl"
TRANSIT = "transit"

# Joint groups (ElRobot motor ids). Motor 8 is the gripper and is never moved.
LIFT = (2,)          # shoulder pitch: the big lever
SHAPE = (3, 4, 6)    # elbow pitch, pitch, wrist pitch
TURN = (1, 5, 7)     # base yaw, forearm roll, wrist roll
PITCH = LIFT + SHAPE
MOVABLE = tuple(sorted(PITCH + TURN))

SPEED_SLOW = 200       # peak steps/s of the joint that travels furthest (~18 deg/s)
SPEED_NORMAL = 400     # (~35 deg/s)
MIN_SEGMENT_S = 0.8
RATE_HZ = 30
MARGIN = 30            # steps kept clear of each calibrated end stop
MIN_RANGE = 400        # a calibrated arc narrower than this is treated as not calibrated
SERVO_ACCEL = 15       # register 0x29, units of 100 steps/s^2

AT_TOL = 60            # steps: "the arm is at this pose / on this spoke"
SKIP_TOL = 15          # a segment whose joints are all this close to its goal is skipped
ARRIVE_TOL = 40        # after a segment every moved joint must be this close to its goal
SETTLE_S = 2.0         # ...within this long, and standing still
SETTLE_POLL_S = 0.15
LEAD_LIMIT = 120       # commanded position may lead the measured one by at most this
STALL_S = 0.6          # not catching up for this long = blocked
DRIFT_LIMIT = 150      # a joint that should be holding still has moved this far = pushed/sagging
STALE_S = 0.3          # joint data older than this is not trusted
WRITE_TIMEOUT_S = 0.5
CURRENT_LIMIT = 250    # raw units of 6.5 mA (~1.6 A); provisional until measured on the arm
CURRENT_S = 0.3
STATUS_IGNORE = 0x01   # voltage bit; set on under-volted leaders, not a motion fault

_move_lock = asyncio.Lock()

# Live state of the current/last move, for UIs to poll. Updated in place, never replaced.
status = {"moving": False, "view": None, "phase": None, "phase_index": 0, "phase_count": 0,
          "progress": 0.0, "error": None, "last_result": None, "route": []}


class MotionError(RuntimeError):
    """A move was refused or aborted. The message is safe to show to the user."""


# ---------------------------------------------------------------- views

def _read_views_file(path: Path | None = None) -> dict:
    path = path or VIEWS_PATH
    if not path.exists():
        return {}
    return json.loads(path.read_text())["views"]


def load_views(path: Path | None = None) -> dict[str, dict[int, int]]:
    return {name: {int(k): v for k, v in view["joints"].items()}
            for name, view in _read_views_file(path).items()}


def view_names() -> list[str]:
    """Views a user (or Claude) can ask for. The transit pose is not a camera view."""
    return [n for n in load_views() if n != TRANSIT]


def view_descriptions() -> dict[str, str]:
    return {n: v.get("description", "") for n, v in _read_views_file().items() if n != TRANSIT}


def view_exit_order(name: str) -> str:
    """"lift_first" (default) or "fold_first": how the arm leaves this view towards the hub."""
    return _read_views_file().get(name, {}).get("exit", "lift_first")


def view_checked(name: str) -> bool:
    """True once a person has watched this view's route run cleanly (go.py --mark-checked).
    Unchecked views only ever run at slow speed. Re-capturing a view clears the flag."""
    return bool(_read_views_file().get(name, {}).get("checked"))


def mark_checked(name: str, checked: bool = True):
    data = json.loads(VIEWS_PATH.read_text())
    if name not in data["views"]:
        raise MotionError(f"unknown view {name!r}")
    data["views"][name]["checked"] = checked
    tmp = VIEWS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(VIEWS_PATH)


# ---------------------------------------------------------------- limits

def envelope(ranges: dict[int, tuple[int, int]], ids=MOVABLE) -> dict[int, tuple[int, int]]:
    """Allowed (lo, hi) per joint: the calibrated arc minus a margin. Refuses bad calibration."""
    env = {}
    for m in ids:
        lo, hi = ranges.get(m, (0, 0))
        if hi <= lo:
            raise MotionError(f"motor {m}: calibrated range {lo}..{hi} is empty or wraps past 4095; recalibrate")
        if hi - lo < MIN_RANGE:
            raise MotionError(f"motor {m}: calibrated range {lo}..{hi} is too small; recalibrate")
        env[m] = (lo + MARGIN, hi - MARGIN)
    return env


def clamp_pose(pose: dict[int, int], env: dict[int, tuple[int, int]]) -> dict[int, int]:
    return {m: min(max(pose[m], env[m][0]), env[m][1]) for m in env}


def check_views(ranges: dict[int, tuple[int, int]], views: dict | None = None) -> dict[str, list[str]]:
    """Per view: joints whose saved value is outside the allowed envelope (they get clamped)."""
    views = views if views is not None else load_views()
    env = envelope(ranges)
    report = {}
    for name, pose in views.items():
        notes = [f"motor {m}: saved {pose[m]}, allowed {lo}..{hi}"
                 for m, (lo, hi) in env.items() if m in pose and not lo <= pose[m] <= hi]
        if notes:
            report[name] = notes
    return report


# ---------------------------------------------------------------- where is the arm?

def _near(present, pose, ids, tol=AT_TOL) -> bool:
    return all(abs(present[m] - pose[m]) <= tol for m in ids)


def _between(x, a, b, tol=AT_TOL) -> bool:
    return min(a, b) - tol <= x <= max(a, b) + tol


def locate(present: dict[int, int], views: dict[str, dict[int, int]], exit_order=None) -> dict:
    """Classify the measured pose on the route network.

    {"where": "view", "view": name}             at a saved view
    {"where": "hub"}                            pitch joints at the transit configuration
    {"where": "spoke", "view": name, "on": s}   between a view and the hub; s is "lift" (shape
                                                still the view's, shoulder in between) or "fold"
                                                (shoulder at transit, shape in between)
    {"where": "unknown"}                        anything else
    `views` must already be clamped to the envelope and include the transit pose.
    """
    exit_order = exit_order or view_exit_order
    hub = views[TRANSIT]
    for name, pose in views.items():
        if name != TRANSIT and _near(present, pose, MOVABLE):
            return {"where": "view", "view": name}
    if _near(present, hub, PITCH):
        return {"where": "hub"}

    best = None
    for name, pose in views.items():
        if name == TRANSIT or not _near(present, pose, TURN):
            continue
        # The two halves of this view's spoke, in the order it leaves towards the hub.
        halves = ((("lift", SHAPE, pose, LIFT), ("fold", LIFT, hub, SHAPE))
                  if exit_order(name) == "lift_first" else
                  (("fold", LIFT, pose, SHAPE), ("lift", SHAPE, hub, LIFT)))
        for on, still, still_pose, moving in halves:
            if not _near(present, still_pose, still):
                continue
            if not all(_between(present[m], pose[m], hub[m]) for m in moving):
                continue
            # Joints moving together must be at about the same fraction of their travel.
            fracs = [(present[m] - pose[m]) / (hub[m] - pose[m]) for m in moving
                     if abs(hub[m] - pose[m]) > 2 * AT_TOL]
            if fracs and max(fracs) - min(fracs) > 0.25:
                continue
            miss = sum(abs(present[m] - still_pose[m]) for m in still)
            if best is None or miss < best[0]:
                best = (miss, {"where": "spoke", "view": name, "on": on})
    return best[1] if best else {"where": "unknown"}


# ---------------------------------------------------------------- planning

def _pick(pose, ids):
    return {m: pose[m] for m in ids}


def plan_route(present: dict[int, int], target: str, views: dict[str, dict[int, int]],
               exit_order=view_exit_order, allow_unknown_start: bool = False) -> list[tuple[str, dict[int, int]]]:
    """Ordered segments [(name, {motor: goal})] from the measured pose to `target`.

    Each segment moves one joint group; everything else holds still. `views` must be clamped
    and include the transit pose. Raises MotionError when the arm is not on the network.
    """
    hub = views[TRANSIT]
    loc = locate(present, views, exit_order)

    def to_hub(name):   # leaving view `name`
        lift, fold = ("lift", _pick(hub, LIFT)), ("fold", _pick(hub, SHAPE))
        return [lift, fold] if exit_order(name) == "lift_first" else [fold, lift]

    def from_hub(name):  # entering view `name`: the exact reverse of leaving it
        pose = views[name]
        shape, lower = ("shape", _pick(pose, SHAPE)), ("lower", _pick(pose, LIFT))
        return [shape, lower] if exit_order(name) == "lift_first" else [lower, shape]

    if loc["where"] == "view" and loc["view"] == target:
        return []
    if loc["where"] == "view":
        out = to_hub(loc["view"])
    elif loc["where"] == "spoke":
        if loc["view"] == target:     # already on the target's own spoke: go straight down it
            return from_hub(target)
        out = to_hub(loc["view"])
    elif loc["where"] == "hub":
        out = []
    elif allow_unknown_start:
        out = [("lift", _pick(hub, LIFT)), ("fold", _pick(hub, SHAPE))]
    else:
        raise MotionError(
            "the arm is not at a known pose, so there is no checked path from here. "
            "Move it by hand to the transit pose (Go limp, pose it, Hold here), "
            "or use 'Recover to transit' with someone watching the arm")

    if target == TRANSIT:
        return out + [("turn", _pick(hub, TURN))]
    return out + [("turn", _pick(views[target], TURN))] + from_hub(target)


# ---------------------------------------------------------------- execution

def _ease(s: float) -> float:
    return s * s * (3 - 2 * s)


def _movable(arm) -> list[int]:
    present = arm.joints(max_age_s=STALE_S)
    grip = gripper_id(present)
    return [m for m in sorted(present) if m != grip]


async def _write(arm, goals, **kw):
    try:
        await asyncio.wait_for(arm.write_goals(goals, **kw), WRITE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise MotionError("Station did not accept a command in time")


async def hold(arm):
    """Freeze the arm where it is right now. Limp joints and the gripper are left alone."""
    tel = arm.telemetry(max_age_s=STALE_S)
    grip = gripper_id(tel)
    goals = {m: t["pos"] for m, t in tel.items() if m != grip and t["torque"]}
    if goals:
        await _write(arm, goals)


async def _safe_hold(arm):
    try:
        await hold(arm)
    except Exception as e:   # stale data or a dead link: the last goal was at most LEAD_LIMIT ahead
        logger.error("could not confirm hold: %s", e)


async def engage(arm):
    """Make a limp arm hold its present pose: goal := present, then torque on (gripper stays limp)."""
    ids = _movable(arm)
    present = arm.joints(max_age_s=STALE_S)
    await _write(arm, {m: present[m] for m in ids})
    await asyncio.wait_for(arm.set_torque(True, ids), WRITE_TIMEOUT_S)


async def release(arm):
    """Go limp (all joints). The arm drops: someone must be holding it."""
    if status["moving"]:
        raise MotionError("the arm is moving; press Stop first")
    await asyncio.wait_for(arm.set_torque(False), WRITE_TIMEOUT_S)


def _check_faults(tel, moving_ids, over_since, now, stats):
    for m, t in tel.items():
        if t["status"] & ~STATUS_IGNORE:
            raise MotionError(f"motor {m} reports a fault (status 0x{t['status']:02x})")
    for m in moving_ids:
        cur = tel[m]["current"]
        stats["max_current"][m] = max(stats["max_current"].get(m, 0), cur)
        if cur > CURRENT_LIMIT:
            over_since.setdefault(m, now)
            if now - over_since[m] > CURRENT_S:
                raise MotionError(f"motor {m} is drawing too much current ({cur}); it may be blocked")
        else:
            over_since.pop(m, None)


async def _run_segment(arm, name: str, goal: dict[int, int], peak_speed: float, stats: dict) -> float:
    """Move the joints in `goal` to their targets; every other joint must stay put."""
    tel = arm.telemetry(max_age_s=STALE_S)
    ids = sorted(goal)
    start = {m: tel[m]["pos"] for m in ids}
    dist = max(abs(goal[m] - start[m]) for m in ids)
    if dist <= SKIP_TOL:
        return 0.0
    held = {m: tel[m]["pos"] for m in MOVABLE if m in tel and m not in goal}
    duration = max(MIN_SEGMENT_S, 1.5 * dist / peak_speed)   # smoothstep peaks at 1.5x the average
    servo_speed = int(peak_speed * 1.5) + 50                 # the servo's own ceiling, just above ours

    s, last, stalled_since, over_since, first = 0.0, time.monotonic(), None, {}, True
    cmd = dict(start)
    while True:
        now = time.monotonic()
        tel = arm.telemetry(max_age_s=STALE_S)               # raises if the data is stale
        _check_faults(tel, ids, over_since, now, stats)
        for m, ref in held.items():
            if abs(tel[m]["pos"] - ref) > DRIFT_LIMIT:
                raise MotionError(f"motor {m} was pushed off its position while holding still")
        lag_m = max(ids, key=lambda m: abs(cmd[m] - tel[m]["pos"]))
        lag = abs(cmd[lag_m] - tel[lag_m]["pos"])
        stats["max_lag"][lag_m] = max(stats["max_lag"].get(lag_m, 0), lag)
        if lag <= LEAD_LIMIT:
            s = min(1.0, s + (now - last) / duration)        # only advance while the arm keeps up
            stalled_since = None
        else:
            stalled_since = stalled_since or now
            if now - stalled_since > STALL_S:
                raise MotionError(f"motor {lag_m} is not following ({lag} steps behind); it may be blocked")
        last = now
        status["progress"] = s
        k = _ease(s)
        cmd = {m: int(round(start[m] + (goal[m] - start[m]) * k)) for m in ids}
        if first:
            await _write(arm, cmd, speed=servo_speed, accel=SERVO_ACCEL)
            first = False
        else:
            await _write(arm, cmd)
        if s >= 1.0:
            break
        await asyncio.sleep(1 / RATE_HZ)

    # Arrived? Every moved joint close to its goal and standing still.
    deadline, prev = time.monotonic() + SETTLE_S, None
    while True:
        await asyncio.sleep(SETTLE_POLL_S)
        tel = arm.telemetry(max_age_s=STALE_S)
        _check_faults(tel, ids, over_since, time.monotonic(), stats)
        pos = {m: tel[m]["pos"] for m in ids}
        close = all(abs(pos[m] - goal[m]) <= ARRIVE_TOL for m in ids)
        still = prev is not None and all(abs(pos[m] - prev[m]) <= 6 for m in ids)
        if close and still:
            return duration
        if time.monotonic() > deadline:
            worst = max(ids, key=lambda m: abs(pos[m] - goal[m]))
            raise MotionError(f"motor {worst} stopped {abs(pos[worst] - goal[worst])} steps short of its goal "
                              f"in '{name}'; it may be blocked or overloaded")
        prev = pos


async def _preflight(arm, target: str | None):
    """Everything that must be true before the first goal is written. Returns (present, views)."""
    raw = _read_views_file()
    if TRANSIT not in raw:
        raise MotionError("no 'transit' pose saved yet; capture one on the capture page")
    tel = arm.telemetry(max_age_s=STALE_S)
    present = {m: t["pos"] for m, t in tel.items()}
    grip = gripper_id(present)
    if sorted(m for m in present if m != grip) != list(MOVABLE):
        raise MotionError(f"expected motors 1-8 on the bus, found {sorted(present)}")
    for name in {TRANSIT, target} - {None}:
        bus = raw[name].get("bus")
        if bus and bus != arm.bus_serial and not getattr(arm, "is_sim", False):
            raise MotionError(f"view '{name}' was captured on arm {bus}, but this is arm {arm.bus_serial}")
    for m, t in tel.items():
        if t["status"] & ~STATUS_IGNORE:
            raise MotionError(f"motor {m} reports a fault (status 0x{t['status']:02x})")
    limp = [m for m in MOVABLE if not tel[m]["torque"]]
    if limp:
        raise MotionError("the arm is limp; press 'Hold here' first (with the arm supported)")
    source = await arm.mirroring_source()
    if source:
        raise MotionError(f"Station is mirroring leader {source} onto this arm; "
                          "set the follower to '(Self-controlled)' in Station first")
    env = envelope(arm.ranges())
    for m, (lo, hi) in env.items():
        if not lo - 2 * AT_TOL <= present[m] <= hi + 2 * AT_TOL:
            raise MotionError(f"motor {m} reads {present[m]}, far outside its calibrated range; recalibrate")
    views = {n: clamp_pose(p, env) for n, p in load_views().items()}
    return present, views


def _log_move(record: dict):
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


async def describe(arm, name: str | None = None, allow_unknown_start: bool = False) -> dict:
    """Where the arm is and what a move to `name` would do, without moving anything."""
    try:
        present, views = await _preflight(arm, name)
    except MotionError as e:
        return {"ok": False, "error": str(e)}
    out = {"ok": True, "error": None, "where": locate(present, views)}
    if name:
        try:
            out["route"] = [seg for seg, _ in plan_route(present, name, views,
                                                         allow_unknown_start=allow_unknown_start)]
        except MotionError as e:
            out.update(ok=False, error=str(e))
    return out


async def go_to_view(arm, name: str, max_speed: float = SPEED_SLOW,
                     allow_unknown_start: bool = False) -> dict:
    """Move to a saved view. The interface everyone codes against: {"ok", "view", "error", ...}.

    Cancel the task running this to stop: the arm holds where it is and CancelledError propagates.
    `allow_unknown_start` is the supervised "recover to transit" path; it forces slow speed.
    """
    if name not in load_views():
        return {"ok": False, "view": name, "error": f"unknown view; known: {sorted(load_views())}"}
    if _move_lock.locked():
        return {"ok": False, "view": name, "error": "already moving"}
    async with _move_lock:
        t0 = time.time()
        stats = {"max_lag": {}, "max_current": {}}
        result = {"ok": False, "view": name, "error": None, "duration": 0.0, "route": []}
        status.update(moving=True, view=name, phase="check", phase_index=0, phase_count=0,
                      progress=0.0, error=None, route=[])
        started = False     # nothing has been commanded yet, so a refusal must not write anything
        try:
            present, views = await _preflight(arm, name)
            route = plan_route(present, name, views, allow_unknown_start=allow_unknown_start)
            start = locate(present, views).get("view")
            if allow_unknown_start or not all(view_checked(v) for v in (name, start) if v and v != TRANSIT):
                max_speed = min(max_speed, SPEED_SLOW)
            result["route"] = [seg for seg, _ in route]
            status.update(route=result["route"], phase_count=len(route))
            started = True
            for i, (seg, goal) in enumerate(route, 1):
                status.update(phase=seg, phase_index=i, progress=0.0)
                result["duration"] += await _run_segment(arm, seg, goal, max_speed, stats)
            result["ok"] = True
        except asyncio.CancelledError:
            if started:
                await _safe_hold(arm)
            result["error"] = "stopped"
            raise
        except (MotionError, RuntimeError) as e:    # RuntimeError: stale data, lost bus
            if started:
                await _safe_hold(arm)
            result["error"] = str(e)
        except Exception as e:
            if started:
                await _safe_hold(arm)
            result["error"] = f"unexpected error: {e!r}"
            logger.exception("move to %s failed", name)
        finally:
            result["duration"] = round(result["duration"], 2)
            status.update(moving=False, phase=None, error=result["error"], last_result=dict(result),
                          progress=1.0 if result["ok"] else status["progress"])
            _log_move({"t": t0, "took": round(time.time() - t0, 2), "speed": max_speed, **result, **stats})
    return result
