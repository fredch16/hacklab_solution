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
  * The arm must already be holding (torque on). We never switch torque on or off here.
  * The gripper (motor 8, the camera mount) is never written.
  * While moving: goals are streamed in small steps and only advance while the arm keeps up,
    so the commanded position is never far from the measured one. Any fault (not following,
    stale data, servo error, overload, a held joint pushed away, another writer, a failed
    write, Stop) ends the move and holds the arm where it is.
"""

import asyncio
import json
import time
from collections import deque
from pathlib import Path

from station_arm import logger

VIEWS_PATH = Path(__file__).parent / "views.json"
LOG_PATH = Path(__file__).parent / "motion_log.jsonl"
TRANSIT = "transit"

# Joint groups (ElRobot motor ids).
PITCH = (2, 3, 4, 6)   # shoulder, elbow, pitch, wrist pitch: always move together
TURN = (1, 5, 7)       # base yaw, forearm roll, wrist roll: only move at the hub
MOVABLE = tuple(sorted(PITCH + TURN))
GRIPPER = 8            # camera mount: never written
EXPECTED_MOTORS = MOVABLE + (GRIPPER,)

SPEED_SLOW = 200       # peak steps/s of the joint that travels furthest (~18 deg/s)
SPEED_NORMAL = 400     # (~35 deg/s)
MIN_SEGMENT_S = 0.8
RATE_HZ = 30
MARGIN = 30            # steps kept clear of each calibrated end stop
MIN_RANGE = 400        # a calibrated arc narrower than this is treated as not calibrated
RANGE_DRIFT = 15       # live calibration may differ this much from the one a view was captured with
SERVO_ACCEL = 15       # register 0x29, units of 100 steps/s^2

AT_TOL = 60            # steps: "the arm is at this pose / on this spoke"
SKIP_TOL = 15          # a segment whose joints are all this close to its goal is skipped
ARRIVE_TOL = 40        # after a segment every moved joint must be this close to its goal
SETTLE_S = 2.0         # ...within this long, and standing still
SETTLE_POLL_S = 0.15
LEAD_LIMIT = 120       # leash: the commanded position never leads the measured one by more
STALL_S = 0.4          # not catching up for this long = blocked
DRIFT_LIMIT = 150      # a joint that should be holding still has moved this far = pushed/sagging
STALE_S = 0.3          # joint data older than this is not trusted
FAULT_S = 0.25         # how long load / current / read errors / a foreign goal must persist
WRITE_TIMEOUT_S = 0.5
CURRENT_LIMIT = 250    # raw units of 6.5 mA (~1.6 A; stall is ~415 at 12 V). Provisional
LOAD_LIMIT = 800       # PWM duty 0-1000: this high for FAULT_S means the joint is stalled
GOAL_MATCH = 8         # steps: a goal register this close to something we wrote is ours
STATUS_IGNORE = 0x01   # voltage bit; not a motion fault

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


def check_calibration(name: str, ranges: dict[int, tuple[int, int]]):
    """Views are raw encoder steps, so they are only valid for the calibration they were captured
    with: Station re-centres every joint when a calibration is saved."""
    saved = _read_views_file().get(name, {}).get("ranges")
    if not saved:
        return
    for m in MOVABLE:
        lo, hi = saved[str(m)]
        now_lo, now_hi = ranges.get(m, (0, 0))
        if abs(lo - now_lo) > RANGE_DRIFT or abs(hi - now_hi) > RANGE_DRIFT:
            raise MotionError(
                f"the arm was recalibrated since '{name}' was captured (motor {m}: {lo}..{hi} then, "
                f"{now_lo}..{now_hi} now), so its saved pose no longer points where it did; re-capture it")


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
    `views` must already be clamped to the envelope and include the transit pose.
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
        # Fraction of the way from the view (0) to the hub (1), from the joint that travels furthest.
        lead = max(PITCH, key=lambda m: abs(hub[m] - pose[m]))
        if abs(hub[lead] - pose[lead]) <= AT_TOL:
            continue
        f = (present[lead] - pose[lead]) / (hub[lead] - pose[lead])
        if not -0.05 <= f <= 1.05:
            continue
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

    Each segment moves one joint group; everything else holds still. `views` must be clamped
    and include the transit pose. Raises MotionError when the arm is not on the network.
    """
    hub = views[TRANSIT]
    loc = locate(present, views)
    rise = [("rise", _pick(hub, PITCH))]

    if loc["where"] == "view" and loc["view"] == target:
        return []
    if loc["where"] == "spoke" and loc["view"] == target:
        return [("descend", _pick(views[target], PITCH))]      # straight back down its own spoke
    if loc["where"] in ("view", "spoke"):
        out = rise
    elif loc["where"] == "hub":
        out = []
    elif allow_unknown_start:
        out = rise
    else:
        raise MotionError(
            "the arm is not at a known pose, so there is no checked path from here. "
            "Move it by hand to the transit pose (Go limp, pose it, Hold here)")

    if target == TRANSIT:
        return out + [("turn", _pick(hub, TURN))]
    return out + [("turn", _pick(views[target], TURN)), ("descend", _pick(views[target], PITCH))]


# ---------------------------------------------------------------- execution

def _ease(s: float) -> float:
    return s * s * (3 - 2 * s)


async def _write(arm, goals, **kw):
    try:
        await asyncio.wait_for(arm.write_goals(goals, **kw), WRITE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise MotionError("Station did not accept a command in time")


async def hold(arm):
    """Freeze the arm where it is right now. Limp joints and the gripper are left alone."""
    tel = arm.telemetry(max_age_s=STALE_S)
    goals = {m: tel[m]["pos"] for m in MOVABLE if m in tel and tel[m]["torque"]}
    if goals:
        await _write(arm, goals)


async def _safe_hold(arm):
    """Hold after an abort. With stale data or a dead link we cannot, and must not guess: the
    last goal we wrote is at most LEAD_LIMIT from where the arm was, so not writing is safe."""
    try:
        await asyncio.shield(hold(arm))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("could not confirm hold: %s", e)


async def engage(arm):
    """Make a limp arm hold its present pose (gripper stays limp).

    Goal := present with a low speed ceiling, then torque on. Never torque on first: the goal
    registers hold stale values from whatever drove the arm last, and it would jump to them."""
    tel = arm.telemetry(max_age_s=STALE_S)
    ids = [m for m in MOVABLE if m in tel]
    await _write(arm, {m: tel[m]["pos"] for m in ids}, speed=SPEED_SLOW, accel=SERVO_ACCEL)
    await asyncio.wait_for(arm.set_torque(True, ids), WRITE_TIMEOUT_S)


async def release(arm):
    """Go limp (all joints). The arm drops: someone must be holding it."""
    if status["moving"]:
        raise MotionError("the arm is moving; press Stop first")
    await asyncio.wait_for(arm.set_torque(False), WRITE_TIMEOUT_S)


class _Watch:
    """Faults that must persist for FAULT_S before they end a move (one noisy sample is not a fault)."""

    def __init__(self):
        self.since: dict[tuple, float] = {}

    def check(self, key, bad: bool, now: float, message: str):
        if not bad:
            self.since.pop(key, None)
        elif now - self.since.setdefault(key, now) > FAULT_S:
            raise MotionError(message)


def _check_faults(tel, moving_ids, watch: _Watch, now: float, stats: dict):
    for m in MOVABLE:
        t = tel[m]
        if t["status"] & ~STATUS_IGNORE:
            raise MotionError(f"motor {m} reports a fault (status 0x{t['status']:02x})")
        watch.check(("read", m), bool(t.get("read_error")), now,
                    f"motor {m} is not answering: {t.get('read_error')}")
        stats["max_temp"] = max(stats.get("max_temp", 0), t.get("temp", 0))
    for m in moving_ids:
        cur, load = tel[m]["current"], tel[m]["load"]
        stats["max_current"][m] = max(stats["max_current"].get(m, 0), cur)
        stats["max_load"][m] = max(stats["max_load"].get(m, 0), load)
        watch.check(("current", m), cur > CURRENT_LIMIT, now,
                    f"motor {m} is drawing too much current ({cur}); it may be blocked")
        watch.check(("load", m), load > LOAD_LIMIT, now,
                    f"motor {m} is pushing at full effort (load {load}); it may be blocked")


async def _run_segment(arm, name: str, goal: dict[int, int], peak_speed: float, stats: dict) -> float:
    """Move the joints in `goal` to their targets; every other joint must stay put."""
    tel = arm.telemetry(max_age_s=STALE_S)
    ids = sorted(goal)
    start = {m: tel[m]["pos"] for m in ids}
    dist = max(abs(goal[m] - start[m]) for m in ids)
    if dist <= SKIP_TOL:
        return 0.0
    held_pos = {m: tel[m]["pos"] for m in MOVABLE if m not in goal}
    held_goal = {m: tel[m]["goal"] for m in MOVABLE if m not in goal}
    duration = max(MIN_SEGMENT_S, 1.5 * dist / peak_speed)   # smoothstep peaks at 1.5x the average
    servo_speed = int(peak_speed * 1.5) + 50                 # the servo's own ceiling, just above ours

    s, last, stalled_since, first = 0.0, time.monotonic(), None, True
    watch = _Watch()
    cmd = dict(start)
    sent = deque([dict(start)], maxlen=12)                   # what we wrote over the last ~0.4 s
    while True:
        now = time.monotonic()
        tel = arm.telemetry(max_age_s=STALE_S)               # raises if any motor's data is stale
        _check_faults(tel, ids, watch, now, stats)
        # Is something else (Station mirroring, another program) writing goals to this arm?
        # A held joint's goal must not change at all; a moving joint's goal must be one we sent
        # recently (the register read-back lags our writes by a few ticks).
        other = "something else is commanding motor {}; is the follower still linked to a leader in Station?"
        for m, g in held_goal.items():
            if abs(tel[m]["goal"] - g) > GOAL_MATCH:
                raise MotionError(other.format(m))
        foreign = [m for m in ids if not first and all(abs(tel[m]["goal"] - c[m]) > GOAL_MATCH for c in sent)]
        watch.check("foreign", bool(foreign), now, other.format(foreign[0] if foreign else ""))
        for m, ref in held_pos.items():
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
        sent.append(cmd)
        if s >= 1.0:
            break
        await asyncio.sleep(1 / RATE_HZ)

    # Arrived? Every moved joint close to its goal and standing still. Guards stay on meanwhile.
    deadline, prev = time.monotonic() + SETTLE_S, None
    while True:
        await asyncio.sleep(SETTLE_POLL_S)
        tel = arm.telemetry(max_age_s=STALE_S)
        _check_faults(tel, ids, watch, time.monotonic(), stats)
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
    if tuple(sorted(present)) != EXPECTED_MOTORS:
        raise MotionError(f"expected motors 1-8 on the bus, found {sorted(present)}")
    ranges = arm.ranges()
    for name in {TRANSIT, target} - {None}:
        bus = raw[name].get("bus")
        if bus and bus != arm.bus_serial and not getattr(arm, "is_sim", False):
            raise MotionError(f"view '{name}' was captured on arm {bus}, but this is arm {arm.bus_serial}")
        check_calibration(name, ranges)
    for m in MOVABLE:
        if tel[m]["status"] & ~STATUS_IGNORE:
            raise MotionError(f"motor {m} reports a fault (status 0x{tel[m]['status']:02x})")
        if tel[m].get("read_error"):
            raise MotionError(f"motor {m} is not answering: {tel[m]['read_error']}")
    if [m for m in MOVABLE if not tel[m]["torque"]]:
        raise MotionError("the arm is limp; press 'Hold here' first (with the arm supported)")
    source = await arm.mirroring_source()
    if source:
        raise MotionError(f"Station is mirroring leader {source} onto this arm; "
                          "set the follower to '(Self-controlled)' in Station first")
    env = envelope(ranges)
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
    except (MotionError, RuntimeError) as e:
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
        stats = {"max_lag": {}, "max_current": {}, "max_load": {}}
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
            result["error"] = "stopped"
            if started:
                await _safe_hold(arm)
            raise
        except (MotionError, RuntimeError) as e:    # RuntimeError: stale data, lost bus
            result["error"] = str(e)
            if started:
                await _safe_hold(arm)
        except Exception as e:
            result["error"] = f"unexpected error: {e!r}"
            logger.exception("move to %s failed", name)
            if started:
                await _safe_hold(arm)
        finally:
            result["duration"] = round(result["duration"], 2)
            status.update(moving=False, phase=None, error=result["error"], last_result=dict(result),
                          progress=1.0 if result["ok"] else status["progress"])
            _log_move({"t": t0, "took": round(time.time() - t0, 2), "speed": max_speed, **result, **stats})
    return result
