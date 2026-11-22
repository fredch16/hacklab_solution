"""M2: move the follower arm to a saved view.

    await go_to_view(arm, "top")  ->  {"ok": bool, "view": str, "error": str | None, ...}

Design and rationale: docs/MOTION_PLAN.md; the direct moves and speed-ups: docs/MOTION_OPT.md.

  * Between two checked views a move is one straight line of all seven joints ("direct"), when
    the geometry model shows that line stays clear of the desk and of the arm itself (see
    direct_plan). If only the straight line fails, the same line lifted in the middle ("arc").
  * Every other move runs on a fixed network. Each view has a "spoke" to the hub (the transit
    pose's pitch configuration):  rise (all four pitch joints together, view -> hub)
                                  turn (base yaw + both rolls, at the hub)
                                  descend (all four pitch joints together, hub -> view)
    Moving the pitch joints together keeps the arm between two poses a person taught; moving
    the shoulder on its own does not (from `home` it drives the gripper into the table).
    At fast and turbo the turn overlaps rise and descent above CLEAR_HEIGHT_M ("blended").
  * A move only starts when the arm is somewhere it recognises (at a view, on a spoke, at the
    hub, on a direct line, or stopped mid-turn above the clear height: see locate). Anywhere
    else it refuses, unless the caller explicitly asks to recover to transit.
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
# Refinements of a blended move through the hub; each can be switched off on its own.
M7_FREE = True         # the wrist roll (M7) runs from the first moment to the last instead of with the turn:
M7_FREE_MAX = 300      # ...it only spins the gripper and camera about their own axis. Not if it has further
                       # than this to go (steps; 300 = 26 deg moves the camera, 7 cm off that axis, by 3 cm)
ALIGN = True           # the turn is slowed to fill the time it has (from the rise clearing the height until
                       # the descent comes back down to it) instead of racing there and waiting. Never the
                       # other way round: nothing is slowed that the move is waiting for
HUB_PASS_THROUGH = True   # blended: the descent takes over as soon as the rise is within ARRIVE_TOL of the
                          # hub with its goal read back, without first waiting for it to stand still
BLEND_IDLE_GUARD = True   # blended: joints no segment is driving at the moment keep their goal and place
                          # (another writer / pushed away), as they always did on a move in sequence
FAST_DESCENT = True    # at fast the descent runs at fast too (as it already does in turbo), not at normal
SHORT_HOPS = False     # True: a segment of a few steps takes as long as that hop needs at slow speed,
                       # not MIN_SEGMENT_S (only the minimum duration changes, no guard). Off because
                       # tests/test_server_sim.py counts on a 40-step correction lasting 0.8 s
CLEAR_POINTS = ("j5_forearm_roll", "j6_wrist_pitch", "j7_wrist_roll", "j8_gripper", "jaw_tip", "camera")
ABOVE_SLACK_M = 0.02   # a stopped blended move counts as "above the clear height" down to this much under it
# Direct moves: between two checked poses, at every speed, all seven joints go together along
# one straight line in joint space instead of through the hub, when the geometry model (fk.py)
# shows that line is no closer to the desk or to the arm itself than its two end poses (see
# direct_plan()). A view with "direct": false in views.json never takes part (`home`).
# A line that fails may still pass when it is lifted towards the hub in the middle: an "arc".
DIRECT = True                      # False: every move goes through the hub, as before
ARC = True                          # Try a model-checked lift before the hub route
DIRECT_SPEED_CAP = SPEED_FAST      # a streamed direct move or arc never runs faster than this
DIRECT_TURBO_PROFILE = False        # turbo: a straight direct move that does not end lower than it starts
                                   # runs on the servos' own profile, like a rise (False: streamed at fast)
DIRECT_DIP_M = 0.01                # no part of the arm may go this far below where it is at the ends
DIRECT_SELF_CLEAR_M = 0.05         # the arm must stay this far from itself (fk.self_clearance); `home` does not
DIRECT_LIFTS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.65, 0.8, 1.0)   # how far towards the hub an arc may bulge
DIRECT_LIFT_EXTRA = 0.1            # an arc is lifted this much more than the model says it needs
DIRECT_SAMPLES = 48                # poses checked along a line
DIRECT_TURN_TOL = None             # steps; set to e.g. 60 to allow direct moves only between views that
                                   # share base yaw and forearm roll (then nothing sweeps sideways down low)
PATH_SEGMENTS = ("direct", "arc")  # route segments that move every joint along one checked line
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


# The clock every wait and timeout in this module reads. Always time.monotonic on the real arm;
# tests/virtual_time.py points it at a simulated clock so simulated moves do not take real time.
# (time.time() is only used for timestamps in the move log.)
_now = time.monotonic


def lead_limit(peak_speed: float) -> float:
    """Leash: how far a commanded goal may be ahead of the measured position (140 / 160 / 200 /
    260 steps at 100 / 200 / 400 / 800 steps/s). Bounds how far the arm can still travel if we
    go blind. From the first real session: the loaded shoulder rests 20-35 steps short of its
    goal, needed more than 60 to start lifting out of `top`, and 100 to lift out of `right`."""
    return min(260, 120 + 0.2 * peak_speed)


_move_lock = asyncio.Lock()
# What the current/last move did, for stop_hold(): did it send any goal, which joints was its
# current segment driving, and how did its own abort hold end.
_last = {"wrote": False, "driving": (), "hold": None, "line": ()}   # line: the direct line last driven (a, b)

# Live state of the current/last move, for UIs to poll. Updated in place, never replaced.
status = {"moving": False, "view": None, "phase": None, "phase_index": 0, "phase_count": 0,
          "progress": 0.0, "error": None, "last_result": None, "route": [], "kind": None, "hold": None}


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


def view_direct(name: str) -> bool:
    """False when views.json says "direct": false for this view: every move from or to it goes
    through the hub, whatever the geometry model thinks (`home`: its folded pose risks the arm
    hitting itself, which the model does not check)."""
    return _read_views_file().get(name, {}).get("direct") is not False


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


def locate(present: dict[int, int], views: dict[str, dict[int, int]], links=None, prefer: tuple = ()) -> dict:
    """Classify the measured pose on the route network.

    {"where": "view", "view": name}    at a saved view
    {"where": "hub"}                   pitch joints at the transit configuration (any yaw/roll)
    {"where": "spoke", "view": name}   between a view and the hub: yaw/rolls are the view's and
                                       the pitch joints are all the same fraction of the way
                                       (the wrist roll M7 may be up to M7_FREE_MAX away: a blended
                                       move turns it all the way along, and it sweeps nothing)
    {"where": "above", "view": name}   pitch joints on that line too, base yaw / forearm roll
                                       anywhere, and the geometry model puts the arm above the
                                       clear height: where a Stop leaves a blended move while its
                                       turn overlaps the rise or the descent. Only with `links`.
    {"where": "between", "view": a, "to": b, "s": 0..1}
                                       on the direct line (or arc) from a to b, a fraction s of the
                                       way: where a Stop during a direct move leaves the arm. Only
                                       with `links` (the lines direct moves may use, see _Links).
                                       Close to a view several lines run side by side; of those
                                       within tolerance the one naming most of `prefer` (the
                                       target, the line last driven) wins, then the closest.
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

    def off_spoke(pose):    # how far the pitch joints are from the straight line view (f=0) -> hub (f=1)
        span = sum((hub[m] - pose[m]) ** 2 for m in PITCH)
        if span <= AT_TOL ** 2:
            return None
        f = sum((present[m] - pose[m]) * (hub[m] - pose[m]) for m in PITCH) / span
        f = min(max(f, 0.0), 1.0)
        return max(abs(present[m] - (pose[m] + (hub[m] - pose[m]) * f)) for m in PITCH)

    best = None
    roll_tol = M7_FREE_MAX + AT_TOL if (M7_FREE and BLEND) else AT_TOL
    for name, pose in views.items():
        if name == TRANSIT or not _near(present, pose, (1, 5)) or abs(present[7] - pose[7]) > roll_tol:
            continue
        miss = off_spoke(pose)
        if miss is not None and miss <= AT_TOL and (best is None or miss < best[0]):
            best = (miss, {"where": "spoke", "view": name})
    if best is None and links is not None:
        for a, b, plan in links.pairs():
            miss, s = _on_path(present, views[a], views[b], hub, plan["lift"])
            rank = (-sum(n in (a, b) for n in set(prefer)), miss)
            if miss <= AT_TOL and (best is None or rank < best[0]):
                best = (rank, {"where": "between", "view": a, "to": b, "s": round(s, 3)})
    if best is None and links is not None and BLEND and links.height(present) >= CLEAR_HEIGHT_M - ABOVE_SLACK_M:
        for name, pose in views.items():
            miss = None if name == TRANSIT else off_spoke(pose)
            if miss is not None and miss <= AT_TOL and (best is None or miss < best[0]):
                best = (miss, {"where": "above", "view": name})
    return best[1] if best else {"where": "unknown"}


# ---------------------------------------------------------------- direct lines between poses

def _path_pose(a: dict, b: dict, hub: dict, lift: float, s: float) -> dict[int, float]:
    """Pose a fraction `s` of the way along the direct line from pose `a` to pose `b`: every joint
    the same fraction of its way. With `lift` > 0 the line becomes an arc: the pitch joints bulge
    towards the hub's pitch configuration, by `lift` of the remaining distance at half-way and
    not at all at either end. The same poses in reverse order give the path from b to a."""
    out = {}
    for m in MOVABLE:
        x = a[m] + (b[m] - a[m]) * s
        if lift and m in PITCH:
            x += lift * 4.0 * s * (1.0 - s) * (hub[m] - x)
        out[m] = x
    return out


def _on_path(present: dict, a: dict, b: dict, hub: dict, lift: float) -> tuple[float, float]:
    """(how far the worst joint is from the path a -> b, at which fraction of it), closest point."""
    def miss(s):
        q = _path_pose(a, b, hub, lift, s)
        return max(abs(present[m] - q[m]) for m in MOVABLE)
    best = min((miss(i / 32), i / 32) for i in range(33))
    for step in (1 / 64, 1 / 128, 1 / 256, 1 / 512):
        for s in (best[1] - step, best[1] + step):
            if 0.0 <= s <= 1.0:
                best = min(best, (miss(s), s))
    return best


_direct_cache: dict = {}


def _line_problem(a: dict, b: dict, hub: dict, lift: float, ranges, grip: int) -> str | None:
    """Why the geometry model does not vouch for this line or arc, or None. See direct_plan()."""
    import fk
    n = DIRECT_SAMPLES
    poses = [{**_path_pose(a, b, hub, lift, i / n), GRIPPER: grip} for i in range(n + 1)]
    worst = None                                   # (how far below its end poses, part, how low)
    for mapping in ("viewer", "true_scale"):
        gaps = [fk.self_clearance(p, ranges, mapping) for p in poses]
        if min(gaps[0], gaps[-1]) < DIRECT_SELF_CLEAR_M:
            return (f"an end pose folds the arm to within {min(gaps[0], gaps[-1]) * 100:.0f} cm of itself, "
                    "and the model cannot check the arm against itself")
        if min(gaps) < min(gaps[0], gaps[-1]) - DIRECT_DIP_M or min(gaps) < DIRECT_SELF_CLEAR_M:
            return f"the arm would come within {min(gaps) * 100:.0f} cm of itself on the way"
        heights = [fk.part_heights(p, ranges, mapping) for p in poses]
        for part in heights[0]:
            ends = min(heights[0][part], heights[-1][part])
            low = min(h[part] for h in heights)
            # Close to the desk the allowance shrinks: a part resting 1 cm up may dip 5 mm, not 1 cm.
            if low < ends - min(DIRECT_DIP_M, max(ends, 0.0) / 2) and (worst is None or ends - low > worst[0]):
                worst = (ends - low, part, low)
    if worst:
        return (f"{worst[1].replace('mesh:', '')} would dip to {worst[2] * 100:.1f} cm, "
                f"{worst[0] * 100:.1f} cm below where it is at the end poses")
    return None


def direct_plan(a: dict[int, int], b: dict[int, int], hub: dict[int, int], ranges, grip: int = 1279) -> dict:
    """May the arm go from pose `a` to pose `b` (both clamped, taught poses) in one move of all
    seven joints, and along which path? {"ok": bool, "lift": float | None, "reason": str}.

    The rule. The path is the straight line in joint space (lift 0), or failing that the same
    line bulging towards the hub's pitch configuration in the middle (an arc, the smallest
    DIRECT_LIFTS value that passes plus DIRECT_LIFT_EXTRA). A path passes when the geometry model
    fk.py, at DIRECT_SAMPLES poses along it and in BOTH of its steps-to-angle mappings, shows:
      1. Desk: no part of the arm (every key point, and the lowest vertex of every link mesh)
         gets more than DIRECT_DIP_M (1 cm) lower than that same part is at the lower of the two
         end poses; for a part that ends within 2 cm of the desk the allowance is half its height.
         Comparing each part with itself at two poses a person taught cancels most of what the
         model gets wrong; there is no absolute floor because taught views sit as low as 1 cm.
      2. Itself: the arm never comes closer to itself (fk.self_clearance: centre lines of links
         three or more joints apart) than at its end poses less 1 cm, and never closer than
         DIRECT_SELF_CLEAR_M. The model has no self-collision meshes, so a folded end pose
         (`home`, 3 cm) rules a pair out altogether: those moves keep going through the hub.
    Not in the rule, on purpose: base yaw and forearm roll turn all the way along a direct move,
    also below CLEAR_HEIGHT_M. The desk is taken to be clear for every move between checked views
    (DIRECT_TURN_TOL restricts direct moves to views with the same heading). The model knows the
    desk plane and nothing on it; it is advisory and was never measured against the real arm.
    Who may use a line at all (both views checked, neither opted out) is decided in _Links.

    History: review round 1 removed an earlier shortcut (any pair whose yaw and rolls matched went
    direct, unchecked, which allowed the 130 degree sweep home -> top). The reviewers' models
    showed that lifting the shoulder alone out of `home` hits the desk and that left <-> right,
    right <-> side_low, close <-> side_low and close <-> right go below it on a straight line;
    rule 1 rejects all of those as straight lines, rule 2 keeps `home` out."""
    turn_gap = max(abs(a[m] - b[m]) for m in (1, 5))
    key = (tuple(sorted((tuple(a[m] for m in MOVABLE), tuple(b[m] for m in MOVABLE)))),
           tuple(hub[m] for m in PITCH), tuple(sorted((m, tuple(r)) for m, r in ranges.items())), grip,
           DIRECT_DIP_M, DIRECT_SELF_CLEAR_M, DIRECT_LIFTS, DIRECT_LIFT_EXTRA, DIRECT_SAMPLES, DIRECT_TURN_TOL)
    if key in _direct_cache:
        return _direct_cache[key]
    if DIRECT_TURN_TOL is not None and turn_gap > DIRECT_TURN_TOL:
        plan = {"ok": False, "lift": None, "reason": f"base yaw or forearm roll differ by {turn_gap} steps"}
    else:
        plan, reason = None, ""
        for lift in DIRECT_LIFTS:
            problem = _line_problem(a, b, hub, lift, ranges, grip)
            if problem is None and lift > 0:        # an arc gets a little more lift than it needs
                more = min(1.0, lift + DIRECT_LIFT_EXTRA)
                if _line_problem(a, b, hub, more, ranges, grip) is None:
                    lift = more
            if problem is None:
                plan = {"ok": True, "lift": lift,
                        "reason": "straight line" if lift == 0 else f"arc lifted {lift:.0%} towards the hub: {reason}"}
                break
            reason = reason or problem              # why the straight line was refused
            if "folds the arm" in problem:
                break                               # no lift changes an end pose
        plan = plan or {"ok": False, "lift": None, "reason": reason}
    if len(_direct_cache) > 400:
        _direct_cache.clear()
    _direct_cache[key] = plan
    return plan


class _Links:
    """The direct lines the arm may use right now: between two trusted poses that are both marked
    checked (the transit pose counts as checked) and pass direct_plan(). Worked out on demand."""

    def __init__(self, views: dict, ranges, grip: int):
        self.views, self.ranges = views, ranges
        self.grip = int(round(grip / 50.0)) * 50     # the limp gripper wobbles; do not re-plan for a step
        self._flags: dict[str, tuple[bool, bool]] = {}

    def height(self, present: dict) -> float:
        """Clear-point height of a measured pose by the geometry model (see _height)."""
        return _height({**{m: present[m] for m in MOVABLE}, GRIPPER: self.grip}, self.ranges)

    def _flag(self, name: str) -> tuple[bool, bool]:    # (checked, may go direct), read once
        if name not in self._flags:
            self._flags[name] = (name == TRANSIT or view_checked(name), view_direct(name))
        return self._flags[name]

    def plan(self, a: str, b: str) -> dict:
        """{"ok", "lift", "reason"} for the pair, in either order."""
        no = {"ok": False, "lift": None}
        if not DIRECT:
            return {**no, "reason": "direct moves are switched off (motion.DIRECT)"}
        if a == b or a not in self.views or b not in self.views:
            return {**no, "reason": "not two known poses"}
        for name in (a, b):
            checked, direct = self._flag(name)
            if not checked:
                return {**no, "reason": f"'{name}' is not marked checked"}
            if not direct:
                return {**no, "reason": f"'{name}' is set to always go through the transit pose"}
        plan = direct_plan(self.views[a], self.views[b], self.views[TRANSIT], self.ranges, self.grip)
        if plan["ok"] and plan["lift"] and not ARC:
            return {**no, "reason": plan["reason"].split(": ", 1)[-1]}
        return plan

    def pairs(self):
        if not DIRECT:
            return
        names = sorted(self.views)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                plan = self.plan(a, b)
                if plan["ok"]:
                    yield a, b, plan


# ---------------------------------------------------------------- planning

def _pick(pose, ids):
    return {m: pose[m] for m in ids}


def route_kind(start_view: str, target_view: str) -> tuple[str, str]:
    """("direct" | "arc" | "hub", why) for a move from one saved view to another, from views.json
    alone: no arm needed, nothing is read from Station. For UIs. It uses the calibration the views
    were captured with and a closed gripper; a move on the arm decides again with live values
    (describe() / go_to_view() report that as `kind`), and anything that starts elsewhere than
    exactly at `start_view` (on a spoke, after a Stop) is planned from where the arm is."""
    raw = _read_views_file()
    for name in (start_view, target_view, TRANSIT):
        if name not in raw:
            return "hub", f"no saved pose '{name}'"
    if start_view == target_view:
        return "hub", "already there: only a small correction along the view's own path"
    try:
        import fk  # noqa: F401
        ranges = {int(m): tuple(r) for m, r in raw[TRANSIT]["ranges"].items()}
        env = envelope(ranges)
        views = {n: clamp_pose({int(k): v for k, v in raw[n]["joints"].items()}, env)
                 for n in (start_view, target_view, TRANSIT)}
    except Exception as e:      # no geometry model, no calibration record, bad calibration
        return "hub", f"cannot check a direct line ({e})"
    plan = _Links(views, ranges, 1279).plan(start_view, target_view)
    if not plan["ok"]:
        return "hub", plan["reason"]
    return ("arc" if plan["lift"] else "direct"), plan["reason"]


class _PathGoal(dict):
    """The goal of a "direct" / "arc" segment: {motor: goal} for all seven joints, plus the line it
    runs along: path = (pose a, pose b, hub, lift, from fraction, to fraction), see _path_pose(),
    and the names of its two ends."""
    path: tuple = ()
    ends: tuple = ()


def _path_segment(views: dict, a: str, b: str, lift: float, s0: float, s1: float) -> tuple[str, dict]:
    goal = _PathGoal({m: int(round(x)) for m, x in _path_pose(views[a], views[b], views[TRANSIT], lift, s1).items()})
    goal.path = (views[a], views[b], views[TRANSIT], lift, s0, s1)
    goal.ends = (a, b)
    return ("arc" if lift else "direct", goal)


def plan_route(present: dict[int, int], target: str, views: dict[str, dict[int, int]],
               allow_unknown_start: bool = False, links=None, direct: bool = False) -> list[tuple[str, dict[int, int]]]:
    """Ordered segments [(name, {motor: goal})] from the measured pose to `target`.

    Through the hub (always, unless `direct`): rise, turn, descend. Each of those moves one joint
    group; everything else holds still. With `direct` and `links` (see _Links), a move that starts
    at a pose with a direct line to the target is a single "direct" segment (or "arc", a lifted
    line) that moves all seven joints together; go_to_view() and describe() plan that way. A pose part-way along such a line (after a Stop)
    always continues along it, whatever `direct` says: to either end, or to the nearer end and on
    from there.
    `views` must be clamped, trusted and include the transit pose. Raises MotionError when the
    arm is not on the network. Segments that turn out to need no motion are skipped when they
    run, so a route always states the full sequence: being "at the hub" within tolerance still
    gets an exact rise.
    """
    hub = views[TRANSIT]
    loc = locate(present, views, links, prefer=(target,) + tuple(_last["line"]))
    if loc["where"] == "unknown" and not allow_unknown_start:
        raise MotionError(
            "the arm is not at a known pose, so there is no checked path from here. "
            "Move it by hand to the transit pose (Go limp, pose it, Hold here)")
    if loc["where"] == "between":
        a, b, s = loc["view"], loc["to"], loc["s"]
        lift = links.plan(a, b)["lift"]
        if target in (a, b):
            return [_path_segment(views, a, b, lift, s, 1.0 if target == b else 0.0)]
        near = a if s < 0.5 else b                  # finish the line at its nearer end, then go on from there
        first = _path_segment(views, a, b, lift, s, 0.0 if near == a else 1.0)
        return [first] + plan_route({**present, **views[near]}, target, views, links=links, direct=direct)
    if direct and links is not None:
        # At a view, or at the transit pose itself (the hub with transit's own yaw and rolls).
        at = loc.get("view") if loc["where"] == "view" else (
            TRANSIT if loc["where"] == "hub" and _near(present, hub, MOVABLE) else None)
        if at and at != target:
            plan = links.plan(at, target)
            if plan["ok"]:
                return [_path_segment(views, at, target, plan["lift"], 0.0, 1.0)]
    if loc["where"] in ("view", "spoke") and loc["view"] == target and target != TRANSIT:
        # At the target or on its own spoke: straight down the spoke onto the exact pose. If a
        # stopped blended move left the wrist roll somewhere else, that is put right first.
        down = [("descend", _pick(views[target], PITCH))]
        if abs(present[7] - views[target][7]) > AT_TOL:
            return [("turn", {7: views[target][7]})] + down
        return down
    rise = ("rise", _pick(hub, PITCH))
    if target == TRANSIT:
        return [rise, ("turn", _pick(hub, TURN))]
    return [rise, ("turn", _pick(views[target], TURN)), ("descend", _pick(views[target], PITCH))]


ALONG_MAX = 0.35   # a stop part-way along a line (go_to_view along=) is at most this far from its view

# Computed paths (go_through): poses a planner worked out, not taught views.
PATH_FLOOR_M = 0.04       # no part of the arm below this in the geometry model (or no lower than it is now)
PATH_STEP_MAX = 400       # steps any joint may change between two consecutive poses of a path
PATH_CHECKS = 8           # poses checked between two consecutive waypoints
# Station sets every ST3215 to trip its own overload protection when its load stays above 25 % for 2 s
# (norma-core presets.rs: DEFAULT_OVERLOAD_TORQUE = 25; protection time 2 s). On the ElRobot the shoulder
# reached that during computed moves. During computed paths we stop well before: load above this, this long.
PATH_TRIP_GUARD = {2: (550, 1.2)}   # motor: (load 0-1000, seconds); below the servo overload threshold, which
                                    # servo_limits.py sets to 60 % (Station: 25 %); 55 % sustained 1.2 s, 0.8 s before the servo


class LoadGuardError(MotionError):
    """A computed path stopped because a joint's load stayed near the servo's own overload threshold."""


_trip_guard: dict = {}             # active during guarded moves (one move at a time: _move_lock)


def plan_along(present: dict[int, int], view: str, toward: str, s: float, views: dict, links) -> list:
    """The one segment from the measured pose to the pose a fraction `s` of the way along the
    checked direct line (or arc) from `view` to `toward`: a point every move view -> toward already
    sweeps. The arm must be at `view`, at `toward`, or already on that line. Raises MotionError otherwise."""
    if not 0.0 < s <= ALONG_MAX:
        raise MotionError(f"a stop along a line must be between 0 and {ALONG_MAX} of the way")
    if links is None:
        raise MotionError("the geometry model is unavailable, so no line can be checked")
    plan = links.plan(view, toward)
    if not plan["ok"]:
        raise MotionError(f"no checked direct line from '{view}' to '{toward}': {plan['reason']}")
    loc = locate(present, views, links, prefer=(view, toward))
    if loc["where"] == "view" and loc["view"] == view:
        s0 = 0.0
    elif loc["where"] == "view" and loc["view"] == toward:    # entering the same line from its other end
        s0 = 1.0
    elif loc["where"] == "between" and {loc["view"], loc["to"]} == {view, toward}:
        s0 = loc["s"] if loc["view"] == view else 1.0 - loc["s"]
    else:
        raise MotionError(f"the arm must be at '{view}' or on its line to '{toward}' first (it is {loc})")
    return [_path_segment(views, view, toward, plan["lift"], s0, s)]


def path_curve(path: list[dict[int, int]]):
    """(f(k) -> pose for k in 0..1, total joint distance): a monotone cubic (Fritsch-Carlson / PCHIP) through
    every waypoint, per joint: velocity is continuous through each waypoint, and between two waypoints a joint
    never goes beyond either of them, so the curve cannot overshoot a limit. k is spread by joint distance."""
    ids = list(MOVABLE)
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + max(max(abs(b[m] - a[m]) for m in ids), 1e-9))
    n = len(path)
    tangents = {}
    for m in ids:
        h = [cum[k + 1] - cum[k] for k in range(n - 1)]
        d = [(path[k + 1][m] - path[k][m]) / h[k] for k in range(n - 1)]
        t = [0.0] * n
        t[0], t[-1] = (d[0] if n > 1 else 0.0), (d[-1] if n > 1 else 0.0)
        for k in range(1, n - 1):
            if d[k - 1] * d[k] <= 0:
                t[k] = 0.0                      # a turning point: stop there, never swing past it
            else:
                w1, w2 = 2 * h[k] + h[k - 1], h[k] + 2 * h[k - 1]
                t[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k])
        tangents[m] = t
    total = cum[-1] or 1.0

    def f(k: float) -> dict[int, float]:
        if n == 1:
            return {m: float(path[0][m]) for m in ids}
        x = min(max(k, 0.0), 1.0) * total
        i = max(0, min(n - 2, next((j for j in range(n - 1) if cum[j + 1] >= x), n - 2)))
        h = cum[i + 1] - cum[i]
        u = (x - cum[i]) / h
        h10, h01, h11 = u ** 3 - 2 * u ** 2 + u, u ** 2 * (3 - 2 * u), u ** 3 - u ** 2
        # Use offsets from the first waypoint so a stationary joint stays exactly at its limit.
        return {m: path[i][m] + (h01 * (path[i + 1][m] - path[i][m]) + h10 * h * tangents[m][i]
                                 + h11 * h * tangents[m][i + 1])
                for m in ids}
    return f, cum[-1]


def path_problem(path: list[dict[int, int]], ranges, env, grip: int, absolute: bool = False,
                 floor: float | None = None) -> str | None:
    """Why the geometry model does not vouch for a computed path, or None. Every waypoint inside the
    envelope; neighbouring waypoints at most PATH_STEP_MAX apart; along the smooth curve the arm will follow
    (path_curve), at PATH_CHECKS poses per waypoint pair and in both of fk's mappings: inside the envelope, no part of the arm below PATH_FLOOR_M (or below where it is at the start)
    and the arm never closer to itself than DIRECT_SELF_CLEAR_M. Advisory like every fk check: it knows
    the desk plane, not what lies on it, and not the camera stalk or cables.
    `absolute`: no allowance for a low first pose (for planned poses the arm is not standing at yet).
    `floor`: a lower desk floor than PATH_FLOOR_M, for the pick job's descent to the object only."""
    import fk
    for i, q in enumerate(path):
        for m in MOVABLE:
            lo, hi = env[m]
            if not lo <= q[m] <= hi:
                return f"waypoint {i}: motor {m} at {q[m]} is outside its envelope {lo}..{hi}"
    for i, (a, b) in enumerate(zip(path, path[1:])):
        jump = max(abs(b[m] - a[m]) for m in MOVABLE)
        if jump > PATH_STEP_MAX:
            return f"waypoints {i}-{i + 1}: a joint would jump {jump} steps"
    start = {**path[0], GRIPPER: grip}
    base = PATH_FLOOR_M if floor is None else floor
    floors = {mp: base if absolute else min(base, fk.min_height(start, ranges, mapping=mp)[0] - 0.005)
              for mp in ("viewer", "true_scale")}
    curve, _ = path_curve(path)
    n = PATH_CHECKS * (len(path) - 1)
    for j in range(n + 1):                      # the curve the arm will follow, not just its waypoints
        i = min(j // PATH_CHECKS, len(path) - 2)
        q = {**{m: v for m, v in curve(j / n).items()}, GRIPPER: grip}
        for m in MOVABLE:
            if not env[m][0] <= q[m] <= env[m][1]:
                return f"between waypoints {i} and {i + 1}: motor {m} would leave its envelope"
        for mp in ("viewer", "true_scale"):
            h, part = fk.min_height(q, ranges, mapping=mp)
            if h < floors[mp]:
                return f"between waypoints {i} and {i + 1}: {part} would be {h * 100:.1f} cm above the desk"
            if fk.self_clearance(q, ranges, mp) < DIRECT_SELF_CLEAR_M:
                return f"between waypoints {i} and {i + 1}: the arm would come too close to itself"
    return None


class _PolyGoal(dict):
    """Goal of a computed path segment: the final pose, plus `poly`, the waypoints to follow."""
    poly: list = []


async def go_through(arm, poses: list[dict[int, int]], max_speed: float = SPEED_SLOW, label: str = "path",
                     load_guard: bool = True, floor: float | None = None) -> dict:
    """Follow computed poses in one continuous guarded move (the camera director's arcs). The path
    starts where the arm is; every pose is checked by path_problem() before anything is written.
    Same result keys and the same guards, Stop and holds as go_to_view."""
    refused = {"ok": False, "view": label, "duration": 0.0, "route": [], "kind": None, "hold": None}
    if not poses:
        return {**refused, "error": "empty path"}
    if _move_lock.locked():
        return {**refused, "error": "already moving"}
    lock_file = None
    if not getattr(arm, "is_sim", False):
        lock_file = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return {**refused, "error": "another program is moving the arm"}
    try:
        async with _move_lock:
            t0 = time.time()
            stats = {"max_lag": {}, "max_current": {}, "max_load": {}}
            result = {**refused, "error": None, "route": ["path"], "kind": "path"}
            status.update(moving=True, view=label, phase="check", phase_index=0, phase_count=1, progress=0.0,
                          error=None, route=["path"], kind="path", hold=None)
            ctx = {"wrote": False, "moving": ()}
            _last.update(wrote=False, driving=(), hold=None, line=())
            try:
                info = await _inspect(arm, None)
                blockers = [b for b in info["blockers"] if "known pose" not in b]
                if blockers:
                    raise MotionError(blockers[0])
                present, env = info["present"], info["env"]
                path = [{m: present[m] for m in MOVABLE}] + [{m: int(q[m]) for m in MOVABLE} for q in poses]
                problem = path_problem(path, arm.ranges(), env, present[GRIPPER], floor=floor)
                if problem:
                    raise MotionError(f"the computed path is not safe by the model: {problem}")
                await _fresh_again(arm)
                goal = _PolyGoal(path[-1])
                goal.poly = path
                status.update(phase="path", phase_index=1)
                ctx["moving"] = _last["driving"] = MOVABLE
                if load_guard:     # not for retracing a path just driven: that leads back to lighter poses
                    _trip_guard.update(PATH_TRIP_GUARD)
                try:
                    result["duration"] += await _run_segment(arm, "path", goal, max_speed, env, stats, ctx, {})
                finally:
                    _trip_guard.clear()
                ctx["moving"] = _last["driving"] = ()
                result["ok"] = True
            except asyncio.CancelledError:
                result["error"] = "stopped"
                if ctx["wrote"]:
                    result["hold"] = _last["hold"] = await _abort_hold(arm, RELAX_TOL, ctx["moving"])
                raise
            except BlockedError as e:
                result["error"] = str(e)
                if ctx["wrote"]:
                    result["hold"] = _last["hold"] = await _abort_hold(arm, RELAX_TOL, ctx["moving"])
            except (MotionError, RuntimeError) as e:
                result["error"] = str(e)
                result["load_guard"] = isinstance(e, LoadGuardError)
                if ctx["wrote"]:
                    result["hold"] = _last["hold"] = await _abort_hold(arm, FREEZE_TOL, ctx["moving"])
            finally:
                if result["hold"] == "unconfirmed":
                    result["error"] = f"{result['error']}; HOLD NOT CONFIRMED"
                result["duration"] = round(result["duration"], 2)
                status.update(moving=False, phase=None, error=result["error"], hold=result["hold"],
                              last_result=dict(result), progress=1.0 if result["ok"] else status["progress"])
                if not getattr(arm, "is_sim", False):
                    _log_move({"t": t0, "took": round(time.time() - t0, 2), "speed": max_speed, **result, **stats})
            return result
    finally:
        if lock_file:
            lock_file.close()


# ---------------------------------------------------------------- talking to the arm

def _ease(s: float) -> float:
    return s * s * (3 - 2 * s)


def _ease_inv(f: float) -> float:
    """The s with _ease(s) = f."""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _ease(mid) < f else (lo, mid)
    return (lo + hi) / 2


def _duration(dist: float, peak_speed: float) -> float:
    """Planned time of a streamed segment whose furthest joint travels `dist` steps: smoothstep
    peaks at 1.5x its average speed. Never shorter than MIN_SEGMENT_S, except (SHORT_HOPS) for a
    hop so small that even at slow speed it would take less: then that time, at least 0.2 s."""
    floor = min(MIN_SEGMENT_S, max(0.2, 1.5 * dist / SPEED_SLOW)) if SHORT_HOPS else MIN_SEGMENT_S
    return max(floor, 1.5 * dist / peak_speed)


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
    deadline = _now() + CONFIRM_S
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
        if _now() > deadline:
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
    for m, (limit, hold_s) in _trip_guard.items():
        if tel[m]["load"] > limit:
            if now - watch.since.setdefault(("trip", m), now) > hold_s:
                raise LoadGuardError(f"motor {m} load stayed above {limit / 10:.0f}% for {hold_s} s: stopped before "
                                     "its overload protection trips (the shoulder is carrying too much here)")
        else:
            watch.since.pop(("trip", m), None)
    for m, g in held_goal.items():      # a joint we are not moving must keep the goal it had
        if abs(tel[m]["goal"] - g) > GOAL_MATCH:
            raise MotionError(OTHER_WRITER.format(m))
    for m, ref in held_pos.items():
        if abs(tel[m]["pos"] - ref) > drift_limit:
            raise BlockedError(f"motor {m} was pushed off its position while holding still")


async def _run_segment(arm, name: str, goal: dict[int, int], peak_speed: float,
                       env: dict, stats: dict, ctx: dict, planned: dict, turbo: bool = False,
                       others: tuple = (), gate=None, need_still: bool = True) -> float:
    """Move the joints in `goal` to their targets; every other joint must stay put.
    `planned` gives, for joints an earlier segment moved, where they are supposed to be.

    Normally the path is streamed in small waypoints on a leash. With `turbo` the final goal
    is written once and each servo runs its own speed/acceleration profile: much faster, but
    the goal is far ahead of the arm, so what protects it is the progress watchdog, the other
    guards and a hard-braking Stop, not the leash.

    `others`: joints another segment is driving at the same time (blended moves); this segment
    does not expect them to hold still. `gate(waypoint)`: a streamed segment only advances to a
    waypoint the gate allows (the descent waits above the clear height until the turn is done).
    `need_still=False`: for a segment whose joints the next segment drives straight on (the rise
    before a blended descent): arrival is the goal read back and every joint within ARRIVE_TOL
    on a fresh sample, without waiting for the joints to come to rest."""
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

    def leaves(t):   # a computed path that comes back to where it started (a wave) still has to be driven
        poly = getattr(goal, "poly", None)
        return bool(poly) and any(abs(p[m] - t[m]["pos"]) > SKIP_TOL for p in poly for m in ids)

    if resting_at_goal(tel) and not leaves(tel):
        # Nothing to do, but only if the joints are really resting there: guard both looks,
        # and the second must be a newer read that has not moved.
        _guard(tel, watch, _now(), stats, held_pos, held_goal)
        await asyncio.sleep(SETTLE_POLL_S)
        again = arm.telemetry(max_age_s=STALE_S)
        _guard(again, watch, _now(), stats, held_pos, held_goal)
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
    path = getattr(goal, "path", None)
    poly = getattr(goal, "poly", None)
    if poly:
        # A computed path (go_through): the smooth curve through checked poses (path_curve, the same curve
        # path_problem checked), one eased move from end to end. The start offset fades out along the way.
        off = {m: start[m] - poly[0][m] for m in ids}
        curve, _ = path_curve(poly)

        def shape(k):
            q = curve(k)
            return {m: q[m] + (1.0 - k) * off[m] for m in ids}
        dist = max(sum(abs(b[m] - a[m]) for a, b in zip(poly, poly[1:])) for m in ids)
    elif path:
        # A direct line or arc: every joint follows _path_pose(). The arm starts a little off that
        # line (sag, or wherever a Stop left it); the difference fades out along the way.
        pa, pb, phub, lift, s0, s1 = path
        off = {m: start[m] - x for m, x in _path_pose(pa, pb, phub, lift, s0).items()}

        def shape(k):
            q = _path_pose(pa, pb, phub, lift, s0 + (s1 - s0) * k)
            return {m: q[m] + (1.0 - k) * off[m] for m in ids}
        pts = [shape(i / 16) for i in range(17)]              # the arc makes some joints travel further
        dist = max(sum(abs(q[m] - p[m]) for p, q in zip(pts, pts[1:])) for m in ids)
    else:
        def shape(k):
            return {m: start[m] + (goal[m] - start[m]) * k for m in ids}
        dist = max(abs(goal[m] - start[m]) for m in ids)
    duration = _duration(dist, peak_speed)
    servo_speed = int(peak_speed * 1.5) + 50                 # the servo's own ceiling, just above ours
    servo_accel = max(SERVO_ACCEL, int(peak_speed / 20))     # enough acceleration to keep up when fast
    leash = lead_limit(peak_speed)

    def waypoint(s):   # inside the calibrated envelope even if the arm started just outside it
        q = shape(_ease(s))
        return {m: min(max(int(round(q[m])), env[m][0]), env[m][1]) for m in ids}

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
        t0 = best_t = _now()
        sent.append((t0, final))
        best = far
        while True:
            await asyncio.sleep(1 / RATE_HZ)
            now = _now()
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
        cmd, duration = final, _now() - t0
    s, last, stalled_since, first = (1.0 if turbo else 0.0), _now(), None, True
    cmd = cmd if turbo else dict(start)
    while not turbo:
        now = _now()
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
            sent.append((_now(), cmd))
        if s >= 1.0:
            break
        await asyncio.sleep(1 / RATE_HZ)

    # Arrived? On two different fresh samples: the final goal read back, every moved joint close
    # to it and standing still. All guards stay on until then.
    final = cmd
    deadline, prev = _now() + SETTLE_S, None
    while True:
        await asyncio.sleep(SETTLE_POLL_S if need_still else 1 / RATE_HZ)
        now = _now()
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
        if echoed and close and (still or not need_still):
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


def _ends_lower(present: dict, goal: dict, ranges) -> bool:
    """Does a move from the measured pose to `goal` bring the far end of the arm (forearm, wrist,
    gripper, camera: CLEAR_POINTS) closer to the desk? By the geometry model, 1 cm allowance."""
    return _height({**present, **goal}, ranges) < _height(present, ranges) - DIRECT_DIP_M


def _kind(route: list[str]) -> str | None:
    """What kind of route this is, for UIs: "direct" (straight line, every joint together), "arc"
    (the same, lifted towards the hub in the middle) or "hub" (rise, turn, descend)."""
    if not route:
        return None
    if any(seg not in PATH_SEGMENTS for seg in route):
        return "hub"
    return "arc" if "arc" in route else "direct"


def _segment_speed(seg: str, max_speed: float) -> tuple[float, bool]:
    """(speed, turbo) for one segment. Turbo: rise and turn on the servos' own profile, the
    descent streamed at fast: coming down onto a view is where an overshoot would matter, so it
    stays on the leash. (With FAST_DESCENT off the descent at fast is capped at normal.)"""
    if seg in PATH_SEGMENTS:
        # Streamed on the leash, which keeps the arm on the line the model checked. (In turbo a
        # straight line that does not end lower than it starts is upgraded, see _go_to_view.)
        return min(max_speed, DIRECT_SPEED_CAP), False
    if seg != "descend":
        return max_speed, max_speed >= SPEED_TURBO
    cap = SPEED_FAST if (FAST_DESCENT or max_speed >= SPEED_TURBO) else SPEED_NORMAL
    return min(max_speed, cap), False


def _blend_plan(segs: dict, present: dict, max_speed: float, ranges, grip: int) -> dict:
    """{segment: (speed, turbo)} for a blended move, and `segs` with the wrist roll split off.

    M7_FREE: M7 leaves the turn and becomes its own segment "roll", spread over the whole move.
    ALIGN: the turn (M1, M5) may not start before the rise has cleared CLEAR_HEIGHT_M and has to be
    done before the descent comes back down to it. If it needs less time than that window, its
    speed is lowered so it arrives as the window closes. Times are the planned ones; the real arm
    lags them a little, so the turn ends slightly early rather than late."""
    plan = {name: _segment_speed(name, max_speed) for name in segs}
    turn = segs.get("turn")
    if not turn:
        return plan

    def planned(name):          # (planned time, furthest joint's travel) of a pitch segment
        if name not in segs:
            return 0.0, 0.0
        start = present if name == "rise" else {**present, **segs.get("rise", {})}
        dist = max(abs(g - start[m]) for m, g in segs[name].items())
        speed, turbo = plan[name]
        return (dist / speed + speed / (TURBO_ACCEL * 100.0) if turbo else _duration(dist, speed)), dist

    (t_rise, _), (t_desc, _) = planned("rise"), planned("descend")
    if M7_FREE and 7 in turn and len(turn) > 1 and abs(turn[7] - present[7]) <= M7_FREE_MAX:
        segs["roll"] = {7: turn.pop(7)}
        whole = max(t_rise + t_desc, MIN_SEGMENT_S)
        slow = 1.5 * abs(segs["roll"][7] - present[7]) / (0.9 * whole)
        plan["roll"] = (min(max(slow, 30.0), min(max_speed, SPEED_FAST)), False)
    if not ALIGN:
        return plan

    def clear_fraction(start: dict, end: dict, turned: dict, rising: bool) -> float:
        """How far along the pitch line start -> end the arm is above the clear height: from this
        fraction on when rising, up to this fraction when descending."""
        ok = [_height({**turned, **{m: start[m] + (end[m] - start[m]) * k / 20 for m in end}, GRIPPER: grip},
                      ranges) >= CLEAR_HEIGHT_M for k in range(21)]
        if rising:
            return next((k / 20 for k in range(21) if all(ok[k:])), 1.0)
        return next((k / 20 for k in range(20, -1, -1) if all(ok[:k + 1])), 0.0)

    window = 0.0
    if "rise" in segs:
        window += t_rise * (1.0 - _ease_inv(clear_fraction(present, segs["rise"], present, True)))
    if "descend" in segs:
        top = {**present, **segs.get("rise", {})}
        window += t_desc * _ease_inv(clear_fraction(top, segs["descend"], {**present, **turn, **segs.get("roll", {})}, False))
    dist = max(abs(g - present[m]) for m, g in turn.items())
    speed, turbo = plan["turn"]
    natural = dist / speed + speed / (TURBO_ACCEL * 100.0) if turbo else _duration(dist, speed)
    window = 0.85 * window - 0.4               # arrive and come to rest before the descent gets there
    if window > 1.1 * natural and dist > SKIP_TOL:
        slower = max(1.5 * dist / window, 30.0)
        if slower <= SPEED_FAST:                 # slow enough to stream it on the leash, also in turbo
            plan["turn"] = (min(slower, speed), False)
        else:
            plan["turn"] = (max(dist / max(window - speed / (TURBO_ACCEL * 100.0), 0.2), SPEED_FAST), True)
    return plan


async def _run_blended(arm, route, max_speed: float, env: dict, stats: dict, ctx: dict, index0: int = 0) -> float:
    """Run rise / turn / descend with the turn overlapping the other two above CLEAR_HEIGHT_M.

      rise starts at once; the turn (base yaw M1 and forearm roll M5) starts as soon as the
      measured pose is above the clear height (or the rise is done); the descent starts when the
      rise has arrived but may only go below the clear height once the turn has arrived and is
      standing still. The wrist roll M7 sweeps nothing and runs alongside from start to end
      (M7_FREE). How fast the turn and the roll run: _blend_plan().
    Each segment keeps its own guards and arrival check. If one fails, all are stopped."""
    segs = {name: dict(goal) for name, goal in route}
    ranges = arm.ranges()
    tel0 = arm.telemetry(max_age_s=STALE_S)
    grip = tel0[GRIPPER]["pos"]
    plan = _blend_plan(segs, {m: tel0[m]["pos"] for m in MOVABLE}, max_speed, ranges, grip)
    tasks: dict[str, asyncio.Future] = {}
    # Where a joint that no segment is driving right now has to stay: (goal register, position).
    rest = {m: (tel0[m]["goal"], tel0[m]["pos"]) for m in MOVABLE}
    seen_done: set[str] = set()
    drift_limit = TURBO_DRIFT_LIMIT if max_speed >= SPEED_TURBO else DRIFT_LIMIT
    t0 = _now()

    def height(overrides: dict) -> float:     # the measured pose, with these joints replaced
        tel = arm.telemetry(max_age_s=STALE_S)
        pose = {**{m: tel[m]["pos"] for m in MOVABLE}, **overrides, GRIPPER: grip}
        if "roll" not in segs:
            return _height(pose, ranges)
        # The wrist roll is on its way and carries the camera a little up or down: count the
        # lower of where it is and where it is going.
        return min(_height(pose, ranges), _height({**pose, **segs["roll"]}, ranges))

    def start(name: str, **kw):
        speed, turbo = plan[name]
        others = tuple(m for n, g in segs.items() if n != name for m in g if m not in segs[name])
        tasks[name] = asyncio.ensure_future(_run_segment(
            arm, name, segs[name], speed, env, stats, ctx, {}, turbo=turbo, others=others, **kw))

    def idle_guard():
        """Joints between segments: the rise's joints before the descent takes them, the turn's
        before it starts and after it has arrived. Each segment only watches its own joints."""
        tel = arm.telemetry(max_age_s=STALE_S)
        for name, t in tasks.items():
            if t.done() and name not in seen_done:
                seen_done.add(name)
                rest.update({m: (tel[m]["goal"], tel[m]["pos"]) for m in segs[name]})
        busy = {m for n, t in tasks.items() if not t.done() for m in segs[n]}
        for m in MOVABLE:
            if m in busy:
                continue
            if abs(tel[m]["goal"] - rest[m][0]) > GOAL_MATCH:
                raise MotionError(OTHER_WRITER.format(m))
            if abs(tel[m]["pos"] - rest[m][1]) > drift_limit:
                raise BlockedError(f"motor {m} was pushed off its position while holding still")

    async def until(done):
        while True:
            running = [n for n, t in tasks.items() if not t.done()]
            ctx["moving"] = _last["driving"] = tuple(sorted({m for n in running for m in segs[n]}))
            status.update(phase="+".join(running) or status["phase"],
                          phase_index=index0 + max(1, sum(n in tasks for n, _ in route)))
            for t in tasks.values():
                if t.done() and not t.cancelled() and t.exception():
                    raise t.exception()
            if BLEND_IDLE_GUARD:
                idle_guard()
            if done():
                return
            await asyncio.sleep(1 / RATE_HZ)

    def finished(name: str) -> bool:
        return name not in segs or (name in tasks and tasks[name].done())

    try:
        if "roll" in segs:
            start("roll")
        if "rise" in segs:
            start("rise", need_still=not (HUB_PASS_THROUGH and "descend" in segs))
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
    return _now() - t0


async def _inspect(arm, target: str | None = None) -> dict:
    """Read-only look at the arm. Returns {"blockers": [...], "where", "present", "views", "env"}.

    `blockers` lists every reason a move cannot start right now (empty = good to go).
    `where` is filled in whenever the joints are readable and transit is trusted, also while
    the arm is limp, so a person can see the arm reach the hub while posing it by hand."""
    out = {"blockers": [], "where": None, "present": None, "views": None, "env": None, "links": None}
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
        try:
            import fk  # noqa: F401  (direct lines and "above the clear height" need the geometry model)
            out["links"] = _Links(views, ranges, present[GRIPPER])
        except Exception:
            pass
        out["where"] = locate(present, views, out["links"],
                              prefer=((target,) if target else ()) + tuple(_last["line"]))
    limp = [m for m in MOVABLE if not tel[m]["torque"]]
    if limp:
        blockers.append("the arm is limp; press 'Hold here' first (with the arm supported)")
    else:
        for m in MOVABLE:
            if abs(tel[m]["goal"] - tel[m]["pos"]) > HELD_GOAL_TOL:
                blockers.append(f"motor {m} is being driven somewhere else (goal {tel[m]['goal']}, "
                                f"position {tel[m]['pos']}); press 'Hold here' to reset it")
    return out


async def _fresh_again(arm, timeout_s: float = 1.0):
    """Checking a direct line with the geometry model keeps the event loop busy (half a second the
    first time in a process, when it loads the link meshes), and on the real arm the task that
    receives joint data cannot run meanwhile. Nothing has been commanded yet at this point: give
    that task a moment, so the move does not refuse itself over data that is only late because
    we were busy. If the data stays stale, that is the usual refusal."""
    deadline = _now() + timeout_s
    while True:
        await asyncio.sleep(0)
        try:
            arm.joints(max_age_s=STALE_S)
            return
        except RuntimeError:
            if _now() > deadline:
                raise
        await asyncio.sleep(0.02)


def _log_move(record: dict):
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


async def describe(arm, name: str | None = None, allow_unknown_start: bool = False) -> dict:
    """Where the arm is and whether a move (to `name`) could start now. Moves nothing.

    {"ok": bool, "error": first blocker or None, "blockers": [...], "where": {...} | None,
     "route": [segment names], "kind": "direct" | "arc" | "hub" | None}
    `kind` says how the move to `name` would go from here (see _kind); None without a route."""
    try:
        info = await _inspect(arm, name)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e), "blockers": [str(e)], "where": None, "route": [], "kind": None}
    blockers, route = list(info["blockers"]), []
    if (info["where"] or {}).get("where") == "unknown" and not allow_unknown_start:
        blockers.append("the arm is not at a known pose; move it by hand to the transit pose "
                        "(Go limp, pose it, Hold here)")
    if name and info["views"] and name in info["views"] and TRANSIT in info["views"]:
        try:
            route = [seg for seg, _ in plan_route(info["present"], name, info["views"],
                                                  allow_unknown_start=allow_unknown_start, links=info["links"],
                                                  direct=not allow_unknown_start)]
        except MotionError as e:
            if str(e) not in blockers and "not at a known pose" not in str(e):
                blockers.append(str(e))
    return {"ok": not blockers, "error": blockers[0] if blockers else None, "blockers": blockers,
            "where": info["where"], "route": route, "kind": _kind(route)}


async def go_to_view(arm, name: str, max_speed: float = SPEED_SLOW,
                     allow_unknown_start: bool = False, along: tuple[str, float] | None = None,
                     load_guard: bool = False, direct_only: bool = False) -> dict:
    """Move to a saved view. The interface everyone codes against:
    {"ok", "view", "error", "duration", "route", "kind", "hold"}.
    `kind`: "direct" (one straight move of all joints), "arc" (the same, lifted) or "hub"; None if refused.

    Cancel the task running this to stop: the arm holds where it is and CancelledError propagates.
    `allow_unknown_start` is the supervised "recover to transit" path; it forces SPEED_FIRST.
    `hold` is None when nothing was commanded, else "confirmed" / "unconfirmed" after an abort.
    `along=(toward, s)`: stop a fraction s (<= ALONG_MAX) of the way along the checked line from
    `name` to `toward` instead (see plan_along); the arm must be at `name` or on that line.
    """
    refused = {"ok": False, "view": name, "duration": 0.0, "route": [], "kind": None, "hold": None}   # same keys as a result
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
        return await _go_to_view(arm, name, max_speed, allow_unknown_start, along, load_guard, direct_only)
    finally:
        if lock_file:
            lock_file.close()


async def _go_to_view(arm, name: str, max_speed: float, allow_unknown_start: bool,
                      along: tuple[str, float] | None, load_guard: bool, direct_only: bool) -> dict:
    async with _move_lock:
        t0 = time.time()
        stats = {"max_lag": {}, "max_current": {}, "max_load": {}}
        result = {"ok": False, "view": name, "error": None, "duration": 0.0, "route": [], "kind": None, "hold": None}
        status.update(moving=True, view=name, phase="check", phase_index=0, phase_count=0,
                      progress=0.0, error=None, route=[], kind=None, hold=None)
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
            present, views, env, links = info["present"], info["views"], info["env"], info["links"]
            where = info["where"]
            involved = [v for v in (name, where.get("view"), where.get("to")) if v and v != TRANSIT]
            if allow_unknown_start or not involved or not all(view_checked(v) for v in involved):
                # Unchecked view, or no checked view involved at all (hub -> transit): slowest speed.
                max_speed = min(max_speed, SPEED_FIRST)
            if along:
                if allow_unknown_start or along[0] not in views or not all(view_checked(v) for v in (name, along[0])):
                    raise MotionError("stops along a line are only for lines between two checked views")
                result["along"] = {"toward": along[0], "s": along[1]}
                route = plan_along(present, name, along[0], float(along[1]), views, links)
            else:
                route = plan_route(present, name, views, allow_unknown_start=allow_unknown_start, links=links,
                                   direct=not allow_unknown_start)
            if direct_only and any(seg not in PATH_SEGMENTS for seg, _ in route):
                raise MotionError("the move requires transit and would leave the checked camera line")
            await _fresh_again(arm)
            if load_guard:
                _trip_guard.update(PATH_TRIP_GUARD)
            result["route"] = [seg for seg, _ in route]
            result["kind"] = _kind(result["route"])
            status.update(route=result["route"], kind=result["kind"], phase_count=len(route))
            # Direct lines first, one after the other; whatever is left goes through the hub.
            lines = [(seg, goal) for seg, goal in route if seg in PATH_SEGMENTS]
            rest = route[len(lines):]
            blend = BLEND and max_speed >= SPEED_FAST and len(rest) > 1
            if blend:
                try:
                    import fk  # noqa: F401  (the clear-height check needs the geometry model)
                except Exception:
                    blend = False
            expect = dict(present)      # where each joint should be as the route progresses
            moved: set[int] = set()
            for i, (seg, goal) in enumerate(route, 1):
                if blend and i > len(lines):
                    status.update(phase="+".join(result["route"][i - 1:i]), phase_index=i, progress=0.0)
                    result["duration"] += await _run_blended(arm, rest, max_speed, env, stats, ctx,
                                                             index0=len(lines))
                    break
                status.update(phase=seg, phase_index=i, progress=0.0)
                # Hand-off: joints this segment does not move must be where earlier segments left them.
                now = arm.joints(max_age_s=STALE_S)
                for m in moved - set(goal):
                    if abs(now[m] - expect[m]) > ARRIVE_TOL:
                        raise MotionError(f"motor {m} is {abs(now[m] - expect[m])} steps from where '{seg}' "
                                          "needs it to be; not continuing")
                ctx["moving"] = _last["driving"] = tuple(goal)
                if seg in PATH_SEGMENTS:
                    _last["line"] = goal.ends       # so a Stop on it is recognised as this line, not a neighbour
                seg_speed, turbo = _segment_speed(seg, max_speed)
                if (seg == "direct" and DIRECT_TURBO_PROFILE and max_speed >= SPEED_TURBO
                        and not _ends_lower(arm.joints(max_age_s=STALE_S), goal, arm.ranges())):
                    seg_speed, turbo = max_speed, True
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
            _trip_guard.clear()
            result["duration"] = round(result["duration"], 2)
            status.update(moving=False, phase=None, error=result["error"], hold=result["hold"],
                          last_result=dict(result),
                          progress=1.0 if result["ok"] else status["progress"])
            if not getattr(arm, "is_sim", False):    # the log is for tuning from real runs only
                _log_move({"t": t0, "took": round(time.time() - t0, 2), "speed": max_speed, **result, **stats})
    return result
