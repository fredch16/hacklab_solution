"""Motion layer on the simulated 8-motor arm, using the real views.json.

    python3 tests/test_motion_sim.py                          # simulated time, a few seconds
    MOTION_TEST_REALTIME=1 python3 tests/test_motion_sim.py   # the same on the real clock (~75 s)

Covers: limits and clamping, where-is-the-arm classification, route planning (order of joint
groups), a full tour, the gripper never being written, and every refusal/abort path:
limp arm, mirroring, unknown pose, stuck joint, stale data, failed writes, Stop, double start.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import motion
import virtual_time
from motion import (MOVABLE, PITCH, TRANSIT, TURN, clamp_pose, envelope, go_to_view,
                    locate, plan_route)
from sim_arm import SIM_RANGES, SimArm

FAST = 2500                      # steps/s, so the suite runs in seconds
motion.MIN_SEGMENT_S = 0.05
motion.SETTLE_POLL_S = 0.04
motion.LOG_PATH = Path("/dev/null")
motion.view_checked = lambda name: True   # the "unchecked views run slow" rule is tested on its own below
motion.SPEED_FIRST = FAST                  # ...and so is the forced first-run speed
motion.SPEED_NORMAL = FAST                 # descents are capped at normal speed; keep the suite quick

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
    check(plan_route(VIEWS["top"], "top", VIEWS) == [("descend", {m: VIEWS["top"][m] for m in PITCH})],
          "already there: only a corrective descend onto the exact pose")
    check([s for s, _ in plan_route(turned, "wide", VIEWS)] == ["rise", "turn", "descend"],
          "from the hub: still an exact rise first")
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
    t0 = virtual_time.now()
    for name in ["top", "left", "right", "close", "side_low", "wide", TRANSIT, "home"]:
        r = await go_to_view(arm, name, max_speed=FAST)
        check(r["ok"], f"move to {name}: {r['error']}")
        if name != TRANSIT:
            check(at(arm, name), f"arrived at {name}")
    print(f"tour of 8 moves ok in {virtual_time.now() - t0:.1f}s; last route {r['route']}")
    check(8 not in arm.written_ids, "the gripper was never written")
    check(arm.joints()[8] == motion.load_views()["home"][8], "the gripper did not move")
    restricted = SimArm()
    r = await go_to_view(restricted, "top", direct_only=True)
    check(not r["ok"] and "requires transit" in r["error"] and not restricted.written_ids,
          "calibration refuses a transit route before writing to the motors")
    restricted.load[2] = motion.PATH_TRIP_GUARD[2][0] + 35   # just over the guard (55 % since the 60 % servo threshold)
    r = await go_to_view(restricted, "top", load_guard=True)
    check(not r["ok"] and "stayed above" in r["error"] and r["hold"] == "confirmed",
          f"director preset move locks on high shoulder load: {r}")
    j = arm.joints()
    check(all(ENV[m][0] - 5 <= j[m] <= ENV[m][1] + 5 for m in MOVABLE), "home was clamped inside the envelope")

    # --- refusals: nothing may be written ------------------------------------------
    r = await go_to_view(arm, "nope")
    check(not r["ok"] and "unknown view" in r["error"], "unknown view")

    limp = SimArm(); await limp.set_torque(False); before = limp.writes
    r = await go_to_view(limp, "top", max_speed=FAST)
    check(not r["ok"] and "limp" in r["error"] and limp.writes == before, "limp arm refused, nothing written")
    check(r["hold"] is None, "a refusal reports no hold (nothing was commanded)")
    d = await motion.describe(limp, "top")
    check(not d["ok"] and d["where"] == {"where": "view", "view": "home"} and any("limp" in b for b in d["blockers"]),
          "describe() still says where a limp arm is")
    check(await motion.hold(limp) == "limp" and limp.writes == before,
          "hold() does not energise a limp arm, and says there was nothing to hold")
    await motion.engage(limp)
    check(limp.telemetry()[1]["torque"] and not limp.telemetry()[8]["torque"], "engage holds joints 1-7, not the gripper")
    limp.torque_written_ids.clear()
    await motion.release(limp)
    check(limp.torque_written_ids == set(MOVABLE), "release only switches off joints 1-7")
    d = await motion.describe(SimArm(start={**VIEWS["top"], 8: 1279, 3: VIEWS["top"][3] + 400, 1: VIEWS["top"][1] + 300}))
    check(not d["ok"] and d["where"] == {"where": "unknown"} and any("known pose" in b for b in d["blockers"]),
          "describe() treats an unknown pose as a blocker")
    limp.mirror = "5B61033001"
    try:
        await motion.engage(limp); check(False, "engage must refuse while mirrored")
    except motion.MotionError as e:
        check("mirroring" in str(e), "engage refuses while Station mirrors a leader onto the arm")
    limp.mirror = None
    await motion.engage(limp)
    limp._goal[2] = limp._pos[2] + 30; limp.stuck = {2}; before = limp.writes     # resting short of its goal
    await motion.engage(limp)
    check(limp.writes == before, "pressing Hold here again does not rewrite goals (no second sag)")
    limp._goal[2] = limp._pos[2]; limp.stuck = set()
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
    async def still(a):     # after a hold the arm may finish the last few steps to its goal, then rests
        await asyncio.sleep(0.4); j1 = a.joints(); await asyncio.sleep(0.3); return j1 == a.joints()

    stuck = SimArm(); stuck.stuck = {2}
    r = await go_to_view(stuck, "top", max_speed=400)
    check(not r["ok"] and "motor 2" in r["error"] and "not following" in r["error"], f"stuck joint: {r['error']}")
    check(await still(stuck), "holding after a stuck joint")
    check(abs(stuck._goal[2] - stuck.joints()[2]) <= motion.RELAX_TOL, "goal pulled back next to the measured position")
    check(r["hold"] == "confirmed", "the abort hold was read back")

    blind = SimArm()
    task = asyncio.create_task(go_to_view(blind, "top", max_speed=400))
    await asyncio.sleep(0.4); blind.stale = True
    r = await task; blind.stale = False
    check(not r["ok"] and "stale" in r["error"], f"stale data: {r['error']}")
    lead = max(abs(blind._goal[m] - blind.joints()[m]) for m in MOVABLE)
    check(lead <= motion.lead_limit(400), f"last goal stays near the arm when data goes stale (lead {lead})")
    check(r["hold"] == "unconfirmed" and "HOLD NOT CONFIRMED" in r["error"], "a hold we cannot confirm is reported as such")

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
    check(not r["ok"] and "rejected" in r["error"], f"failed writes abort: {r['error']}")
    check(r["hold"] == "unconfirmed" and "HOLD NOT CONFIRMED" in r["error"],
          "...and since the hold could not be written either, it is reported as unconfirmed")

    # --- the leash: a goal is never sent further ahead of the arm than lead_limit ------
    slowpoke = SimArm(speed=60)                      # an arm that cannot keep up at all
    worst = 0
    async def watch_lead():
        nonlocal worst
        while True:
            pos = slowpoke.joints()
            worst = max(worst, max(abs(slowpoke._goal[m] - pos[m]) for m in MOVABLE))
            await asyncio.sleep(0.01)
    w = asyncio.create_task(watch_lead())
    slowpoke._speed = {m: 60 for m in slowpoke._speed}
    real_write = slowpoke.write_goals
    async def write_keep_speed(goals, speed=None, accel=None):   # ignore our speed ceiling: stay slow
        await real_write(goals)
    slowpoke.write_goals = write_keep_speed
    r = await go_to_view(slowpoke, "top", max_speed=400)
    w.cancel()
    check(worst <= motion.lead_limit(400) + 2, f"goal never led the arm by more than the leash (worst {worst:.0f})")

    # --- starting slightly off: the route pulls the arm onto the exact poses ----------
    near_hub = SimArm(start={**VIEWS[TRANSIT], 8: 1758, 2: VIEWS[TRANSIT][2] + 50, 3: VIEWS[TRANSIT][3] - 50})
    motion.DIRECT = False
    r = await go_to_view(near_hub, "wide", max_speed=FAST)
    check(r["ok"] and r["route"] == ["rise", "turn", "descend"] and at(near_hub, "wide"), "near the hub: exact rise, then on")
    motion.DIRECT = True
    near_hub = SimArm(start={**VIEWS[TRANSIT], 8: 1758, 2: VIEWS[TRANSIT][2] + 50, 3: VIEWS[TRANSIT][3] - 50})
    r = await go_to_view(near_hub, "wide", max_speed=FAST)
    check(r["ok"] and r["route"] == ["direct"] and r["kind"] == "direct" and at(near_hub, "wide"),
          "near the transit pose: one direct line that ends on the exact pose (tests/test_motion_opt.py has the rest)")
    raw_home = SimArm(start=dict(motion.load_views()["home"]))      # M2 = 1061, below the allowed 1101
    r = await go_to_view(raw_home, "home", max_speed=FAST)
    check(r["ok"] and r["route"] == ["descend"] and raw_home.joints()[2] >= ENV[2][0] - 5,
          "resting on a joint stop: pulled just inside the allowed range")

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
    lead = max(abs(arm._goal[m] - arm.joints()[m]) for m in MOVABLE)
    check(lead <= motion.RELAX_TOL and motion.status["hold"] == "confirmed",
          f"Stop: goals pulled back to the arm, hold confirmed (lead {lead})")

    pressed = SimArm(); pressed.stuck = {2}                 # Stop while a joint is being blocked
    task = asyncio.create_task(go_to_view(pressed, "top", max_speed=100))
    await asyncio.sleep(0.6)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    lead = abs(pressed._goal[2] - pressed.joints()[2])
    check(lead <= motion.RELAX_TOL, f"Stop on a blocked joint stops it pushing (lead {lead})")

    # ...also when the goal registers are read back late: "confirmed" must mean the real goals
    late = SimArm(); late.stuck = {2}; late.echo_delay = 0.2
    task = asyncio.create_task(go_to_view(late, "top", max_speed=100))
    await asyncio.sleep(0.9)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    lead = abs(late._goal[2] - late.joints()[2])
    check(motion.status["hold"] == "confirmed" and lead <= motion.RELAX_TOL,
          f"Stop with delayed read-back: real goal pulled back before 'confirmed' (lead {lead})")
    delayed = SimArm(); delayed.echo_delay = 0.2
    r = await go_to_view(delayed, "wide", max_speed=400)
    check(r["ok"], f"a normal move works with delayed read-back: {r['error']}")

    # --- a joint resting against its stop has not arrived ---------------------------------
    on_stop = SimArm(start=dict(motion.load_views()["home"])); on_stop.stuck = {2}    # M2 at 1061, min 1071
    r = await go_to_view(on_stop, "home", max_speed=400)
    check(not r["ok"] and "motor 2" in r["error"], f"resting on a mechanical stop is not arrival: {r['error']}")

    # --- a pushed-back arm gets no further goals at all -----------------------------------
    shoved = SimArm()
    task = asyncio.create_task(go_to_view(shoved, "top", max_speed=100))
    await asyncio.sleep(1.0)
    shoved.stuck = {2}; shoved._pos[2] -= 150                 # knocked back 150 steps and held there
    await asyncio.sleep(0.1); n = shoved.writes
    await asyncio.sleep(0.25)
    check(shoved.writes == n, "over the leash nothing is sent, not even a repeat")
    r = await task
    check(not r["ok"] and r["hold"] == "confirmed", f"...and the move ends: {r['error']}")
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

    # --- hold keeps the goal of a joint that is merely sagging under load -------------------
    sag = SimArm(start={**VIEWS["top"], 8: 1279})
    sag._goal[2] = sag._pos[2] + 35; sag.stuck = {2}       # shoulder rests 35 steps short of its goal
    before = sag.writes
    await motion.hold(sag)
    check(sag.writes == before and sag._goal[2] == sag._pos[2] + 35, "hold leaves a sagging joint's goal alone")
    sag._goal[2] = sag._pos[2] + 200                        # ...but a joint still pushing far gets pulled back
    await motion.hold(sag)
    check(abs(sag._goal[2] - sag._pos[2]) <= motion.FREEZE_TOL, "hold pulls back a goal that is far from the joint")

    # --- blended moves: the turn overlaps rise and descent, but only above the clear height ----
    # (the route through the hub; with DIRECT on most of these pairs take one direct line instead)
    motion.DIRECT = False
    for speed, label in ((motion.SPEED_FAST, "fast"), (motion.SPEED_TURBO, "turbo")):
        for a, b in (("top", "right"), ("right", "left"), ("left", "close"), ("close", "home")):
            blend = SimArm(start={**VIEWS[a], 8: 1279})
            low_while_turning, overlapped = 9.9, False
            async def watch_blend():
                nonlocal low_while_turning, overlapped
                while True:
                    j = blend.joints()
                    turning = any(abs(j[m] - VIEWS[a][m]) > 30 and abs(j[m] - VIEWS[b][m]) > 30 for m in TURN)
                    pitching = any(abs(blend._goal[m] - j[m]) > 3 for m in PITCH)
                    if turning:
                        low_while_turning = min(low_while_turning, motion._height({**j}, SIM_RANGES))
                        overlapped = overlapped or pitching
                    await asyncio.sleep(0.01)
            w = asyncio.create_task(watch_blend())
            t0 = virtual_time.now()
            r = await go_to_view(blend, b, max_speed=speed)
            w.cancel()
            check(r["ok"] and at(blend, b), f"blended {label} {a}->{b} arrives: {r['error']}")
            check(low_while_turning >= motion.CLEAR_HEIGHT_M - 0.03,
                  f"{label} {a}->{b}: never turning below the clear height (lowest {low_while_turning*100:.0f} cm)")
            if (a, b) == ("top", "right"):
                check(overlapped, f"{label}: the turn really overlaps the rise or descent")
                print(f"blended {label} {a}->{b}: {virtual_time.now() - t0:.1f}s, lowest while turning {low_while_turning*100:.0f} cm")
    jam = SimArm(); jam.stuck = {5}                           # the turn fails while the rise is still going
    r = await go_to_view(jam, "right", max_speed=motion.SPEED_FAST)
    check(not r["ok"] and "motor 5" in r["error"] and r["hold"] == "confirmed" and await still(jam),
          f"blended: one segment failing stops them all: {r['error']}")
    stop_blend = SimArm()
    task = asyncio.create_task(go_to_view(stop_blend, "left", max_speed=motion.SPEED_FAST))
    await asyncio.sleep(1.2); task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    lead = max(abs(stop_blend._goal[m] - stop_blend.joints()[m]) for m in MOVABLE)
    check(motion.status["hold"] == "confirmed" and lead <= motion.RELAX_TOL and await still(stop_blend),
          f"Stop during a blended move holds every joint (lead {lead})")
    motion.BLEND = False
    seq = SimArm(start={**VIEWS["top"], 8: 1279})
    r = await go_to_view(seq, "right", max_speed=motion.SPEED_FAST)
    check(r["ok"], "with BLEND off the move still runs, strictly in sequence")
    motion.BLEND = True
    motion.DIRECT = True

    # --- turbo: rise and turn on the servos' own profile -------------------------------------
    quick = SimArm()
    t0 = virtual_time.now()
    r = await go_to_view(quick, "left", max_speed=motion.SPEED_TURBO)
    check(r["ok"] and at(quick, "left") and r["route"] == ["rise", "turn", "descend"],
          f"turbo move arrives ({virtual_time.now() - t0:.1f}s): {r['error']}")
    check(8 not in quick.written_ids, "turbo never writes the gripper either")
    jam = SimArm(); jam.stuck = {3}
    r = await go_to_view(jam, "top", max_speed=motion.SPEED_TURBO)
    check(not r["ok"] and "motor 3" in r["error"] and "progress" in r["error"] and r["hold"] == "confirmed",
          f"turbo: a joint that stops making progress ends the move: {r['error']}")
    check(abs(jam._goal[3] - jam.joints()[3]) <= motion.RELAX_TOL, "...and its far-away goal is pulled back")
    brake = SimArm()
    task = asyncio.create_task(go_to_view(brake, "right", max_speed=motion.SPEED_TURBO))
    await asyncio.sleep(0.35); task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    lead = max(abs(brake._goal[m] - brake.joints()[m]) for m in MOVABLE)
    check(motion.status["hold"] == "confirmed" and lead <= motion.RELAX_TOL and await still(brake),
          f"Stop during turbo: goals pulled back to the arm (lead {lead})")
    slow_view = SimArm()
    motion.view_checked = lambda name: name != "top"
    r = await go_to_view(slow_view, "top", max_speed=motion.SPEED_TURBO)
    check(r["ok"] and slow_view.writes > 40, "an unchecked view never runs in turbo (it is streamed, slowly)")
    motion.view_checked = lambda name: True

    # --- the controller's hold after stopping a move (stop_hold) ----------------------------
    idle = SimArm(start={**VIEWS["top"], 8: 1279})
    idle._goal[2] = idle._pos[2] + 35; idle.stuck = {2}       # resting 35 steps short under load
    idle.mirror = "5B61033001"                                # so the move is refused before any goal
    r = await go_to_view(idle, "wide", max_speed=FAST)
    before = idle.writes
    check(not r["ok"] and await motion.stop_hold(idle) == "confirmed" and idle.writes == before,
          "Stop during the pre-move checks leaves a resting joint's goal alone")
    idle.mirror = None; idle.stuck = set(); idle._goal[2] = idle._pos[2]
    task = asyncio.create_task(go_to_view(idle, "wide", max_speed=400))
    await asyncio.sleep(0.6); task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.3); before = idle.writes
    check(motion.status["hold"] == "confirmed" and await motion.stop_hold(idle) == "confirmed"
          and idle.writes == before, "after a confirmed abort hold, the controller's hold rewrites nothing")

    gone = SimArm(); real_tel = gone.telemetry
    gone.telemetry = lambda max_age_s=0.5: {m: t for m, t in real_tel(max_age_s).items() if m != 4}
    try:
        await motion.hold(gone); check(False, "hold must fail when a joint has no data")
    except motion.MotionError as e:
        check("motor 4" in str(e), "hold reports a joint without data instead of skipping it")

    sticky = SimArm()
    async def ignore_torque(enable, motor_ids=None):          # the arm never actually goes limp
        pass
    sticky.set_torque = ignore_torque
    try:
        await motion.release(sticky); check(False, "release must fail when torque stays on")
    except motion.MotionError as e:
        check("could not confirm the arm is limp" in str(e), "Go limp is only reported once the arm is really limp")

    # --- unchecked views are forced to slow speed ----------------------------------------
    motion.view_checked = lambda name: name != "wide"
    motion.SPEED_FIRST = 100
    arm = SimArm(start={**VIEWS[TRANSIT], 8: 1279})
    t0 = virtual_time.now()
    r = await go_to_view(arm, "wide", max_speed=FAST)
    slow_took = virtual_time.now() - t0
    dist = max(abs(VIEWS["wide"][m] - VIEWS[TRANSIT][m]) for m in PITCH)
    check(r["ok"] and slow_took > 1.5 * dist / motion.SPEED_FIRST * 0.8,
          f"unchecked view ran at first-run speed ({slow_took:.1f}s)")

    print(f"ALL OK ({passed} checks)")


# Simulated time by default (tests/virtual_time.py): the suite takes a few seconds instead of over a
# minute and every sleep and timeout in it is exact. MOTION_TEST_REALTIME=1 runs it on the real clock.
t_start = time.perf_counter()
virtual_time.run(main(), realtime=bool(os.environ.get("MOTION_TEST_REALTIME")))
print(f"({time.perf_counter() - t_start:.0f} s of real time)")
