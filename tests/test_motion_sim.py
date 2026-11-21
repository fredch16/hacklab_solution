"""Motion layer on the simulated 8-motor arm, using the real views.json.

    python3 tests/test_motion_sim.py

Covers: limits and clamping, where-is-the-arm classification, route planning (order of joint
groups), a full tour, the gripper never being written, and every refusal/abort path:
limp arm, mirroring, unknown pose, stuck joint, stale data, failed writes, Stop, double start.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import motion
from motion import (MOVABLE, PITCH, TRANSIT, TURN, clamp_pose, envelope, go_to_view,
                    locate, plan_route)
from sim_arm import SIM_RANGES, SimArm

FAST = 8000                      # steps/s, so the suite runs in seconds
motion.MIN_SEGMENT_S = 0.05
motion.SETTLE_POLL_S = 0.04
motion.LOG_PATH = Path("/dev/null")
motion.view_checked = lambda name: True   # the "unchecked views run slow" rule is tested on its own below

ENV = envelope(SIM_RANGES)
VIEWS = {n: clamp_pose(p, ENV) for n, p in motion.load_views().items()}
NAMES = [n for n in VIEWS if n != TRANSIT]
passed = 0


def check(cond, what):
    global passed
    assert cond, what
    passed += 1


def at(arm, name, tol=motion.ARRIVE_TOL):
    j = arm.joints()
    return all(abs(j[m] - VIEWS[name][m]) <= tol for m in MOVABLE)


def groups_of(route):
    return [tuple(sorted(goal)) for _, goal in route]


async def main():
    # --- limits -----------------------------------------------------------------
    check(ENV[2] == (1101, 2995), "envelope = calibrated range minus the margin")
    for bad in ({**SIM_RANGES, 5: (3500, 500)}, {**SIM_RANGES, 3: (2000, 2100)}):
        try:
            envelope(bad); check(False, "wrapped / tiny range must be refused")
        except motion.MotionError:
            check(True, "")
    outside = motion.check_views(SIM_RANGES)
    print("views with joints outside the allowed envelope (will be clamped):")
    for name, notes in outside.items():
        print(f"  {name}: " + "; ".join(notes))
    check(all(ENV[m][0] <= VIEWS[n][m] <= ENV[m][1] for n in VIEWS for m in MOVABLE), "clamped views are inside")

    # --- where is the arm -------------------------------------------------------
    for n in NAMES:
        check(locate(VIEWS[n], VIEWS) == {"where": "view", "view": n}, f"at {n}")
    check(locate(VIEWS[TRANSIT], VIEWS)["where"] == "hub", "transit pose is the hub")
    turned = {**VIEWS[TRANSIT], 1: VIEWS["left"][1], 5: VIEWS["left"][5]}
    check(locate(turned, VIEWS)["where"] == "hub", "hub with any yaw/roll is still the hub")
    half_up = {**VIEWS["top"], **{m: (VIEWS["top"][m] + VIEWS[TRANSIT][m]) // 2 for m in PITCH}}
    check(locate(half_up, VIEWS) == {"where": "spoke", "view": "top"}, "half-way up top's spoke")
    home_up = {**VIEWS["home"], **{m: round(VIEWS["home"][m] + 0.3 * (VIEWS[TRANSIT][m] - VIEWS["home"][m])) for m in PITCH}}
    check(locate(home_up, VIEWS) == {"where": "spoke", "view": "home"}, "30% up home's spoke")
    shoulder_only = {**VIEWS["home"], 2: VIEWS[TRANSIT][2]}
    check(locate(shoulder_only, VIEWS)["where"] == "unknown", "shoulder lifted on its own is NOT on the network")
    odd = {**VIEWS["top"], 3: VIEWS["top"][3] + 400, 1: VIEWS["top"][1] + 300}
    check(locate(odd, VIEWS)["where"] == "unknown", "an arbitrary pose is unknown")

    # --- planning ---------------------------------------------------------------
    route = plan_route(VIEWS["top"], "left", VIEWS)
    check([s for s, _ in route] == ["rise", "turn", "descend"], "view -> view order")
    check(groups_of(route) == [PITCH, TURN, PITCH], "pitch joints always move together, turn on its own")
    check(route[1][1] == {m: VIEWS["left"][m] for m in TURN}, "turn goes to the target's yaw/rolls")
    check(route[0][1] == {m: VIEWS[TRANSIT][m] for m in PITCH}, "rise goes to the transit pitch configuration")
    check(route[2][1] == {m: VIEWS["left"][m] for m in PITCH}, "descend goes to the target's pitch joints")
    check(all(8 not in goal for _, goal in route), "gripper is never in a route")
    check(plan_route(VIEWS["top"], "top", VIEWS) == [], "already there")
    check([s for s, _ in plan_route(turned, "wide", VIEWS)] == ["turn", "descend"], "from the hub")
    check([s for s, _ in plan_route(half_up, "top", VIEWS)] == ["descend"], "back down the same spoke")
    check([s for s, _ in plan_route(half_up, "right", VIEWS)] == ["rise", "turn", "descend"],
          "from a spoke to another view: finish going up first")
    check([s for s, _ in plan_route(VIEWS["close"], TRANSIT, VIEWS)] == ["rise", "turn"], "to transit")
    # home -> top share yaw and roll but must still go through the hub (no shortcut)
    check([s for s, _ in plan_route(VIEWS["home"], "top", VIEWS)] == ["rise", "turn", "descend"],
          "no direct shortcut between views")
    try:
        plan_route(odd, "top", VIEWS); check(False, "unknown start must be refused")
    except motion.MotionError:
        check(True, "")
    check([s for s, _ in plan_route(odd, TRANSIT, VIEWS, allow_unknown_start=True)] == ["rise", "turn"],
          "explicit recovery goes up first")

    # --- a tour on the simulated arm ---------------------------------------------
    arm = SimArm()
    check(at(arm, "home", 80), "sim starts at home")
    t0 = time.monotonic()
    for name in ["top", "left", "right", "close", "side_low", "wide", TRANSIT, "home"]:
        r = await go_to_view(arm, name, max_speed=FAST)
        check(r["ok"], f"move to {name}: {r['error']}")
        if name != TRANSIT:
            check(at(arm, name), f"arrived at {name}")
    print(f"tour of 8 moves ok in {time.monotonic() - t0:.1f}s; last route {r['route']}")
    check(8 not in arm.written_ids, "the gripper was never written")
    check(arm.joints()[8] == motion.load_views()["home"][8], "the gripper did not move")
    j = arm.joints()
    check(all(ENV[m][0] - 5 <= j[m] <= ENV[m][1] + 5 for m in MOVABLE), "home was clamped inside the envelope")

    # --- refusals: nothing may be written ------------------------------------------
    r = await go_to_view(arm, "nope")
    check(not r["ok"] and "unknown view" in r["error"], "unknown view")

    limp = SimArm(); await limp.set_torque(False); before = limp.writes
    r = await go_to_view(limp, "top", max_speed=FAST)
    check(not r["ok"] and "limp" in r["error"] and limp.writes == before, "limp arm refused, nothing written")
    await motion.hold(limp)
    check(limp.writes == before, "hold() does not energise a limp arm")
    await motion.engage(limp)
    check(limp.telemetry()[1]["torque"] and not limp.telemetry()[8]["torque"], "engage holds joints 1-7, not the gripper")
    r = await go_to_view(limp, "top", max_speed=FAST)
    check(r["ok"], f"after engage the move runs: {r['error']}")

    mir = SimArm(); mir.mirror = "5B61033001"; before = mir.writes
    r = await go_to_view(mir, "top", max_speed=FAST)
    check(not r["ok"] and "mirroring" in r["error"] and mir.writes == before, "mirroring refused")

    lost = SimArm(start={**VIEWS["top"], 8: 1279, 3: VIEWS["top"][3] + 400, 1: VIEWS["top"][1] + 300}); before = lost.writes
    r = await go_to_view(lost, "top", max_speed=FAST)
    check(not r["ok"] and "known pose" in r["error"] and lost.writes == before, "unknown pose refused")
    r = await go_to_view(lost, TRANSIT, max_speed=FAST, allow_unknown_start=True)
    check(r["ok"] and r["route"] == ["rise", "turn"], f"explicit recovery to transit: {r['error']}")

    # --- aborts: the arm must end up holding still -----------------------------------
    async def still(a):
        j1 = a.joints(); await asyncio.sleep(0.3); return j1 == a.joints()

    stuck = SimArm(); stuck.stuck = {2}
    r = await go_to_view(stuck, "top", max_speed=FAST)
    check(not r["ok"] and "motor 2" in r["error"] and "not following" in r["error"], f"stuck joint: {r['error']}")
    check(await still(stuck), "holding after a stuck joint")
    check(abs(stuck._goal[2] - stuck.joints()[2]) <= 2, "goal pulled back to the measured position")

    blind = SimArm()
    task = asyncio.create_task(go_to_view(blind, "top", max_speed=400))
    await asyncio.sleep(0.4); blind.stale = True
    r = await task; blind.stale = False
    check(not r["ok"] and "stale" in r["error"], f"stale data: {r['error']}")
    lead = max(abs(blind._goal[m] - blind.joints()[m]) for m in MOVABLE)
    check(lead <= motion.LEAD_LIMIT, f"last goal stays near the arm when data goes stale (lead {lead})")

    hot = SimArm(); hot.load = {2: 950}
    r = await go_to_view(hot, "top", max_speed=400)
    check(not r["ok"] and "motor 2" in r["error"] and "full effort" in r["error"], f"stalled-load guard: {r['error']}")
    check(await still(hot), "holding after a load abort")

    other = SimArm(); other.hijack = {1: 1500}          # e.g. Station mirroring writes the base yaw
    r = await go_to_view(other, "top", max_speed=400)
    check(not r["ok"] and "something else is commanding motor 1" in r["error"], f"foreign writer: {r['error']}")

    deaf = SimArm()
    task = asyncio.create_task(go_to_view(deaf, "top", max_speed=400))
    await asyncio.sleep(0.4); deaf.fail_writes = True
    r = await task
    check(not r["ok"], f"failed writes abort: {r['error']}")

    # --- Stop, then carry on from wherever it stopped --------------------------------
    arm = SimArm()
    task = asyncio.create_task(go_to_view(arm, "left", max_speed=400))
    await asyncio.sleep(0.2)
    r2 = await go_to_view(arm, "top")
    check(not r2["ok"] and r2["error"] == "already moving", "second move while moving is rejected")
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task; check(False, "cancel must propagate")
    except asyncio.CancelledError:
        check(True, "")
    check(await still(arm), "holding after Stop")
    check(motion.status["error"] == "stopped" and not motion.status["moving"], "status says stopped")
    where = locate(arm.joints(), VIEWS)
    check(where["where"] in ("spoke", "hub", "view"), f"after Stop the arm is still on the network: {where}")
    r = await go_to_view(arm, "right", max_speed=FAST)
    check(r["ok"] and at(arm, "right"), f"new target after Stop: {r['error']}")

    # --- views are tied to the calibration they were captured with -----------------------
    real_read = motion._read_views_file
    shifted = {str(m): [lo + 40, hi + 40] for m, (lo, hi) in SIM_RANGES.items()}
    motion._read_views_file = lambda path=None: {n: ({**v, "ranges": shifted} if n == "top" else v)
                                                 for n, v in real_read(path).items()}
    recal = SimArm(); before = recal.writes
    r = await go_to_view(recal, "top", max_speed=FAST)
    check(not r["ok"] and "recalibrated" in r["error"] and recal.writes == before, f"recalibration refused: {r['error']}")
    r = await go_to_view(recal, "wide", max_speed=FAST)
    check(r["ok"], "views without a calibration mismatch still run")
    motion._read_views_file = real_read

    # --- unchecked views are forced to slow speed ----------------------------------------
    motion.view_checked = lambda name: name != "wide"
    arm = SimArm(start={**VIEWS[TRANSIT], 8: 1279})
    t0 = time.monotonic()
    r = await go_to_view(arm, "wide", max_speed=FAST)
    slow_took = time.monotonic() - t0
    dist = max(abs(VIEWS["wide"][m] - VIEWS[TRANSIT][m]) for m in PITCH)
    check(r["ok"] and slow_took > 1.5 * dist / motion.SPEED_SLOW * 0.8,
          f"unchecked view ran at slow speed ({slow_took:.1f}s)")

    print(f"ALL OK ({passed} checks)")


asyncio.run(main())
