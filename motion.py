"""M2: move the follower arm to a saved view through a taught transit pose, in three ordered steps.

    await go_to_view(arm, "top")  ->  {"ok": bool, "view": str, "error": str | None, ...}

Design and rationale: docs/MOTION_PLAN.md. In short:

  * Every move runs on a fixed network. Each view has a "spoke" to the hub (the transit pose's
    pitch configuration):  rise (all four pitch joints together, view -> hub)
                           turn (base yaw + both rolls, at the hub)
                           descend (all four pitch joints together, hub -> view)
    Moving the pitch joints together keeps the arm between two poses a person taught; moving
    the shoulder on its own does not (from `home` it drives the gripper into the table).
  * A move only starts when the arm is somewhere on that network (at a view, on a spoke, at the
    hub). Anywhere else it refuses, unless the caller explicitly asks to recover to transit.
  * The arm must already be holding (torque on). A move never switches torque on or off.
  * The gripper (motor 8, the camera mount) is never written.
  * While moving: goals are streamed in small steps and a goal is only sent if it is within a
    short leash of where the arm really is. Any fault (not following, stale data, servo error,
    overload, a held joint pushed away, a joint going limp, another writer, a failed write,
    Stop) ends the move and holds the arm where it is; the result says whether that hold was
    confirmed by reading it back.
"""

import asyncio
import fcntl
import json
import time
from collections import deque
from pathlib import Path

from station_arm import logger

VIEWS_PATH = Path(__file__).parent / "views.json"
LOG_PATH = Path(__file__).parent / "motion_log.jsonl"
LOCK_PATH = Path(__file__).parent / ".motion.lock"
TRANSIT = "transit"

# Joint groups (ElRobot motor ids).
PITCH = (2, 3, 4, 6)   # shoulder, elbow, pitch, wrist pitch: always move together
TURN = (1, 5, 7)       # base yaw, forearm roll, wrist roll: only move at the hub
MOVABLE = tuple(sorted(PITCH + TURN))
GRIPPER = 8            # camera mount: never written
EXPECTED_MOTORS = MOVABLE + (GRIPPER,)

# Peak steps/s of the joint that travels furthest (4096 steps = one turn).
SPEED_FIRST = 100      # ~9 deg/s: forced for views nobody has checked on the real arm yet
SPEED_SLOW = 200       # ~18 deg/s
SPEED_NORMAL = 400     # ~35 deg/s
SPEED_FAST = 800       # ~70 deg/s for rise and turn; the descent into a view is capped at SPEED_NORMAL
SPEED_TURBO = 2000     # ~175 deg/s for rise and turn, run by the servo's own motion profile (see
                       # _run_segment); the descent is streamed at SPEED_FAST. The servo's limit is ~3400.
TURBO_ACCEL = 40       # register 0x29 for the joint that travels furthest (4000 steps/s^2)
TURBO_CURRENT_LIMIT = 380   # raw; accelerating hard draws more than CURRENT_LIMIT (stall is ~415)
TURBO_DRIFT_LIMIT = 100     # held joints get shaken more when the others move fast
HOLD_ACCEL = 100       # a Stop brakes joints that were being driven as hard as this allows
# Blending: at fast and turbo (checked views, desk assumed clear) the turn overlaps the rise and
# the descent, but only while the geometry model (fk.py) puts the forearm, wrist, gripper and
# camera at least this high above the desk. Below that line the moves stay strictly in sequence.
BLEND = True
CLEAR_HEIGHT_M = 0.25
CLEAR_POINTS = ("j5_forearm_roll", "j6_wrist_pitch", "j7_wrist_roll", "j8_gripper", "jaw_tip", "camera")
TEMP_LIMIT = 70        # deg C, sustained: stop before a servo cooks
MIN_SEGMENT_S = 0.8
RATE_HZ = 30
MAX_TICK_S = 2 / RATE_HZ   # a late tick never advances the path by more than this much time
MARGIN = 30            # steps kept clear of each calibrated end stop
STOP_CLEAR = 10        # a joint has only arrived if it rests at least this far inside its range
MIN_RANGE = 400        # a calibrated arc narrower than this is treated as not calibrated
RANGE_DRIFT = 15       # live calibration may differ this much from the one a view was captured with
SERVO_ACCEL = 15       # register 0x29, units of 100 steps/s^2

# All thresholds below are provisional until tuned from motion_log.jsonl on the real arm.
AT_TOL = 60            # steps: "the arm is at this view / on this spoke" when deciding where it is
SKIP_TOL = 10          # a segment whose joints are all this close to its goal is skipped
ARRIVE_TOL = 50        # after a segment (and before the next) joints must be this close to plan
                       # (a gravity-loaded ST3215 settles a few dozen steps short of its goal)
SETTLE_S = 2.0         # ...within this long, standing still, with the final goal read back
SETTLE_POLL_S = 0.15
STALL_S = 0.4          # the arm not catching up with the leash for this long = blocked
DRIFT_LIMIT = 60       # a joint that should be holding still has moved this far = pushed/sagging
HELD_GOAL_TOL = 80     # before a move, each joint's goal register must be this close to its position
FREEZE_TOL = 60        # Stop: a joint whose goal is this close to it keeps that goal (see hold())
RELAX_TOL = 25         # ...after a suspected blockage, only this close
STALE_S = 0.3          # joint data older than this is not trusted
FAULT_S = 0.25         # how long load / current / read errors / a foreign goal must persist
WRITE_TIMEOUT_S = 0.5
CONFIRM_S = 0.6        # how long to wait for a hold / engage to show up in the read-back
CURRENT_LIMIT = 250    # raw units of 6.5 mA (~1.6 A; stall is ~415 at 12 V)
LOAD_LIMIT = 800       # PWM duty 0-1000: this high for FAULT_S means the joint is stalled
GOAL_MATCH = 8         # steps: a goal register this close to something we wrote is ours
GOAL_HISTORY_S = 0.6   # how far back a read-back goal may lag our writes
STATUS_IGNORE = 0x01   # voltage bit; not a motion fault


def lead_limit(peak_speed: float) -> float:
    """Leash: how far a commanded goal may be ahead of the measured position (140 / 160 / 200 /
    260 steps at 100 / 200 / 400 / 800 steps/s). Bounds how far the arm can still travel if we
    go blind. From the first real session: the loaded shoulder rests 20-35 steps short of its
    goal, needed more than 60 to start lifting out of `top`, and 100 to lift out of `right`."""
    return min(260, 120 + 0.2 * peak_speed)


_move_lock = asyncio.Lock()
# What the current/last move did, for stop_hold(): did it send any goal, which joints was its
# current segment driving, and how did its own abort hold end.
_last = {"wrote": False, "driving": (), "hold": None}

# Live state of the current/last move, for UIs to poll. Updated in place, never replaced.
status = {"moving": False, "view": None, "phase": None, "phase_index": 0, "phase_count": 0,
          "progress": 0.0, "error": None, "last_result": None, "route": [], "hold": None}


class MotionError(RuntimeError):
    """A move was refused or aborted. The message is safe to show to the user."""


class BlockedError(MotionError):
    """The arm seems to be pushing against something: stop pushing, not just stop moving."""


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


def default_bus() -> str | None:
    """Serial of the arm the saved views belong to (the one recorded with the transit pose)."""
    views = _read_views_file()
    return views.get(TRANSIT, {}).get("bus") or next((v["bus"] for v in views.values() if v.get("bus")), None)


def view_checked(name: str) -> bool:
    """True once a person has watched this view's route run cleanly (go.py --mark-checked).
    Unchecked views only ever run at SPEED_FIRST. Re-capturing the view or the transit pose
    clears the flag."""
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


def view_problem(name: str, view: dict, ranges: dict[int, tuple[int, int]], arm) -> str | None:
    """Why this saved view cannot be trusted on this arm right now, or None.

    Views are raw encoder steps: only valid on the arm and with the calibration they were
    captured with (Station re-centres every joint when a calibration is saved)."""
    joints = view.get("joints", {})
    if any(str(m) not in joints for m in MOVABLE):
        return f"view '{name}' has no saved position for some joints; re-capture it"
    if not getattr(arm, "is_sim", False):
        if not view.get("bus"):
            return f"view '{name}' does not say which arm it was captured on; re-capture it"
        if view["bus"] != arm.bus_serial:
            return f"view '{name}' was captured on arm {view['bus']}, but this is arm {arm.bus_serial}"
    saved = view.get("ranges")
    if not saved:
        return f"view '{name}' has no record of the calibration it was captured with; re-capture it"
    for m in MOVABLE:
        lo, hi = saved.get(str(m), (0, 0))
        now_lo, now_hi = ranges.get(m, (0, 0))
        if abs(lo - now_lo) > RANGE_DRIFT or abs(hi - now_hi) > RANGE_DRIFT:
            return (f"the arm was recalibrated since '{name}' was captured (motor {m}: {lo}..{hi} then, "
                    f"{now_lo}..{now_hi} now), so its saved pose no longer points where it did; re-capture it")
    return None


# ---------------------------------------------------------------- where is the arm?

def _near(present, pose, ids, tol=AT_TOL) -> bool:
    return all(abs(present[m] - pose[m]) <= tol for m in ids)


def locate(present: dict[int, int], views: dict[str, dict[int, int]]) -> dict:
    """Classify the measured pose on the route network.

    {"where": "view", "view": name}    at a saved view
    {"where": "hub"}                   pitch joints at the transit configuration (any yaw/roll)
    {"where": "spoke", "view": name}   between a view and the hub: yaw/rolls are the view's and
                                       the pitch joints are all the same fraction of the way
    {"where": "unknown"}               anything else
    `views` must be clamped to the envelope, trusted (see view_problem) and include transit.
    This only decides which route applies; every route starts with a segment that pulls the
    joints onto the exact pose, so the tolerance here is not the accuracy of the move.
    """
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
        # Closest point on the straight line from the view (f=0) to the hub (f=1) in joint space.
        span = sum((hub[m] - pose[m]) ** 2 for m in PITCH)
        if span <= AT_TOL ** 2:
            continue
        f = sum((present[m] - pose[m]) * (hub[m] - pose[m]) for m in PITCH) / span
        f = min(max(f, 0.0), 1.0)
        miss = max(abs(present[m] - (pose[m] + (hub[m] - pose[m]) * f)) for m in PITCH)
        if miss <= AT_TOL and (best is None or miss < best[0]):
            best = (miss, {"where": "spoke", "view": name})
    return best[1] if best else {"where": "unknown"}


# ---------------------------------------------------------------- planning

def _pick(pose, ids):
    return {m: pose[m] for m in ids}


def plan_route(present: dict[int, int], target: str, views: dict[str, dict[int, int]],
               allow_unknown_start: bool = False) -> list[tuple[str, dict[int, int]]]:
    """Ordered segments [(name, {motor: goal})] from the measured pose to `target`.

    Each segment moves one joint group; everything else holds still. `views` must be clamped,
    trusted and include the transit pose. Raises MotionError when the arm is not on the network.
    Segments that turn out to need no motion are skipped when they run, so a route always
    states the full sequence: being "at the hub" within tolerance still gets an exact rise.
    """
    hub = views[TRANSIT]
    loc = locate(present, views)
    if loc["where"] == "unknown" and not allow_unknown_start:
        raise MotionError(
            "the arm is not at a known pose, so there is no checked path from here. "
            "Move it by hand to the transit pose (Go limp, pose it, Hold here)")
    if loc.get("view") == target and target != TRANSIT:
        # At the target or on its own spoke: straight down the spoke onto the exact pose.
        return [("descend", _pick(views[target], PITCH))]
    rise = ("rise", _pick(hub, PITCH))
    if target == TRANSIT:
        return [rise, ("turn", _pick(hub, TURN))]
    return [rise, ("turn", _pick(views[target], TURN)), ("descend", _pick(views[target], PITCH))]


# ---------------------------------------------------------------- talking to the arm

def _ease(s: float) -> float:
    return s * s * (3 - 2 * s)


async def _write(arm, goals, **kw):
    try:
        await asyncio.wait_for(arm.write_goals(goals, **kw), WRITE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise MotionError("Station did not accept a command in time")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise MotionError(f"Station rejected a command: {e!r}")


async def _torque(arm, on: bool, ids):
    try:
        await asyncio.wait_for(arm.set_torque(on, list(ids)), WRITE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise MotionError("Station did not accept a command in time")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise MotionError(f"Station rejected a command: {e!r}")


async def _confirm(arm, before: dict, goals: dict[int, int] | None = None, torque: bool | None = None,
                   ids=MOVABLE):
    """Wait until the arm shows the commanded state: every joint in `ids` read again since the
    command (`before` is the telemetry taken just before it), without a read error, with the
    goal registers in `goals` and the torque state `torque`, on two different reads in a row,
    so a look-alike value still on its way through does not count. Raises if it doesn't."""
    deadline = time.monotonic() + CONFIRM_S
    seen = None         # per-motor read stamps of the last sample that matched
    while True:
        tel = arm.telemetry(max_age_s=STALE_S)
        if (all(m in tel and not tel[m].get("read_error") and tel[m]["stamp"] != before[m]["stamp"] for m in ids)
                and all(abs(tel[m]["goal"] - g) <= GOAL_MATCH for m, g in (goals or {}).items())
                and (torque is None or all(tel[m]["torque"] == torque for m in ids))):
            stamps = {m: tel[m]["stamp"] for m in ids}
            if seen is not None and all(stamps[m] != seen[m] for m in ids):
                return
            seen = seen or stamps
        else:
            seen = None
        if time.monotonic() > deadline:
            raise MotionError("the arm did not confirm the command")
        await asyncio.sleep(0.05)


async def hold(arm, keep_within: int = FREEZE_TOL, moving: tuple = ()):
    """Stop the arm where it is and confirm it by reading the goals back.

    A resting joint whose goal register is within `keep_within` steps of its position keeps
    that goal: a loaded joint always rests a little short of its goal, so rewriting goal :=
    position would make it sag by that much again. Joints in `moving` (the ones a move was
    driving when it stopped) and joints whose goal is further away always get goal := position.
    Limp joints and the gripper are left alone. Returns "confirmed", or "limp" when no joint
    is energised. Raises (MotionError / RuntimeError) when the hold cannot be written or
    confirmed: the caller must tell the user it is unconfirmed."""
    tel = arm.telemetry(max_age_s=STALE_S)
    missing = [m for m in MOVABLE if m not in tel]
    if missing:
        raise MotionError(f"no data from motor {missing[0]}; cannot tell whether the arm is holding")
    held = [m for m in MOVABLE if tel[m]["torque"]]
    if not held:
        return "limp"           # nothing is energised, so there is nothing to hold
    # For a joint that was being driven, the register read-back lags our writes and the last
    # write may or may not have arrived, so its register cannot be trusted: always rewrite it.
    # Rewritten joints keep up to `keep_within` steps of the effort they had: goal := position
    # would drop a loaded joint by its sag every time a move is stopped.
    def near(m):
        pull = max(-keep_within, min(keep_within, tel[m]["goal"] - tel[m]["pos"]))
        return tel[m]["pos"] + pull

    goals = {m: near(m) for m in held
             if m in moving or abs(tel[m]["goal"] - tel[m]["pos"]) > keep_within}
    if goals:
        # Joints that were being driven brake as hard as the servo allows (matters after turbo,
        # where the servo was running towards a far goal on a gentle acceleration setting).
        await (_write(arm, goals, accel=HOLD_ACCEL) if moving else _write(arm, goals))
    # Confirmed only when reads taken after this point show every joint with the goal it should have.
    await _confirm(arm, tel, {m: goals.get(m, tel[m]["goal"]) for m in held}, ids=held)
    return "confirmed"


async def _abort_hold(arm, keep_within: int, moving: tuple = ()) -> str:
    """Hold after an abort or Stop. Returns "confirmed", "limp" or "unconfirmed".

    With stale data or a dead link we cannot hold and must not guess a position: every goal we
    sent was within the leash of where the arm was, so not writing is the safe choice."""
    inner = asyncio.ensure_future(hold(arm, keep_within, moving))
    while True:
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            # Being cancelled again must not leave this hold running behind our back (its write
            # could land after someone has let the arm go limp): see it through, it is bounded.
            if inner.done():
                raise
        except Exception as e:
            logger.error("HOLD NOT CONFIRMED: %s", e)
            return "unconfirmed"


def _basic_problems(arm, tel) -> list[str]:
    """Problems with the arm itself, independent of any saved view."""
    out = []
    if tuple(sorted(tel)) != EXPECTED_MOTORS:
        return [f"expected motors 1-8 on the bus, found {sorted(tel)}"]
    for m in MOVABLE:
        if tel[m]["status"] & ~STATUS_IGNORE:
            out.append(f"motor {m} reports a fault (status 0x{tel[m]['status']:02x})")
        if tel[m].get("read_error"):
            out.append(f"motor {m} is not answering: {tel[m]['read_error']}")
    return out


async def _mirroring_problem(arm) -> str | None:
    try:
        source = await arm.mirroring_source()
    except asyncio.CancelledError:
        raise
    except Exception as e:      # not being able to tell is a reason not to move
        return f"cannot tell whether Station is mirroring a leader onto this arm ({e})"
    if source:
        return (f"Station is mirroring leader {source} onto this arm; "
                "set the follower to '(Self-controlled)' in Station first")
    return None


async def engage(arm):
    """Make a limp arm hold its present pose (joints 1-7; the gripper stays limp).

    Goal := present with a low speed ceiling, then torque on, then read both back. Never torque
    on first: the goal registers hold stale values from whatever drove the arm last, and it
    would jump to them. Refuses on the wrong arm, with motors missing, with faults, or while
    Station mirrors a leader onto the arm."""
    if status["moving"]:
        raise MotionError("the arm is moving; press Stop first")
    problem = await _mirroring_problem(arm)
    tel = arm.telemetry(max_age_s=STALE_S)          # after the await, so it is current
    problems = ([problem] if problem else []) + _basic_problems(arm, tel)
    if not getattr(arm, "is_sim", False):
        buses = {v["bus"] for v in _read_views_file().values() if v.get("bus")}
        if buses and arm.bus_serial not in buses:
            problems.append(f"the saved views belong to arm {sorted(buses)[0]}, but this is arm {arm.bus_serial}")
    if problems:
        raise MotionError(problems[0])
    # A joint that is already holding close to its goal keeps that goal: rewriting it to the
    # (slightly sagged) present position would make it sag again on every press.
    goals = {m: tel[m]["pos"] for m in MOVABLE
             if not tel[m]["torque"] or abs(tel[m]["goal"] - tel[m]["pos"]) > FREEZE_TOL}
    if goals:
        await _write(arm, goals, speed=SPEED_FIRST, accel=SERVO_ACCEL)
    await _torque(arm, True, MOVABLE)
    await _confirm(arm, tel, {m: goals.get(m, tel[m]["goal"]) for m in MOVABLE}, torque=True)


async def release(arm):
    """Go limp (joints 1-7; the gripper is never written). The arm drops: someone must hold it."""
    if status["moving"]:
        raise MotionError("the arm is moving; press Stop first")
    before = arm.telemetry(max_age_s=STALE_S)
    await _torque(arm, False, MOVABLE)
    try:    # limp only counts on reads taken after the command, error-free, from all seven joints
        await _confirm(arm, before, torque=False)
    except (MotionError, RuntimeError) as e:
        raise MotionError(f"could not confirm the arm is limp ({e})")


async def stop_hold(arm):
    """The hold a controller issues after it has stopped (cancelled) a move, or found one crashed.

    If the move's own abort already held the arm, or the move never commanded anything, this is
    a plain hold(): resting joints keep their goals. Only when the move wrote goals and its own
    hold did not go through are the joints it was driving stopped where they are."""
    if _last["wrote"] and _last["hold"] not in ("confirmed", "limp"):
        return await hold(arm, RELAX_TOL, moving=_last["driving"])
    return await hold(arm)


# ---------------------------------------------------------------- execution

class _Watch:
    """Faults that must persist for FAULT_S before they end a move (one noisy sample is not a fault)."""

    def __init__(self):
        self.since: dict = {}

    def check(self, key, bad: bool, now: float, message: str, error=None):
        if not bad:
            self.since.pop(key, None)
        elif now - self.since.setdefault(key, now) > FAULT_S:
            raise (error or MotionError)(message)


OTHER_WRITER = "something else is commanding motor {}; is the follower still linked to a leader in Station?"


def _guard(tel, watch: _Watch, now: float, stats: dict, held_pos: dict, held_goal: dict,
           hard: tuple = (), drift_limit: int = DRIFT_LIMIT):
    """Checks that run on every sample from the first waypoint until the segment has settled.
    `hard`: joints being driven flat out (turbo); for them full PWM is expected, so only the
    higher current limit applies."""
    if tuple(sorted(tel)) != EXPECTED_MOTORS:
        raise MotionError(f"a motor dropped off the bus (found {sorted(tel)})")
    for m in MOVABLE:
        t = tel[m]
        if not t["torque"]:
            raise MotionError(f"motor {m} went limp during the move")
        if t["status"] & ~STATUS_IGNORE:
            raise MotionError(f"motor {m} reports a fault (status 0x{t['status']:02x})")
        watch.check(("read", m), bool(t.get("read_error")), now,
                    f"motor {m} is not answering: {t.get('read_error')}")
        stats["max_current"][m] = max(stats["max_current"].get(m, 0), t["current"])
        stats["max_load"][m] = max(stats["max_load"].get(m, 0), t["load"])
        stats["max_temp"] = max(stats.get("max_temp", 0), t.get("temp", 0))
        watch.check(("temp", m), t.get("temp", 0) >= TEMP_LIMIT, now,
                    f"motor {m} is too hot ({t.get('temp')} C); let it cool down")
        watch.check(("current", m), t["current"] > (TURBO_CURRENT_LIMIT if m in hard else CURRENT_LIMIT), now,
                    f"motor {m} is drawing too much current ({t['current']}); it may be blocked", BlockedError)
        watch.check(("load", m), m not in hard and t["load"] > LOAD_LIMIT, now,
                    f"motor {m} is pushing at full effort (load {t['load']}); it may be blocked", BlockedError)
    for m, g in held_goal.items():      # a joint we are not moving must keep the goal it had
        if abs(tel[m]["goal"] - g) > GOAL_MATCH:
            raise MotionError(OTHER_WRITER.format(m))
    for m, ref in held_pos.items():
        if abs(tel[m]["pos"] - ref) > drift_limit:
            raise BlockedError(f"motor {m} was pushed off its position while holding still")


async def _run_segment(arm, name: str, goal: dict[int, int], peak_speed: float,
                       env: dict, stats: dict, ctx: dict, planned: dict, turbo: bool = False,
                       others: tuple = (), gate=None) -> float:
    """Move the joints in `goal` to their targets; every other joint must stay put.
    `planned` gives, for joints an earlier segment moved, where they are supposed to be.

    Normally the path is streamed in small waypoints on a leash. With `turbo` the final goal
    is written once and each servo runs its own speed/acceleration profile: much faster, but
    the goal is far ahead of the arm, so what protects it is the progress watchdog, the other
    guards and a hard-braking Stop, not the leash.

    `others`: joints another segment is driving at the same time (blended moves); this segment
    does not expect them to hold still. `gate(waypoint)`: a streamed segment only advances to a
    waypoint the gate allows (the descent waits above the clear height until the turn is done)."""
    tel = arm.telemetry(max_age_s=STALE_S)
    ids = sorted(goal)
    watch = _Watch()
    # A held joint must stay near its planned place (e.g. the hub during a turn), not merely
    # near wherever it happened to be when this segment started.
    held_pos = {m: planned.get(m, tel[m]["pos"]) for m in MOVABLE if m not in goal and m not in others}
    held_goal = {m: tel[m]["goal"] for m in MOVABLE if m not in goal and m not in others}

    def resting_at_goal(t):   # at the goal, and its own goal register is right there too
        return all(abs(goal[m] - t[m]["pos"]) <= SKIP_TOL and abs(t[m]["goal"] - goal[m]) <= HELD_GOAL_TOL
                   for m in ids)

    if resting_at_goal(tel):
        # Nothing to do, but only if the joints are really resting there: guard both looks,
        # and the second must be a newer read that has not moved.
        _guard(tel, watch, time.monotonic(), stats, held_pos, held_goal)
        await asyncio.sleep(SETTLE_POLL_S)
        again = arm.telemetry(max_age_s=STALE_S)
        _guard(again, watch, time.monotonic(), stats, held_pos, held_goal)
        if (resting_at_goal(again)
                and all(again[m]["stamp"] != tel[m]["stamp"] for m in ids)
                and all(abs(again[m]["pos"] - tel[m]["pos"]) <= 6 for m in ids)):
            return 0.0
        tel = again
    # Start from the goal the joint is already holding (a loaded joint rests a little short of
    # it); starting from the measured position would first take that holding effort away and
    # let the arm dip. Only if the register is far off do we start from where the joint is.
    start = {m: tel[m]["goal"] if abs(tel[m]["goal"] - tel[m]["pos"]) <= HELD_GOAL_TOL else tel[m]["pos"]
             for m in ids}
    dist = max(abs(goal[m] - start[m]) for m in ids)
    duration = max(MIN_SEGMENT_S, 1.5 * dist / peak_speed)   # smoothstep peaks at 1.5x the average
    servo_speed = int(peak_speed * 1.5) + 50                 # the servo's own ceiling, just above ours
    servo_accel = max(SERVO_ACCEL, int(peak_speed / 20))     # enough acceleration to keep up when fast
    leash = lead_limit(peak_speed)

    def waypoint(s):   # inside the calibrated envelope even if the arm started just outside it
        k = _ease(s)
        return {m: min(max(int(round(start[m] + (goal[m] - start[m]) * k)), env[m][0]), env[m][1]) for m in ids}

    sent: deque = deque()                                    # (time, goals) we wrote recently
    if turbo:
        final = {m: min(max(goal[m], env[m][0]), env[m][1]) for m in ids}
        todo = {m: abs(final[m] - tel[m]["pos"]) for m in ids}
        far = max(max(todo.values()), 1)
        # Slower joints get proportionally less speed and acceleration, so all arrive together
        # and the arm stays close to the straight line between the two taught poses.
        speeds = {m: max(60, int(peak_speed * todo[m] / far)) for m in ids}
        accels = {m: max(5, int(TURBO_ACCEL * todo[m] / far)) for m in ids}
        expected = far / peak_speed + peak_speed / (TURBO_ACCEL * 100)
        ctx["wrote"] = _last["wrote"] = True
        await _write(arm, final, speed=speeds, accel=accels)
        t0 = best_t = time.monotonic()
        sent.append((t0, final))
        best = far
        while True:
            await asyncio.sleep(1 / RATE_HZ)
            now = time.monotonic()
            tel = arm.telemetry(max_age_s=STALE_S)
            _guard(tel, watch, now, stats, held_pos, held_goal, hard=tuple(ids), drift_limit=TURBO_DRIFT_LIMIT)
            watch.check("foreign", now - t0 > GOAL_HISTORY_S
                        and any(abs(tel[m]["goal"] - final[m]) > GOAL_MATCH for m in ids), now,
                        OTHER_WRITER.format(next((m for m in ids if abs(tel[m]["goal"] - final[m]) > GOAL_MATCH), "")))
            worst = max(ids, key=lambda m: abs(final[m] - tel[m]["pos"]))
            left = abs(final[worst] - tel[worst]["pos"])
            status["progress"] = max(0.0, 1 - left / far)
            if left <= ARRIVE_TOL:
                break
            if left < best - 8:
                best, best_t = left, now
            elif now - best_t > 0.5:
                raise BlockedError(f"motor {worst} stopped making progress {left} steps from its goal; it may be blocked")
            if now - t0 > 2 * expected + 2:
                raise BlockedError(f"'{name}' is taking far longer than it should; stopping")
        cmd, duration = final, time.monotonic() - t0
    s, last, stalled_since, first = (1.0 if turbo else 0.0), time.monotonic(), None, True
    cmd = cmd if turbo else dict(start)
    while not turbo:
        now = time.monotonic()
        tel = arm.telemetry(max_age_s=STALE_S)               # raises if any motor's data is stale
        _guard(tel, watch, now, stats, held_pos, held_goal)
        while sent and now - sent[0][0] > GOAL_HISTORY_S:
            sent.popleft()
        # A moving joint's goal register must be something we sent recently (read-back lags a little).
        foreign = [m for m in ids if sent and all(abs(tel[m]["goal"] - c[m]) > GOAL_MATCH for _, c in sent)]
        watch.check("foreign", bool(foreign) and not first and len(sent) > 3, now,
                    OTHER_WRITER.format(foreign[0] if foreign else ""))

        # Leash: only send the next waypoint if every joint's goal stays close to where it really is.
        s_next = min(1.0, s + min(now - last, MAX_TICK_S) / duration)
        nxt = waypoint(s_next)
        lag_m = max(ids, key=lambda m: abs(nxt[m] - tel[m]["pos"]))
        lead = abs(nxt[lag_m] - tel[lag_m]["pos"])
        if lead <= leash and gate is not None and s_next > s and not gate(nxt):
            stalled_since = None                             # waiting at the gate, not blocked
        elif lead <= leash:
            s, cmd, stalled_since = s_next, nxt, None
            stats["max_lag"][lag_m] = max(stats["max_lag"].get(lag_m, 0), lead)
        else:
            stalled_since = stalled_since or now
            if now - stalled_since > STALL_S:
                raise BlockedError(f"motor {lag_m} is not following ({lead} steps behind); it may be blocked")
        last = now
        status["progress"] = s
        if stalled_since is None:           # over the leash nothing is sent, not even a repeat
            ctx["wrote"] = _last["wrote"] = True
            if first:
                await _write(arm, cmd, speed=servo_speed, accel=servo_accel)
                first = False
            else:
                await _write(arm, cmd)
            sent.append((time.monotonic(), cmd))
        if s >= 1.0:
            break
        await asyncio.sleep(1 / RATE_HZ)

    # Arrived? On two different fresh samples: the final goal read back, every moved joint close
    # to it and standing still. All guards stay on until then.
    final = cmd
    deadline, prev = time.monotonic() + SETTLE_S, None
    while True:
        await asyncio.sleep(SETTLE_POLL_S)
        now = time.monotonic()
        tel = arm.telemetry(max_age_s=STALE_S)
        _guard(tel, watch, now, stats, held_pos, held_goal,
               drift_limit=TURBO_DRIFT_LIMIT if turbo else DRIFT_LIMIT)
        echoed = all(abs(tel[m]["goal"] - final[m]) <= GOAL_MATCH for m in ids)
        if not echoed and now - sent[-1][0] > GOAL_HISTORY_S:
            bad = next(m for m in ids if abs(tel[m]["goal"] - final[m]) > GOAL_MATCH)
            raise MotionError(OTHER_WRITER.format(bad))
        # Close to the goal, and not resting against a mechanical stop outside the allowed range.
        close = all(abs(tel[m]["pos"] - final[m]) <= ARRIVE_TOL
                    and env[m][0] - MARGIN + STOP_CLEAR <= tel[m]["pos"] <= env[m][1] + MARGIN - STOP_CLEAR
                    for m in ids)
        fresh = prev is not None and all(tel[m]["stamp"] != prev[m]["stamp"] for m in ids)
        still = fresh and all(abs(tel[m]["pos"] - prev[m]["pos"]) <= 6 for m in ids)
        if echoed and close and still:
            return duration
        if now > deadline:
            worst = max(ids, key=lambda m: abs(tel[m]["pos"] - final[m]))
            raise BlockedError(f"motor {worst} stopped {abs(tel[worst]['pos'] - final[worst])} steps short of "
                               f"its goal in '{name}'; it may be blocked or overloaded")
        if prev is None or fresh:
            prev = tel


def _height(pose: dict[int, int], ranges) -> float:
    """Lowest of the forearm, wrist, gripper and camera above the desk, in metres, by the geometry
    model (the lower of its two joint-angle mappings)."""
    import fk
    return min(fk.fk(pose, ranges, mapping=mapping)[point][2]
               for mapping in ("viewer", "true_scale") for point in CLEAR_POINTS)


def _segment_speed(seg: str, max_speed: float) -> tuple[float, bool]:
    """(speed, turbo) for one segment. Turbo: rise and turn on the servos' own profile, the
    descent streamed at fast. Otherwise the descent is capped at normal: coming down onto a
    view is where an overshoot would matter."""
    if seg != "descend":
        return max_speed, max_speed >= SPEED_TURBO
    return (SPEED_FAST if max_speed >= SPEED_TURBO else min(max_speed, SPEED_NORMAL)), False


async def _run_blended(arm, route, max_speed: float, env: dict, stats: dict, ctx: dict) -> float:
    """Run rise / turn / descend with the turn overlapping the other two above CLEAR_HEIGHT_M.

      rise starts at once; the turn starts as soon as the measured pose is above the clear
      height (or the rise is done); the descent starts when the rise is done but may only go
      below the clear height once the turn has arrived.
    Each segment keeps its own guards and arrival check. If one fails, all are stopped."""
    segs = dict(route)
    ranges = arm.ranges()
    grip = arm.telemetry(max_age_s=STALE_S)[GRIPPER]["pos"]
    tasks: dict[str, asyncio.Future] = {}
    t0 = time.monotonic()

    def height(overrides: dict) -> float:     # the measured pose, with these joints replaced
        tel = arm.telemetry(max_age_s=STALE_S)
        return _height({**{m: tel[m]["pos"] for m in MOVABLE}, **overrides, GRIPPER: grip}, ranges)

    def start(name: str, **kw):
        speed, turbo = _segment_speed(name, max_speed)
        others = tuple(m for n, g in segs.items() if n != name for m in g if m not in segs[name])
        tasks[name] = asyncio.ensure_future(_run_segment(
            arm, name, segs[name], speed, env, stats, ctx, {}, turbo=turbo, others=others, **kw))

    async def until(done):
        while True:
            running = [n for n, t in tasks.items() if not t.done()]
            ctx["moving"] = _last["driving"] = tuple(sorted({m for n in running for m in segs[n]}))
            status.update(phase="+".join(running) or status["phase"], phase_index=len(tasks))
            for t in tasks.values():
                if t.done() and not t.cancelled() and t.exception():
                    raise t.exception()
            if done():
                return
            await asyncio.sleep(1 / RATE_HZ)

    def finished(name: str) -> bool:
        return name not in segs or (name in tasks and tasks[name].done())

    try:
        if "rise" in segs:
            start("rise")
        if "turn" in segs:
            await until(lambda: finished("rise") or height({}) >= CLEAR_HEIGHT_M)
            start("turn")
        await until(lambda: finished("rise"))
        if "descend" in segs:
            start("descend", gate=lambda waypoint: finished("turn") or height(waypoint) >= CLEAR_HEIGHT_M)
        await until(lambda: all(t.done() for t in tasks.values()))
        for t in tasks.values():
            t.result()
    except BaseException:
        for t in tasks.values():
            t.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
    ctx["moving"] = _last["driving"] = ()
    return time.monotonic() - t0


async def _inspect(arm, target: str | None = None) -> dict:
    """Read-only look at the arm. Returns {"blockers": [...], "where", "present", "views", "env"}.

    `blockers` lists every reason a move cannot start right now (empty = good to go).
    `where` is filled in whenever the joints are readable and transit is trusted, also while
    the arm is limp, so a person can see the arm reach the hub while posing it by hand."""
    out = {"blockers": [], "where": None, "present": None, "views": None, "env": None}
    blockers = out["blockers"]
    raw = _read_views_file()
    if TRANSIT not in raw:
        blockers.append("no 'transit' pose saved yet; capture one on the capture page")
    problem = await _mirroring_problem(arm)
    if problem:
        blockers.append(problem)
    try:
        tel = arm.telemetry(max_age_s=STALE_S)      # after the await above, so it is current
    except RuntimeError as e:
        blockers.append(str(e))
        return out
    basic = _basic_problems(arm, tel)
    blockers.extend(basic)
    if tuple(sorted(tel)) != EXPECTED_MOTORS:
        return out
    present = {m: t["pos"] for m, t in tel.items()}
    ranges = arm.ranges()
    try:
        env = envelope(ranges)
    except MotionError as e:
        blockers.append(str(e))
        return out
    for m, (lo, hi) in env.items():
        if not lo - AT_TOL <= present[m] <= hi + AT_TOL:
            blockers.append(f"motor {m} reads {present[m]}, outside its calibrated range {lo - MARGIN}..{hi + MARGIN}; recalibrate")
    # Only views that are valid for this arm and this calibration take part in anything.
    problems = {name: view_problem(name, view, ranges, arm) for name, view in raw.items()}
    for name in (TRANSIT, target):
        if name and problems.get(name):
            blockers.append(problems[name])
    views = {name: clamp_pose({int(k): v for k, v in view["joints"].items()}, env)
             for name, view in raw.items() if not problems[name]}
    out.update(present=present, views=views, env=env)
    if TRANSIT in views:
        out["where"] = locate(present, views)
    limp = [m for m in MOVABLE if not tel[m]["torque"]]
    if limp:
        blockers.append("the arm is limp; press 'Hold here' first (with the arm supported)")
    else:
        for m in MOVABLE:
            if abs(tel[m]["goal"] - tel[m]["pos"]) > HELD_GOAL_TOL:
                blockers.append(f"motor {m} is being driven somewhere else (goal {tel[m]['goal']}, "
                                f"position {tel[m]['pos']}); press 'Hold here' to reset it")
    return out


def _log_move(record: dict):
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


async def describe(arm, name: str | None = None, allow_unknown_start: bool = False) -> dict:
    """Where the arm is and whether a move (to `name`) could start now. Moves nothing.

    {"ok": bool, "error": first blocker or None, "blockers": [...], "where": {...} | None,
     "route": [segment names]}"""
    try:
        info = await _inspect(arm, name)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e), "blockers": [str(e)], "where": None, "route": []}
    blockers, route = list(info["blockers"]), []
    if (info["where"] or {}).get("where") == "unknown" and not allow_unknown_start:
        blockers.append("the arm is not at a known pose; move it by hand to the transit pose "
                        "(Go limp, pose it, Hold here)")
    if name and info["views"] and name in info["views"] and TRANSIT in info["views"]:
        try:
            route = [seg for seg, _ in plan_route(info["present"], name, info["views"],
                                                  allow_unknown_start=allow_unknown_start)]
        except MotionError as e:
            if str(e) not in blockers and "not at a known pose" not in str(e):
                blockers.append(str(e))
    return {"ok": not blockers, "error": blockers[0] if blockers else None, "blockers": blockers,
            "where": info["where"], "route": route}


async def go_to_view(arm, name: str, max_speed: float = SPEED_SLOW,
                     allow_unknown_start: bool = False) -> dict:
    """Move to a saved view. The interface everyone codes against:
    {"ok", "view", "error", "duration", "route", "hold"}.

    Cancel the task running this to stop: the arm holds where it is and CancelledError propagates.
    `allow_unknown_start` is the supervised "recover to transit" path; it forces SPEED_FIRST.
    `hold` is None when nothing was commanded, else "confirmed" / "unconfirmed" after an abort.
    """
    refused = {"ok": False, "view": name, "duration": 0.0, "route": [], "hold": None}   # same keys as a result
    if name not in load_views():
        return {**refused, "error": f"unknown view; known: {sorted(load_views())}"}
    if _move_lock.locked():
        return {**refused, "error": "already moving"}
    lock_file = None
    if not getattr(arm, "is_sim", False):
        # One program moves the arm at a time: a Stop in the web page cannot stop a move that
        # go.py or the director started from another process.
        lock_file = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return {**refused, "error": "another program is moving the arm"}
    try:
        return await _go_to_view(arm, name, max_speed, allow_unknown_start)
    finally:
        if lock_file:
            lock_file.close()


async def _go_to_view(arm, name: str, max_speed: float, allow_unknown_start: bool) -> dict:
    async with _move_lock:
        t0 = time.time()
        stats = {"max_lag": {}, "max_current": {}, "max_load": {}}
        result = {"ok": False, "view": name, "error": None, "duration": 0.0, "route": [], "hold": None}
        status.update(moving=True, view=name, phase="check", phase_index=0, phase_count=0,
                      progress=0.0, error=None, route=[], hold=None)
        # Until a waypoint is sent, a refusal must not write anything.
        # "moving": the joints the current segment is driving (empty between segments).
        ctx = {"wrote": False, "moving": ()}
        _last.update(wrote=False, driving=(), hold=None)

        async def abort(reason: str, blocked: bool = False):
            result["error"] = reason
            if ctx["wrote"]:
                result["hold"] = await _abort_hold(arm, RELAX_TOL if blocked else FREEZE_TOL,
                                                   ctx["moving"])
                _last["hold"] = result["hold"]
                if result["hold"] == "unconfirmed":
                    result["error"] = f"{reason}; HOLD NOT CONFIRMED"

        try:
            info = await _inspect(arm, name)
            if info["blockers"]:
                raise MotionError(info["blockers"][0])
            present, views, env = info["present"], info["views"], info["env"]
            route = plan_route(present, name, views, allow_unknown_start=allow_unknown_start)
            start = info["where"].get("view")
            checked = [view_checked(v) for v in (name, start) if v and v != TRANSIT]
            if allow_unknown_start or not checked or not all(checked):
                # Unchecked view, or no checked view involved at all (hub -> transit): slowest speed.
                max_speed = min(max_speed, SPEED_FIRST)
            result["route"] = [seg for seg, _ in route]
            status.update(route=result["route"], phase_count=len(route))
            blend = BLEND and max_speed >= SPEED_FAST and len(route) > 1
            if blend:
                try:
                    import fk  # noqa: F401  (the clear-height check needs the geometry model)
                except Exception:
                    blend = False
            if blend:
                status.update(phase="+".join(result["route"][:1]), phase_index=1, progress=0.0)
                result["duration"] += await _run_blended(arm, route, max_speed, env, stats, ctx)
            else:
                expect = dict(present)      # where each joint should be as the route progresses
                moved: set[int] = set()
                for i, (seg, goal) in enumerate(route, 1):
                    status.update(phase=seg, phase_index=i, progress=0.0)
                    # Hand-off: joints this segment does not move must be where earlier segments left them.
                    now = arm.joints(max_age_s=STALE_S)
                    for m in moved - set(goal):
                        if abs(now[m] - expect[m]) > ARRIVE_TOL:
                            raise MotionError(f"motor {m} is {abs(now[m] - expect[m])} steps from where '{seg}' "
                                              "needs it to be; not continuing")
                    ctx["moving"] = _last["driving"] = tuple(goal)
                    seg_speed, turbo = _segment_speed(seg, max_speed)
                    result["duration"] += await _run_segment(arm, seg, goal, seg_speed, env, stats, ctx,
                                                             {m: expect[m] for m in moved}, turbo=turbo)
                    ctx["moving"] = _last["driving"] = ()
                    expect.update(goal)
                    moved |= set(goal)
            result["ok"] = True
        except asyncio.CancelledError:
            # Stop may have been pressed because the arm touches something: stop pushing too.
            await abort("stopped", blocked=True)
            raise
        except BlockedError as e:
            await abort(str(e), blocked=True)
        except (MotionError, RuntimeError) as e:    # RuntimeError: stale data, lost bus
            await abort(str(e))
        except Exception as e:
            logger.exception("move to %s failed", name)
            await abort(f"unexpected error: {e!r}")
        finally:
            result["duration"] = round(result["duration"], 2)
            status.update(moving=False, phase=None, error=result["error"], hold=result["hold"],
                          last_result=dict(result),
                          progress=1.0 if result["ok"] else status["progress"])
            if not getattr(arm, "is_sim", False):    # the log is for tuning from real runs only
                _log_move({"t": t0, "took": round(time.time() - t0, 2), "speed": max_speed, **result, **stats})
    return result
