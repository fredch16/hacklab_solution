"""Direct moves and the other speed-ups in motion.py, on the simulated arm with its realistic
response (lag, acceleration limit, shoulder sag) and a virtual clock, so it is quick and gives
the same result every run.

    python3 tests/test_motion_opt.py

Covers: which pairs may go direct and why not (geometry rule, checked flag, per-view opt-out),
every ordered pair at normal / fast / turbo (arrives, takes the route route_kind() promised,
stays on the checked line, never turns base yaw or forearm roll below the clear height on a hub
route), Stop in the middle of each kind of move (holding, recognised by locate(), carries on),
a joint failing mid-move, and the old guards on the new kinds of move.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fk
import motion

# Exercise turbo direct as well as the default lift-over mode.
motion.ARC = True
motion.DIRECT_TURBO_PROFILE = True
import virtual_time
from motion import MOVABLE, PITCH, TRANSIT, clamp_pose, envelope, go_to_view, locate
from sim_arm import SIM_RANGES, SimArm

motion.LOG_PATH = Path("/dev/null")
REAL_CHECKED = motion.view_checked
motion.view_checked = lambda name: True          # the checked / unchecked rule is tested on its own below

ENV = envelope(SIM_RANGES)
RAW = motion.load_views()
VIEWS = {n: clamp_pose(p, ENV) for n, p in RAW.items()}
NAMES = [n for n in VIEWS if n != TRANSIT]
HUB = VIEWS[TRANSIT]
SPEEDS = {"normal": motion.SPEED_NORMAL, "fast": motion.SPEED_FAST, "turbo": motion.SPEED_TURBO}
MOVING = 40.0                                    # steps/s: motion.py's own "standing still" threshold
passed = 0


def check(cond, what):
    global passed
    assert cond, what
    passed += 1


def start_pose(name):
    return {**VIEWS[name], motion.GRIPPER: RAW[name][motion.GRIPPER]}


def at(arm, name, tol=motion.ARRIVE_TOL):
    j = arm.joints()
    return all(abs(j[m] - VIEWS[name][m]) <= tol for m in MOVABLE)


def links():
    return motion._Links(VIEWS, SIM_RANGES, 1279)


async def settle(arm):
    await asyncio.sleep(1.0)                     # the simulated shoulder sags onto its resting place


async def still(arm):
    """Resting: the lagging simulated joints creep their last steps for a second or two, then nothing."""
    await asyncio.sleep(2.5)
    before = arm.joints()
    await asyncio.sleep(1.0)
    after = arm.joints()
    return all(abs(after[m] - before[m]) <= 1 for m in before)


async def watched_move(arm, target, speed, plan=None, start=None):
    """Run a move and sample the arm every 10 ms. Returns (result, facts about what it did)."""
    facts = {"turn_low": 9.9, "off_line": 0.0, "lead": 0.0, "part_dip": 0.0}
    ranges = arm.ranges()
    ends = None
    if plan is not None:
        ends = [{mp: fk.part_heights({**VIEWS[n], 8: 1279}, ranges, mp) for mp in ("viewer", "true_scale")}
                for n in (start, target)]

    async def watch():
        prev, prev_t = dict(arm._pos), virtual_time.now()
        n = 0
        while True:
            await asyncio.sleep(0.01)
            pos = arm.joints()
            now = virtual_time.now()
            vel = {m: abs(arm._pos[m] - prev[m]) / max(now - prev_t, 1e-6) for m in (1, 5)}
            prev, prev_t = dict(arm._pos), now
            if max(vel.values()) > MOVING:
                facts["turn_low"] = min(facts["turn_low"], motion._height(pos, ranges))
            facts["lead"] = max(facts["lead"], max(abs(arm._goal[m] - arm._pos[m]) for m in MOVABLE))
            n += 1
            if plan is not None and n % 4 == 0:
                facts["off_line"] = max(facts["off_line"],
                                        motion._on_path(pos, VIEWS[start], VIEWS[target], HUB, plan["lift"])[0])
                for mp in ("viewer", "true_scale"):
                    h = fk.part_heights(pos, ranges, mp)
                    for part in h:
                        low = min(ends[0][mp][part], ends[1][mp][part])
                        facts["part_dip"] = max(facts["part_dip"], low - h[part])

    w = asyncio.ensure_future(watch())
    try:
        r = await go_to_view(arm, target, max_speed=speed)
    finally:
        w.cancel()
    return r, facts


async def stop_after(arm, target, speed, delay):
    task = asyncio.ensure_future(go_to_view(arm, target, max_speed=speed))
    await asyncio.sleep(delay)
    task.cancel()
    try:
        await task
        return False
    except asyncio.CancelledError:
        return True


async def main():
    L = links()

    # --- the geometry rule: which pairs may go in a straight line ------------------------------
    must_not_be_straight = [("left", "right"), ("right", "side_low"), ("right", "top"),
                            ("right", "close"), ("close", "side_low")]
    for a, b in must_not_be_straight:
        for x, y in ((a, b), (b, a)):
            plan = L.plan(x, y)
            check(not (plan["ok"] and plan["lift"] == 0), f"{x} -> {y}: the straight line dips, never direct")
            problem = motion._line_problem(VIEWS[x], VIEWS[y], HUB, 0.0, SIM_RANGES, 1279)
            check(problem and "dip" in problem, f"{x} -> {y}: refused for dipping ({problem})")
    motion.ARC = False
    for a, b in must_not_be_straight:
        check(not links().plan(a, b)["ok"], f"{a} <-> {b}: without arcs it goes the long way")
        check([s for s, _ in motion.plan_route(VIEWS[a], b, VIEWS, links=links(), direct=True)]
              == ["rise", "turn", "descend"], f"{a} -> {b}: planned through the hub")
    motion.ARC = True
    for a, b in (("top", "wide"), ("wide", "close"), ("top", "close"), ("left", "wide"), ("right", "wide"),
                 ("top", TRANSIT), ("right", TRANSIT)):
        plan = L.plan(a, b)
        check(plan["ok"] and plan["lift"] == 0 and L.plan(b, a) == plan, f"{a} <-> {b}: straight line allowed ({plan['reason']})")
        route = motion.plan_route(VIEWS[a], b, VIEWS, links=L, direct=True)
        check([s for s, _ in route] == ["direct"] and set(route[0][1]) == set(MOVABLE) and 8 not in route[0][1],
              f"{a} -> {b}: one segment 'direct' with all seven joints, never the gripper")
        check(route[0][1] == VIEWS[b], "a direct segment ends on the exact target pose")
        check([s for s, _ in motion.plan_route(VIEWS[a], b, VIEWS)] in (["rise", "turn", "descend"], ["rise", "turn"]),
              "plan_route() without links still plans through the hub")
    # home: opted out in views.json, and refused by the model as well (folded: no self-collision check)
    check(not motion.view_direct("home") and all(motion.view_direct(n) for n in NAMES if n != "home"),
          'views.json: "direct": false on home only')
    for other in [n for n in NAMES if n != "home"] + [TRANSIT]:
        plan = L.plan("home", other)
        check(not plan["ok"] and "always go through" in plan["reason"], f"home <-> {other}: opted out")
        check([s for s, _ in motion.plan_route(VIEWS["home"], other, VIEWS, links=L, direct=True)][0] == "rise",
              f"home -> {other}: through the hub")
        folded = motion.direct_plan(VIEWS["home"], VIEWS[other], HUB, SIM_RANGES)
        check(not folded["ok"] and "folds the arm" in folded["reason"],
              f"home <-> {other}: without the opt-out the self-clearance floor still refuses")
    # lifting the shoulder alone out of home is what round 1 showed going into the desk: the model agrees
    alone = fk.path_min_height({**VIEWS["home"], 8: 1279}, {2: HUB[2]}, SIM_RANGES, moving_ids=[2], mapping="worst")
    check(alone[0] < 0, f"shoulder alone out of home goes below the desk in the model ({alone[0] * 100:.1f} cm)")
    # every allowed path, re-checked four times as finely as the planner samples it
    arcs = 0
    for i, a in enumerate(sorted(VIEWS)):
        for b in sorted(VIEWS)[i + 1:]:
            plan = L.plan(a, b)
            if not plan["ok"]:
                continue
            arcs += plan["lift"] > 0
            worst = 0.0
            for mp in ("viewer", "true_scale"):
                hs = [fk.part_heights({**motion._path_pose(VIEWS[a], VIEWS[b], HUB, plan["lift"], k / 192), 8: 1279},
                                      SIM_RANGES, mp) for k in range(193)]
                for part in hs[0]:
                    worst = max(worst, min(hs[0][part], hs[-1][part]) - min(h[part] for h in hs))
            check(worst <= motion.DIRECT_DIP_M + 0.002, f"{a} <-> {b}: no part dips more than 1 cm ({worst * 1000:.0f} mm)")
    check(arcs >= 5, "the pairs whose straight line fails get an arc")
    motion.DIRECT_TURN_TOL = 60
    check(not links().plan("top", "right")["ok"] and not links().plan("left", "wide")["ok"]
          and links().plan("top", "wide")["ok"], "DIRECT_TURN_TOL keeps direct moves to views with the same heading")
    motion.DIRECT_TURN_TOL = None

    # --- checked flag, opt-out, route_kind (no arm needed) ---------------------------------------
    motion.view_checked = lambda name: name != "wide"
    check(not links().plan("top", "wide")["ok"] and "not marked checked" in links().plan("top", "wide")["reason"],
          "an unchecked view never goes direct")
    check(motion.route_kind("top", "wide")[0] == "hub" and motion.route_kind("top", "right")[0] == "arc",
          "route_kind follows the checked flags")
    motion.view_checked = lambda name: True
    check(motion.route_kind("top", "wide") == ("direct", "straight line"), "route_kind: direct")
    check(motion.route_kind("home", "top")[0] == "hub" and "transit" in motion.route_kind("home", "top")[1],
          "route_kind: home is opted out, with the reason")
    check(motion.route_kind("left", "right")[0] == "arc" and "dip" in motion.route_kind("left", "right")[1],
          "route_kind: arc, with what was wrong with the straight line")
    check(motion.route_kind("nope", "top")[0] == "hub", "route_kind: unknown view is not an error")
    real_read = motion._read_views_file
    motion._read_views_file = lambda path=None: {n: ({**v, "direct": False} if n == "top" else v)
                                                 for n, v in real_read(path).items()}
    check(motion.route_kind("top", "wide")[0] == "hub" and motion.route_kind("wide", "top")[0] == "hub"
          and motion.route_kind("wide", "right")[0] == "direct", '"direct": false on a view sends its moves through the hub')
    opt = SimArm(start=start_pose("wide"), realistic=True)
    r = await go_to_view(opt, "top", max_speed=motion.SPEED_FAST)
    check(r["ok"] and r["route"] == ["rise", "turn", "descend"] and r["kind"] == "hub", "...also on the arm")
    motion._read_views_file = real_read
    motion.DIRECT = False
    check(motion.route_kind("top", "wide")[0] == "hub", "DIRECT = False: everything through the hub")
    off = SimArm(start=start_pose("top"), realistic=True)
    r = await go_to_view(off, "wide", max_speed=motion.SPEED_NORMAL)
    check(r["ok"] and r["kind"] == "hub" and at(off, "wide"), "...also on the arm")
    motion.DIRECT = True

    # --- every ordered pair, at every speed ---------------------------------------------------------
    pairs = [(a, b) for a in NAMES for b in NAMES if a != b] + [(a, TRANSIT) for a in NAMES] + [(TRANSIT, b) for b in NAMES]
    kinds = {}
    for label, speed in SPEEDS.items():
        worst_turn, worst_off, worst_dip = 9.9, 0.0, 0.0
        for a, b in pairs:
            kind, why = motion.route_kind(a, b)
            kinds[(a, b)] = kind
            plan = L.plan(a, b) if kind != "hub" else None
            arm = SimArm(start=start_pose(a), realistic=True)
            await settle(arm)
            d = await motion.describe(arm, b)
            r, facts = await watched_move(arm, b, speed, plan, a)
            check(r["ok"] and at(arm, b), f"{label} {a} -> {b} arrives: {r['error']}")
            check(r["kind"] == kind == d["kind"] and r["route"] == d["route"],
                  f"{label} {a} -> {b}: goes {r['kind']} as route_kind and describe() said ({kind})")
            check(8 not in arm.written_ids, "the gripper is never written")
            if kind == "hub":
                check(r["route"][0] == "rise", "a hub route starts with the rise")
                check(facts["turn_low"] >= motion.CLEAR_HEIGHT_M - 0.01,
                      f"{label} {a} -> {b}: M1/M5 never turning below the clear height ({facts['turn_low'] * 100:.1f} cm)")
                worst_turn = min(worst_turn, facts["turn_low"])
            else:
                check(r["route"] == [kind], f"{label} {a} -> {b}: a single '{kind}' segment")
                check(facts["off_line"] <= motion.AT_TOL, f"{label} {a} -> {b}: stays on its line ({facts['off_line']:.0f} steps off)")
                check(facts["part_dip"] <= motion.DIRECT_DIP_M + 0.01,
                      f"{label} {a} -> {b}: no part dips below its end poses ({facts['part_dip'] * 1000:.0f} mm)")
                worst_off, worst_dip = max(worst_off, facts["off_line"]), max(worst_dip, facts["part_dip"])
                streamed = not (label == "turbo" and kind == "direct")
                if streamed:
                    check(facts["lead"] <= motion.lead_limit(speed) + 25,
                          f"{label} {a} -> {b}: goal never led the arm by more than the leash ({facts['lead']:.0f})")
        print(f"{label}: {len(pairs)} moves ok; hub routes turn no lower than {worst_turn * 100:.1f} cm; "
              f"direct moves at most {worst_off:.0f} steps off their line, parts at most {worst_dip * 1000:.0f} mm below their end poses")
    n = {k: sum(1 for (a, b), v in kinds.items() if v == k and TRANSIT not in (a, b)) for k in ("direct", "arc", "hub")}
    print(f"view to view: {n['direct']} direct, {n['arc']} arc, {n['hub']} through the hub")
    check(n["hub"] == 12, "only the twelve moves from or to home still go through the hub")

    # --- and with direct moves switched off: every pair through the hub, blended at fast and turbo ---
    motion.DIRECT = False
    for label in ("fast", "turbo"):
        worst = 9.9
        for a, b in pairs:
            arm = SimArm(start=start_pose(a), realistic=True)
            await settle(arm)
            r, facts = await watched_move(arm, b, SPEEDS[label])
            check(r["ok"] and at(arm, b) and r["kind"] == "hub" and r["route"][0] == "rise",
                  f"hub only, {label} {a} -> {b} arrives: {r['error']}")
            check(facts["turn_low"] >= motion.CLEAR_HEIGHT_M - 0.01,
                  f"hub only, {label} {a} -> {b}: M1/M5 never turning below the clear height ({facts['turn_low'] * 100:.1f} cm)")
            worst = min(worst, facts["turn_low"])
        print(f"DIRECT off, {label}: {len(pairs)} moves ok through the hub; M1/M5 turn no lower than {worst * 100:.1f} cm")
    motion.DIRECT = True

    # --- unchecked views keep the old behaviour exactly ----------------------------------------------
    motion.view_checked = lambda name: name != "wide"
    slow = SimArm(start=start_pose("top"), realistic=True)
    await settle(slow)
    t0 = virtual_time.now()
    r = await go_to_view(slow, "wide", max_speed=motion.SPEED_TURBO)
    check(r["ok"] and r["route"] == ["rise", "turn", "descend"] and r["kind"] == "hub" and at(slow, "wide"),
          "unchecked view: through the hub")
    check(virtual_time.now() - t0 > 20 and slow.writes > 300, "...at first-run speed, streamed")
    motion.view_checked = lambda name: True

    # --- Stop in the middle of a direct move, an arc and a turbo direct move ---------------------------
    for label, a, b, speed, delay, third in (("direct, normal", "top", "wide", motion.SPEED_NORMAL, 1.6, "right"),
                                             ("direct, slow", "wide", "close", motion.SPEED_SLOW, 2.5, "top"),
                                             ("arc, fast", "left", "right", motion.SPEED_FAST, 2.0, "top"),
                                             ("arc, normal", "right", "top", motion.SPEED_NORMAL, 1.5, "wide"),
                                             ("direct on the servo profile, turbo", "right", "wide", motion.SPEED_TURBO, 0.45, "top")):
        for onward in (b, a, third):
            arm = SimArm(start=start_pose(a), realistic=True)
            await settle(arm)
            check(await stop_after(arm, b, speed, delay), f"{label}: Stop propagates")
            check(motion.status["hold"] == "confirmed" and motion.status["error"] == "stopped", f"{label}: hold confirmed")
            check(await still(arm), f"{label}: holding still after Stop")
            lead = max(abs(arm._goal[m] - arm._pos[m]) for m in MOVABLE)
            check(lead <= motion.RELAX_TOL + max(arm.sag.values()) + 2, f"{label}: goals pulled back to the arm (lead {lead:.0f})")
            where = (await motion.describe(arm))["where"]
            check(where["where"] == "between" and {where["view"], where["to"]} == {a, b} and 0.05 < where["s"] < 0.95,
                  f"{label}: recognised as between {a} and {b}: {where}")
            check(locate(arm.joints(), VIEWS, links())["where"] == "between", "...also by locate() with nothing to prefer")
            check(locate(arm.joints(), VIEWS)["where"] == "unknown", "...which plain locate() without links does not claim")
            d = await motion.describe(arm, onward)
            check(d["ok"] and d["where"]["where"] == "between" and d["route"][0] in ("direct", "arc"),
                  f"{label}: describe() offers a route on from there: {d}")
            r = await go_to_view(arm, onward, max_speed=speed)
            check(r["ok"] and at(arm, onward), f"{label}: carries on to {onward}: {r['error']} {r['route']}")
            if onward in (a, b):
                check(len(r["route"]) == 1, f"{label}: straight along the same line to {onward}")
            else:
                check(len(r["route"]) >= 2 and r["route"][0] in ("direct", "arc"),
                      f"{label}: finishes the line to its nearer end, then on to {onward}: {r['route']}")
    # ...and if direct moves are switched off after such a Stop, the old refusal and recovery remain
    arm = SimArm(start=start_pose("top"), realistic=True)
    await settle(arm)
    await stop_after(arm, "wide", motion.SPEED_NORMAL, 1.6)
    motion.DIRECT = False
    before = arm.writes
    r = await go_to_view(arm, "wide", max_speed=motion.SPEED_NORMAL)
    check(not r["ok"] and "known pose" in r["error"] and arm.writes == before, "between two views with DIRECT off: unknown pose, refused")
    r = await go_to_view(arm, TRANSIT, max_speed=motion.SPEED_NORMAL, allow_unknown_start=True)
    check(r["ok"] and r["route"] == ["rise", "turn"], "...and Recover to transit still works")
    motion.DIRECT = True
    lost = SimArm(start={**start_pose("top"), 3: VIEWS["top"][3] + 400, 1: VIEWS["top"][1] + 300}, realistic=True)
    check(locate(lost.joints(), VIEWS, links())["where"] == "unknown", "an arbitrary pose is still unknown with links")

    # --- a joint failing in the middle stops every joint ------------------------------------------------
    for label, a, b, speed, word in (("direct", "top", "wide", motion.SPEED_NORMAL, "not following"),
                                     ("arc", "right", "top", motion.SPEED_FAST, "not following"),
                                     ("turbo direct", "right", "wide", motion.SPEED_TURBO, "progress")):
        arm = SimArm(start=start_pose(a), realistic=True)
        await settle(arm)
        task = asyncio.ensure_future(go_to_view(arm, b, max_speed=speed))
        await asyncio.sleep(0.3 if speed >= motion.SPEED_TURBO else 0.8)
        arm.stuck = {5 if label == "arc" else 2}
        r = await task
        check(not r["ok"] and word in r["error"] and r["hold"] == "confirmed", f"{label}: a stuck joint ends the move: {r['error']}")
        check(await still(arm), f"{label}: every joint holds after the failure")
        lead = max(abs(arm._goal[m] - arm._pos[m]) for m in MOVABLE)
        check(lead <= motion.RELAX_TOL + 22, f"{label}: no joint keeps pushing (lead {lead:.0f})")

    # --- the old guards on a direct move ------------------------------------------------------------------
    other = SimArm(start=start_pose("top"), realistic=True)
    other.hijack = {1: 1500}
    r = await go_to_view(other, "wide", max_speed=motion.SPEED_NORMAL)
    check(not r["ok"] and "something else is commanding motor 1" in r["error"], f"direct: another writer: {r['error']}")
    blind = SimArm(start=start_pose("top"), realistic=True)
    task = asyncio.ensure_future(go_to_view(blind, "wide", max_speed=motion.SPEED_NORMAL))
    await asyncio.sleep(0.8)
    blind.stale = True
    r = await task
    blind.stale = False
    check(not r["ok"] and "stale" in r["error"] and r["hold"] == "unconfirmed", f"direct: stale data: {r['error']}")
    limp = SimArm(start=start_pose("top"), realistic=True)
    await limp.set_torque(False)
    before = limp.writes
    r = await go_to_view(limp, "wide", max_speed=motion.SPEED_FAST)
    check(not r["ok"] and "limp" in r["error"] and limp.writes == before and r["kind"] is None, "direct: limp arm refused, nothing written")
    mir = SimArm(start=start_pose("top"), realistic=True)
    mir.mirror = "5B61033001"
    r = await go_to_view(mir, "wide", max_speed=motion.SPEED_FAST)
    check(not r["ok"] and "mirroring" in r["error"] and mir.writes == 0, "direct: mirroring refused")
    pushed = SimArm(start=start_pose("top"), realistic=True)       # stopped, then the controller's own hold
    await settle(pushed)
    await stop_after(pushed, "wide", motion.SPEED_NORMAL, 1.2)
    await asyncio.sleep(0.5)
    before = pushed.writes
    check(await motion.stop_hold(pushed) == "confirmed" and pushed.writes == before,
          "direct: after a confirmed abort hold, stop_hold() rewrites nothing")

    # ====== the route through the hub, blended (home, and everything when DIRECT is off) ============
    # --- the wrist roll sweeps nothing: only the camera point moves when M7 alone changes -----------
    m7s = [v[7] for v in VIEWS.values()]
    cam_views = cam_max = others = 0.0
    for n in VIEWS:
        for f in (0.0, 0.5, 1.0):
            pose = {**start_pose(n), **{m: VIEWS[n][m] + (HUB[m] - VIEWS[n][m]) * f for m in PITCH}}
            for mp in ("viewer", "true_scale"):
                for lo, hi, far in ((min(m7s), max(m7s), False), (pose[7], pose[7] + motion.M7_FREE_MAX, True)):
                    p0, p1 = fk.fk({**pose, 7: lo}, SIM_RANGES, mp), fk.fk({**pose, 7: hi}, SIM_RANGES, mp)
                    for point in motion.CLEAR_POINTS:
                        d = sum((x - y) ** 2 for x, y in zip(p0[point], p1[point])) ** 0.5
                        if point != "camera":
                            others = max(others, d)
                        elif far:
                            cam_max = max(cam_max, d)
                        else:
                            cam_views = max(cam_views, d)
    check(others < 1e-6, "M7 alone moves none of the forearm, wrist, gripper or jaw-tip points")
    check(cam_views < 0.016 and cam_max < 0.035,
          f"...the camera moves {cam_views * 1000:.0f} mm across all saved views and {cam_max * 1000:.0f} mm at the M7_FREE_MAX limit")
    print(f"wrist roll alone: camera point moves {cam_views * 1000:.0f} mm across the saved views, "
          f"{cam_max * 1000:.0f} mm for {motion.M7_FREE_MAX} steps; nothing else moves")

    async def blended(a, b, speed, **flags):
        """Run a -> b through the hub and time what each joint group did."""
        saved = {k: getattr(motion, k) for k in flags}
        for k, v in flags.items():
            setattr(motion, k, v)
        arm = SimArm(start=start_pose(a), realistic=True)
        await settle(arm)
        ranges = arm.ranges()
        f = {"roll_low": 9.9, "roll_first": None, "turn_first": None, "roll_after_turn": False, "turn_alone": 0.0,
             "turn_peak": 0.0, "handover": None}

        async def watch():
            prev, prev_t, t0 = dict(arm._pos), virtual_time.now(), virtual_time.now()
            while True:
                await asyncio.sleep(0.01)
                pos = arm.joints()
                now = virtual_time.now()
                vel = {m: abs(arm._pos[m] - prev[m]) / max(now - prev_t, 1e-6) for m in MOVABLE}
                prev, prev_t = dict(arm._pos), now
                turning, pitching = max(vel[1], vel[5]) > MOVING, any(vel[m] > MOVING for m in PITCH)
                if vel[7] > 8:
                    f["roll_low"] = min(f["roll_low"], motion._height(pos, ranges))
                    f["roll_first"] = f["roll_first"] if f["roll_first"] is not None else now - t0
                    f["roll_after_turn"] = f["roll_after_turn"] or "descend" in (motion.status["phase"] or "")
                if turning:
                    f["turn_first"] = f["turn_first"] if f["turn_first"] is not None else now - t0
                    f["turn_peak"] = max(f["turn_peak"], vel[1], vel[5])
                    f["turn_alone"] += 0.0 if pitching else 0.01
                if f["handover"] is None and "descend" in (motion.status["phase"] or ""):
                    f["handover"] = max(abs(pos[m] - HUB[m]) for m in PITCH)

        w = asyncio.ensure_future(watch())
        t0 = virtual_time.now()
        r = await go_to_view(arm, b, max_speed=speed)
        f["time"] = virtual_time.now() - t0
        w.cancel()
        for k, v in saved.items():
            setattr(motion, k, v)
        check(r["ok"] and at(arm, b) and r["kind"] == "hub", f"blended {a} -> {b} {flags}: {r['error']}")
        return f

    # --- 2a: the wrist roll runs from the first moment to the last ------------------------------------
    for speed in (motion.SPEED_FAST, motion.SPEED_TURBO):
        f = await blended("home", "side_low", speed)
        check(f["roll_first"] is not None and f["roll_first"] < 0.5 and f["roll_first"] < f["turn_first"] - 0.3,
              f"M7 starts at once ({f['roll_first']:.2f} s), the turn only once it is clear ({f['turn_first']:.2f} s)")
        check(f["roll_low"] < motion.CLEAR_HEIGHT_M - 0.05 and f["roll_after_turn"],
              f"M7 turns below the clear height ({f['roll_low'] * 100:.0f} cm) and on into the descent")
        g = await blended("home", "side_low", speed, M7_FREE=False)
        check(g["roll_low"] >= motion.CLEAR_HEIGHT_M - 0.01,
              f"with M7_FREE off it is gated with the turn again ({g['roll_low'] * 100:.0f} cm)")
    real_read = motion._read_views_file
    far_roll = RAW["left"][7] + 700
    motion._read_views_file = lambda path=None: {
        n: ({**v, "joints": {**v["joints"], "7": far_roll}} if n == "left" else v) for n, v in real_read(path).items()}
    saved_left = VIEWS["left"]
    VIEWS["left"] = {**saved_left, 7: far_roll}
    f = await blended("home", "left", motion.SPEED_FAST)
    check(f["roll_low"] >= motion.CLEAR_HEIGHT_M - 0.01,
          f"a wrist roll of more than M7_FREE_MAX stays gated with the turn ({f['roll_low'] * 100:.0f} cm)")
    VIEWS["left"] = saved_left
    motion._read_views_file = real_read

    # --- 2b: the turn fills its window instead of racing and waiting; nothing gets slower --------------
    for a, b in (("home", "top"), ("home", "close"), ("top", "home")):
        on, off = await blended(a, b, motion.SPEED_FAST), await blended(a, b, motion.SPEED_FAST, ALIGN=False)
        check(on["turn_peak"] < 0.6 * off["turn_peak"] or off["turn_peak"] < MOVING,
              f"{a} -> {b}: aligned turn is gentler (peak {on['turn_peak']:.0f} vs {off['turn_peak']:.0f} steps/s)")
        check(on["time"] <= off["time"] + 0.15, f"{a} -> {b}: aligning costs no time ({on['time']:.2f} vs {off['time']:.2f} s)")
        check(on["turn_alone"] <= 0.1, f"{a} -> {b}: M1/M5 never turn on their own ({on['turn_alone']:.2f} s)")
    for a, b in (("home", "left"), ("right", "home")):      # a long turn is what the move waits for: left alone
        on, off = await blended(a, b, motion.SPEED_FAST), await blended(a, b, motion.SPEED_FAST, ALIGN=False)
        check(abs(on["time"] - off["time"]) <= 0.15 and on["turn_peak"] > 0.9 * off["turn_peak"],
              f"{a} -> {b}: a turn the move is waiting for is not slowed")

    # --- 2e: no standstill at the hub, quicker descent at fast, short hops ---------------------------------
    on, off = await blended("home", "left", motion.SPEED_FAST), await blended("home", "left", motion.SPEED_FAST, HUB_PASS_THROUGH=False)
    check(on["time"] < off["time"] - 0.2, f"passing through the hub saves time ({on['time']:.2f} vs {off['time']:.2f} s)")
    check(on["handover"] is not None and on["handover"] <= motion.ARRIVE_TOL,
          f"the descent only takes over within ARRIVE_TOL of the hub ({on['handover']:.0f} steps)")
    off = await blended("home", "left", motion.SPEED_FAST, FAST_DESCENT=False)
    check(on["time"] < off["time"] - 0.5, f"fast descent saves time ({on['time']:.2f} vs {off['time']:.2f} s)")
    check(motion._duration(5, 800) == motion.MIN_SEGMENT_S and motion._duration(2000, 800) == 3.75,
          "SHORT_HOPS is off by default: the old minimum segment time")
    motion.SHORT_HOPS = True
    check(motion._duration(5, 800) == 0.2 and motion._duration(60, 800) == 0.45 and motion._duration(2000, 800) == 3.75
          and motion._duration(200, 800) == motion.MIN_SEGMENT_S, "SHORT_HOPS on: short hops take as long as they need at slow speed")
    hop = await blended("home", "top", motion.SPEED_FAST)
    motion.SHORT_HOPS = False
    check(hop["time"] <= (await blended("home", "top", motion.SPEED_FAST))["time"] + 0.05, "...and moves still arrive, no slower")

    # --- joints between segments are watched too -----------------------------------------------------------
    for label, delay, joint in (("before the turn starts", 0.4, 1), ("after the turn has arrived", 5.2, 5)):
        arm = SimArm(start=start_pose("home"), realistic=True)
        await settle(arm)
        task = asyncio.ensure_future(go_to_view(arm, "left", max_speed=motion.SPEED_FAST))
        await asyncio.sleep(delay)
        check(joint not in motion._last["driving"], f"motor {joint} is idle {label}")
        arm.hijack = {joint: arm.joints()[joint] + 200}
        r = await task
        check(not r["ok"] and f"something else is commanding motor {joint}" in r["error"] and r["hold"] is not None,
              f"blended: another writer on an idle joint {label}: {r['error']}")
    arm = SimArm(start=start_pose("home"), realistic=True)
    await settle(arm)
    task = asyncio.ensure_future(go_to_view(arm, "left", max_speed=motion.SPEED_FAST))
    await asyncio.sleep(0.4)
    arm._pos[5] += 150
    arm.stuck = {5}                                           # the forearm roll knocked away and held there
    r = await task
    check(not r["ok"] and "motor 5 was pushed off" in r["error"], f"blended: an idle joint pushed away: {r['error']}")

    # --- one group failing stops them all ---------------------------------------------------------------------
    for joint, word in ((7, "motor 7"), (5, "motor 5"), (3, "motor 3")):
        arm = SimArm(start=start_pose("home"), realistic=True)
        arm.stuck = {joint}
        await settle(arm)
        r = await go_to_view(arm, "side_low", max_speed=motion.SPEED_FAST)
        check(not r["ok"] and word in r["error"] and r["hold"] == "confirmed", f"blended: stuck {word} ends the move: {r['error']}")
        arm.stuck = set()
        check(await still(arm), f"blended: everything holds after {word} failed")

    # --- Stop anywhere in a blended move: holding, recognised, and the next click works -----------------------
    seen = set()
    for speed in (motion.SPEED_FAST, motion.SPEED_TURBO):
        for a, b in (("home", "side_low"), ("left", "home")):
            for delay in (0.5, 1.2, 1.9, 2.6, 3.3, 4.2, 5.0):
                arm = SimArm(start=start_pose(a), realistic=True)
                await settle(arm)
                if not await stop_after(arm, b, speed, delay):
                    continue                                  # the move had already finished
                check(motion.status["hold"] == "confirmed" and await still(arm), f"blended {a}->{b} stopped at {delay} s: holding")
                d = await motion.describe(arm, b)
                where = d["where"]["where"]
                seen.add(where)
                check(d["ok"] and where in ("view", "spoke", "hub", "above"),
                      f"blended {a}->{b} stopped at {delay} s: recognised ({d['where']}, {d['blockers']})")
                onward = b if delay < 3 else a
                r = await go_to_view(arm, onward, max_speed=speed)
                check(r["ok"] and at(arm, onward), f"...and carries on to {onward} from {where}: {r['error']}")
    check({"spoke", "above"} <= seen, f"stops were recognised on a spoke and above the clear height: {sorted(seen)}")
    # with the roll left half-way on a view's own spoke, going back down puts it right first
    arm = SimArm(start=start_pose("home"), realistic=True)
    await settle(arm)
    await stop_after(arm, "side_low", motion.SPEED_FAST, 1.0)
    await still(arm)
    pose = {**arm.joints(), 7: VIEWS["home"][7] - 100}
    check(locate(pose, VIEWS)["where"] == "spoke", "a spoke with the wrist roll elsewhere is still that spoke")
    check([(s, sorted(g)) for s, g in motion.plan_route(pose, "home", VIEWS)] == [("turn", [7]), ("descend", sorted(PITCH))],
          "back down the same spoke: wrist roll first, then the descent")
    check(locate({**pose, 7: VIEWS["home"][7] - 500}, VIEWS)["where"] == "unknown", "...but not with the roll anywhere at all")

    print(f"ALL OK ({passed} checks)")


if __name__ == "__main__":
    t0 = time.perf_counter()
    # Simulated time by default; MOTION_TEST_REALTIME=1 runs the same checks on the real clock (slow).
    virtual_time.run(main(), realtime=bool(os.environ.get("MOTION_TEST_REALTIME")))
    print(f"({time.perf_counter() - t0:.0f} s)")
