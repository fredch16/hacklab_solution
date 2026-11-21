"""capture_server.py on the simulated arm: who may command the arm, and when.

    python3 tests/test_server_sim.py            # needs port 8891 free; takes about four minutes
    python3 tests/test_server_sim.py 8899       # or another port (also: SERVER_TEST_PORT=8899)

Part A drives capture_server.State in this process (no HTTP): goto during a STOP, stale stop
generation, overlapping STOPs sharing one cleanup, STOP not queueing behind a slow command,
Go limp vs goto, unconfirmed holds, shutdown; then the same races on an arm whose registers lag
150 ms behind the commands it accepts (DelayedArm).
Part B starts the real server (`--sim --port 8891`) and hammers it over HTTP from two threads at
the same instant: STOP vs goto, Go limp vs goto, SIGTERM / SIGHUP during a move.
"""
import asyncio
import http.client
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import motion
import capture_server
from capture_server import Refused, State
from sim_arm import SimArm

PORT = int(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SERVER_TEST_PORT", 8891))
motion.LOG_PATH = Path(os.devnull)
passed = 0


def check(cond, what):
    global passed
    assert cond, what
    passed += 1


class DelayedArm(SimArm):
    """Registers lag: a goal write or torque command is accepted at once but takes effect DELAY
    seconds later, like a real bus where the read-back trails the command."""

    DELAY = 0.15

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending = 0                    # accepted, not yet in effect
        self.limp_accepted = False          # the last torque command accepted was "off"
        self.goal_writes_after_limp = 0     # goal writes accepted since then: must stay 0
        self.deaf = False                   # torque-off is accepted but never takes effect

    def _later(self, method, *args):
        self.pending += 1

        def apply():
            self.pending -= 1
            asyncio.ensure_future(method(self, *args))

        asyncio.get_running_loop().call_later(self.DELAY, apply)

    async def set_torque(self, enable, motor_ids=None):
        self.limp_accepted = not enable
        self.goal_writes_after_limp = 0
        if enable or not self.deaf:
            self._later(SimArm.set_torque, enable, motor_ids)

    async def write_goals(self, goals, speed=None, accel=None):
        if self.fail_writes:
            raise RuntimeError("write failed (sim)")
        if self.limp_accepted:
            self.goal_writes_after_limp += 1
        self._later(SimArm.write_goals, dict(goals), speed, accel)

    def registers_match(self, tol=25):
        """Fresh sample: every energised joint's goal register is where the joint is."""
        pos = self.joints()
        return all(abs(self._goal[m] - pos[m]) <= tol for m in motion.MOVABLE if self._torque[m])


def refusal(fn):
    """The refusal message if fn() is refused, else None."""
    try:
        fn()
    except (Refused, motion.MotionError) as e:
        return str(e)
    return None


def comes_to_rest(read, within=1.5, quiet=0.12):
    """True once two readings `quiet` seconds apart are equal. A STOP lets a joint finish the last
    few steps to a goal it had almost reached (motion.hold), so "still" is judged after a moment."""
    deadline, prev = time.monotonic() + within, read()
    while time.monotonic() < deadline:
        time.sleep(quiet)
        cur = read()
        if cur == prev:
            return True
        prev = cur
    return False


def both(*fns):
    """Run the functions on separate threads, released at the same instant. Returns their results."""
    barrier, out = threading.Barrier(len(fns)), [None] * len(fns)

    def run(i, fn):
        barrier.wait()
        time.sleep(random.uniform(0, 0.001))    # so neither side always wins the race
        try:
            out[i] = fn()
        except Exception as e:      # a refusal is a result here, not a failure
            out[i] = e

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return out


# ------------------------------------------------------------------ part A: in-process

def part_a():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    st = State(loop, sim=True)
    st.call(st.start_sim())
    motion.LOG_PATH = Path(os.devnull)

    def fresh_arm():
        check(not st.moving, "idle before the next test")
        st.arm = SimArm()

    def still():
        return comes_to_rest(lambda: st.arm.joints())

    def go(view="top", gen=None):
        return st.call(st.start_move(view, "slow", gen=st.gen if gen is None else gen))

    # --- a goto whose request arrived before a STOP can never start afterwards
    fresh_arm()
    before = st.gen
    check(st.call(st.stop())["ok"], "idle stop holds")
    msg = refusal(lambda: go(gen=before))
    check(msg and "stopped before it started" in msg, f"goto from before the STOP is refused: {msg}")
    check(not st.moving, "and nothing moves")
    msg = refusal(lambda: st.call(st.set_torque(False, gen=before)))
    check(msg and "stopped before it started" in msg and st.arm.torque, "a Go limp from before the STOP is refused too")
    check(go()["started"] and st.moving, "a goto that arrives after the STOP starts")

    # --- while a STOP is in progress nothing else may start
    real_hold = motion.hold

    async def slow_hold(arm, *args, **kwargs):
        await asyncio.sleep(0.4)
        await real_hold(arm, *args, **kwargs)

    time.sleep(0.5)
    motion.hold = slow_hold
    stopper = threading.Thread(target=lambda: results.append(st.call(st.stop())))
    results = []
    stopper.start()
    time.sleep(0.15)
    check(st.stopping, "stop is in progress")
    for what, fn in [("goto", lambda: go()), ("Hold here", lambda: st.call(st.set_torque(True, gen=st.gen))),
                     ("Go limp", lambda: st.call(st.set_torque(False, gen=st.gen))),
                     ("arm switch", lambda: st.call(st.select_bus("SIM", gen=st.gen)))]:
        msg = refusal(fn)
        check(msg and "stopped before it started" in msg, f"{what} during a STOP is refused: {msg}")
    stopper.join(5)
    motion.hold = real_hold
    check(results and results[0]["ok"] and results[0]["was_moving"] and results[0]["hold"] == "confirmed",
          f"the STOP itself completed: {results}")
    check(not st.moving and not st.stopping and still(), "stopped, holding still")
    check(st.snapshot()["motion"]["last_result"]["error"] == "stopped", "state says stopped")

    # --- overlapping STOPs share ONE cleanup; admission stays closed until its hold is confirmed
    fresh_arm()
    go()
    time.sleep(0.5)
    server_holds, real_server_hold = [], st._hold

    async def counted_server_hold(active=False):
        server_holds.append(active)
        return await real_server_hold(active)

    st._hold = counted_server_hold
    motion.hold = slow_hold
    answers, done_at = [], []

    def one_stop():
        answers.append(st.call(st.stop())); done_at.append(time.monotonic())

    stoppers = [threading.Thread(target=one_stop) for _ in range(3)]
    for t in stoppers:
        t.start(); time.sleep(0.12)         # the 2nd and 3rd arrive while the 1st cleanup is running
    check(st.stopping, "cleanup still running after the third STOP was sent")
    msg = refusal(lambda: go("wide"))
    check(msg and "stopped before it started" in msg, f"goto during the shared cleanup is refused: {msg}")
    for t in stoppers:
        t.join(5)
    motion.hold, st._hold = real_hold, real_server_hold
    strip = lambda r: {k: v for k, v in r.items() if k != "generation"}
    check(len(answers) == 3 and all(strip(a) == strip(answers[0]) for a in answers), f"all three got the same result: {answers}")
    check(answers[0]["ok"] and answers[0]["was_moving"] and answers[0]["hold"] == "confirmed", f"...the real one: {answers[0]}")
    check(server_holds == [True], f"one cleanup, one hold (not three), as for an active move: {server_holds}")
    check(max(done_at) - min(done_at) < 0.1, "they all returned when that cleanup finished, none early")
    check(not st.moving and not st.stopping and still(), "stopped, holding still")
    check(go("wide")["started"], "admission reopens once the cleanup is done")
    st.call(st.stop())

    # --- stopping an active move relaxes the joints it was driving; an idle STOP keeps the default
    fresh_arm()
    tolerances = []

    async def spying_hold(arm, *args, **kwargs):
        tolerances.append(kwargs.get("keep_within", args[0] if args else motion.FREEZE_TOL))
        return await real_hold(arm, *args, **kwargs)

    motion.hold = spying_hold
    go()
    time.sleep(0.5)
    st.call(st.stop())
    # The move's own abort stops the driven joints (RELAX_TOL); once that hold is confirmed, the
    # server's follow-up hold is a plain one that leaves resting joints alone (no second sag).
    check(tolerances and tolerances[0] == motion.RELAX_TOL and tolerances[-1] == motion.FREEZE_TOL,
          f"active STOP: the move relaxes its joints, the server's follow-up hold is plain: {tolerances}")
    tolerances.clear()
    st.call(st.stop())
    motion.hold = real_hold
    check(tolerances == [motion.FREEZE_TOL], f"idle STOP uses the default tolerance: {tolerances}")

    # --- repeated idle STOPs are cheap and leave a settled arm alone
    fresh_arm()
    writes, t0 = st.arm.writes, time.monotonic()
    idle = [st.call(st.stop()) for _ in range(10)]
    check(all(r["ok"] and r["hold"] == "confirmed" and not r["was_moving"] for r in idle), "10 idle STOPs all confirmed")
    check(st.arm.writes == writes and time.monotonic() - t0 < 10 * 0.4, f"no write to the settled arm, {time.monotonic() - t0:.2f}s for 10")

    # --- a click from before a STOP is refused by the generation the page had seen
    fresh_arm()
    seen = st.gen
    st.call(st.stop())
    msg = refusal(lambda: st.call(st.start_move("top", "slow", gen=st.gen, click=seen)))
    check(msg and "stopped since this page last updated" in msg and not st.moving, f"stale click refused: {msg}")
    msg = refusal(lambda: st.call(st.set_torque(False, gen=st.gen, click=seen)))
    check(msg and "stopped since this page last updated" in msg and st.arm.torque, f"stale Go limp refused: {msg}")
    check(st.call(st.start_move("top", "slow", gen=st.gen, click=st.gen))["started"], "a click made after the STOP starts")
    st.call(st.stop())

    # --- torque and arm selection are refused while a move is alive
    fresh_arm()
    go()
    for what, fn in [("Hold here", lambda: st.call(st.set_torque(True, gen=st.gen))),
                     ("Go limp", lambda: st.call(st.set_torque(False, gen=st.gen))),
                     ("arm switch", lambda: st.call(st.select_bus("SIM", gen=st.gen))),
                     ("second goto", lambda: go("wide"))]:
        msg = refusal(fn)
        check(msg and "press STOP first" in msg, f"{what} while moving is refused: {msg}")
    check(st.arm.torque and st.moving, "the move carries on, arm still holding")
    st.call(st.stop())

    # --- STOP does not queue behind a slow command; it takes the arm away from it
    fresh_arm()
    holder = []

    async def slow_body():
        await asyncio.sleep(3)

    t = threading.Thread(target=lambda: holder.append(refusal(lambda: st.call(st.command(slow_body)))))
    t.start()
    time.sleep(0.15)
    check(st.command_task is not None, "the slow command owns the arm")
    t0 = time.monotonic()
    r = st.call(st.stop())
    took = time.monotonic() - t0
    t.join(5)
    check(r["ok"] and took < 0.7, f"STOP answered in {took:.2f}s while a 3 s command was running")
    check(holder and holder[0] and "interrupted by STOP" in holder[0], f"the slow command was interrupted: {holder}")
    check(st.command_task is None and not st.owner.locked(), "ownership is free again")

    # --- a Hold here / Go limp in progress is not cut in half: STOP waits a bounded moment for it
    real_engage = motion.engage

    async def slow_engage(arm):
        await asyncio.sleep(0.4)
        await real_engage(arm)

    motion.engage = slow_engage
    holder = []
    t = threading.Thread(target=lambda: holder.append(refusal(lambda: st.call(st.set_torque(True, gen=st.gen)))))
    t.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    r = st.call(st.stop())
    took = time.monotonic() - t0
    t.join(5)
    check(r["ok"] and 0.25 < took < 1.2 and holder == [None], f"STOP let the 0.4 s Hold here finish ({took:.2f}s): {holder}")

    async def stuck_engage(arm):
        await asyncio.sleep(30)

    motion.engage = stuck_engage
    holder = []
    t = threading.Thread(target=lambda: holder.append(refusal(lambda: st.call(st.set_torque(True, gen=st.gen)))))
    t.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    r = st.call(st.stop())
    took = time.monotonic() - t0
    t.join(5)
    motion.engage = real_engage
    check(r["ok"] and took < capture_server.TORQUE_WAIT_S + 0.8, f"...but not for ever: a stuck one is cut off after {took:.2f}s")
    check(holder and holder[0] and "interrupted by STOP" in holder[0] and not st.owner.locked(), f"and it was interrupted: {holder}")

    # --- commands run one at a time
    order = []

    def cmd(name):
        async def body():
            order.append(name + " in"); await asyncio.sleep(0.05); order.append(name + " out")
        return lambda: st.call(st.command(body))

    both(cmd("a"), cmd("b"), cmd("c"))
    check(all(order[i][0] == order[i + 1][0] for i in range(0, 6, 2)) and len(order) == 6, f"no interleaving: {order}")

    # --- Go limp vs goto at the same instant: a move never runs on (or re-energises) a limp arm
    limp_won = move_won = 0
    for i in range(40):
        fresh_arm()
        gen = st.gen
        released, started = both(lambda: st.call(st.set_torque(False, gen=gen)),
                                 lambda: st.call(st.start_move("top", "slow", gen=gen)))
        time.sleep(0.25)
        if not isinstance(released, Exception):     # Go limp went through
            limp_won += 1
            writes = st.arm.writes
            time.sleep(0.15)
            check(not st.moving and not st.arm.torque and st.arm.writes == writes and still(),
                  f"#{i}: arm is limp, so nothing moves or writes (goto: {started})")
        else:
            move_won += 1
            check("press STOP first" in str(released) and st.arm.torque, f"#{i}: Go limp refused while moving: {released}")
            st.call(st.stop())
    print(f"  Go limp vs goto x40: limp first {limp_won}, move first {move_won}")

    # --- STOP vs goto at the same instant, on the loop directly
    fresh_arm()
    for i in range(60):
        gen = st.gen
        both(lambda: st.call(st.stop()), lambda: st.call(st.start_move(("top", "wide")[i % 2], "slow", gen=gen)))
        check(not st.moving and not st.stopping, f"#{i}: nothing is moving after STOP")
    check(still(), "and the arm is still")

    # --- a hold that cannot be confirmed is reported, loudly and persistently
    fresh_arm()
    go()
    time.sleep(0.6)

    async def lose_the_arm_then_stop():     # in one step on the loop, so the move cannot notice first
        st.arm.stale = True
        return await st.stop()

    r = st.call(lose_the_arm_then_stop())
    check(not r["ok"] and r["hold"] == "unconfirmed" and r["was_moving"], f"STOP reports the unconfirmed hold: {r}")
    snap = st.snapshot()
    check(snap["hold"] == "unconfirmed" and snap["motion"]["hold"] == "unconfirmed", "state carries it")
    check("HOLD NOT CONFIRMED" in snap["motion"]["last_result"]["error"], f"and the result says so: {snap['motion']['last_result']}")
    check(not st.moving, "the move task is gone all the same")
    st.arm.stale = False
    r = st.call(st.stop())
    check(r["ok"] and r["hold"] == "confirmed" and st.snapshot()["hold"] == "confirmed", "a later STOP confirms the hold")
    st.arm.stale = True
    r = st.call(st.stop())
    check(not r["ok"] and r["hold"] == "unconfirmed" and st.snapshot()["hold"] == "unconfirmed", "idle STOP with stale data")
    st.arm.stale = False
    st.call(st.stop())

    # --- where the arm is, also while limp; blockers say why a move cannot start
    fresh_arm()
    st.call(st.set_torque(False, gen=st.gen))
    writes = st.arm.writes
    r = st.call(st.stop())
    check(r["ok"] and r["hold"] == "limp" and st.snapshot()["hold"] == "limp", f"STOP on a limp arm: nothing to hold, said so: {r}")
    check(not st.arm.torque and st.arm.writes == writes, "and it did not energise or write to the limp arm")
    time.sleep(0.3)
    st.snapshot(); time.sleep(0.2)
    snap = st.snapshot()
    check(snap["where"] == {"where": "view", "view": "home"}, f"limp arm is still located: {snap['where']}")
    check(snap["can_move"] is False and any("limp" in b for b in snap["blockers"]), f"blockers: {snap['blockers']}")
    st.call(st.set_torque(True, gen=st.gen))
    time.sleep(0.3)
    st.snapshot(); time.sleep(0.2)
    snap = st.snapshot()
    check(snap["can_move"] is True and snap["blockers"] == [] and snap["torque"] == "holding", "after Hold here a move can start")

    # --- a Go limp that cannot be confirmed keeps move starts blocked until something re-checks the arm
    fresh_arm()
    real_release = motion.release

    async def unconfirmed_release(arm):
        raise motion.MotionError("could not confirm the arm is limp")

    motion.release = unconfirmed_release
    msg = refusal(lambda: st.call(st.set_torque(False, gen=st.gen)))
    motion.release = real_release
    check(msg and "could not confirm the arm is limp" in msg, f"Go limp reports it: {msg}")
    snap = st.snapshot()
    check(snap["can_move"] is not True and any("could not confirm the arm is limp" in b for b in snap["blockers"]),
          f"state carries a blocker: {snap['blockers']}")
    msg = refusal(lambda: go())
    check(msg and "could not confirm the arm is limp" in msg and not st.moving, f"no move starts meanwhile: {msg}")
    check(st.call(st.stop())["hold"] == "confirmed" and st.torque_doubt is None, "a confirmed STOP clears it")
    check(go()["started"], "and then a move may start")
    st.call(st.stop())

    # --- the same races on an arm whose registers lag 150 ms behind the commands it accepts
    def delayed_arm():
        check(not st.moving, "idle before the next test")
        st.arm = DelayedArm()
        return st.arm

    arm = delayed_arm()
    r = go()
    time.sleep(1.5)
    check(st.moving and st.snapshot()["motion"]["phase"] == "rise", f"a move runs on the delayed arm: {st.snapshot()['motion']}")
    r = st.call(st.stop())
    check(r["ok"] and r["hold"] == "confirmed" and arm.pending == 0, f"STOP confirmed only once nothing was pending: {r} pending={arm.pending}")
    check(still() and arm.registers_match(), "goal registers are where the arm is")

    for i in range(12):     # overlapping STOPs, and a goto fired into the middle of them
        if i % 4 == 0:
            arm = delayed_arm()
        go(("top", "wide", "left")[i % 3])
        time.sleep(random.uniform(0.3, 1.0))
        out = []

        def late_stop():
            time.sleep(0.07)
            return st.call(st.stop())

        def late_goto():
            time.sleep(0.1)
            return st.call(st.start_move("close", "slow", gen=st.gen))

        out = both(lambda: st.call(st.stop()), lambda: st.call(st.stop()), late_stop, late_goto)
        stops, late = out[:3], out[3]
        check(all(isinstance(r, dict) and r["ok"] and r["hold"] == "confirmed" for r in stops), f"delayed #{i}: every STOP confirmed: {stops}")
        check(isinstance(late, Refused), f"delayed #{i}: the goto fired during the cleanup was refused: {late}")
        check(arm.pending == 0, f"delayed #{i}: no hold write was still pending when STOP returned ({arm.pending})")
        check(not st.moving and not st.stopping, f"delayed #{i}: nothing was admitted in between")
        check(still() and arm.registers_match(), f"delayed #{i}: on a fresh sample the goal registers match the positions")
        check(not st.moving, f"delayed #{i}: still nothing moving")

    limp_won = move_won = 0
    for i in range(16):     # Go limp vs goto: once Go limp is accepted, no goal write is
        arm = delayed_arm()
        gen = st.gen
        released, started = both(lambda: st.call(st.set_torque(False, gen=gen)),
                                 lambda: st.call(st.start_move("top", "slow", gen=gen)))
        time.sleep(0.5)
        if not isinstance(released, Exception):
            limp_won += 1
            check(arm.goal_writes_after_limp == 0, f"delayed limp #{i}: {arm.goal_writes_after_limp} goal write(s) accepted after Go limp")
            check(not st.moving and not arm.torque and arm.pending == 0, f"delayed limp #{i}: limp and quiet (goto: {started})")
        else:
            move_won += 1
            check("press STOP first" in str(released) and not arm.limp_accepted, f"delayed limp #{i}: Go limp refused, never sent: {released}")
            st.call(st.stop())
    print(f"  delayed registers: 12 rounds of overlapping STOPs; Go limp vs goto x16: limp first {limp_won}, move first {move_won}")

    arm = delayed_arm()     # the arm never confirms it went limp
    arm.deaf = True
    msg = refusal(lambda: st.call(st.set_torque(False, gen=st.gen)))
    check(msg and "could not confirm the arm is limp" in msg and st.torque_doubt, f"deaf arm: Go limp is reported unconfirmed: {msg}")
    msg = refusal(lambda: go())
    check(msg and "could not confirm the arm is limp" in msg and arm.goal_writes_after_limp == 0, f"deaf arm: no move, no write: {msg}")
    arm.deaf = False
    st.call(st.set_torque(True, gen=st.gen))
    check(st.torque_doubt is None and arm.torque, "a confirmed Hold here clears it")
    st.arm = SimArm()

    # --- the control centre: status strip advice, unlink, settings, joint table, arm picture, history
    def look():
        """A fresh look at the arm (motion.describe), then the state the page would get."""
        async def again():
            st._forget_where()
            await st._describe()
        st.call(again())
        return st.snapshot()

    fresh_arm()
    snap = look()
    check(snap["advice"]["code"] == "ready" and snap["advice"]["action"] is None, f"ready: {snap['advice']}")
    check(snap["data"]["fresh"] and snap["link"]["linked"] is False, f"data fresh, no leader linked: {snap['data']} {snap['link']}")
    check(set(snap["speed_options"]) == {"slow", "normal", "fast", "turbo"} and all(k in snap["speed_about"] for k in snap["speed_options"]),
          "every speed has a plain description")
    check(snap["joints"].startswith("j1=") and "gripper=" in snap["joints"], "the joints string other tools parse is unchanged")

    st.arm.mirror = "5B61033001"                            # Station still links the leader
    snap = look()
    check(snap["link"] == {"linked": True, "leader": "5B61033001", "text": snap["link"]["text"]} and snap["can_move"] is False,
          f"linked to a leader: {snap['link']}")
    check(snap["advice"]["code"] == "linked" and snap["advice"]["action"] == "unlink", f"advice says unlink: {snap['advice']}")
    r = st.call(st.unlink())
    check(r == {"ok": True, "linked": False} and st.arm.mirror is None, f"unlink: {r}")
    snap = look()
    check(snap["link"]["linked"] is False and snap["advice"]["code"] == "ready", f"after unlink: {snap['link']} {snap['advice']}")

    st.call(st.set_torque(False, gen=st.gen))               # limp: the next step is Hold here
    snap = look()
    check(snap["advice"]["code"] == "limp" and snap["advice"]["action"] == "hold" and '"home"' in snap["advice"]["text"],
          f"limp advice: {snap['advice']}")
    st.call(st.set_torque(True, gen=st.gen))

    st.arm.stale = True                                     # arm data stops: no override, a clear message
    snap = st.snapshot()
    check(not snap["data"]["fresh"] and snap["data"]["since_clock"] and snap["joints"] == "no data", f"stale data: {snap['data']}")
    time.sleep(1.1)
    snap = st.snapshot()
    check(snap["advice"]["code"] == "stale" and snap["advice"]["action"] is None and "ago" in snap["advice"]["text"]
          and snap["data"]["age_s"] >= 1.0, f"stale advice with its age: {snap['advice']} {snap['data']}")
    msg = refusal(lambda: go())
    check(msg and "no arm data" in msg and "USB" in msg and not st.moving, f"goto with no arm data is refused with what to do: {msg}")
    msg = refusal(lambda: st.call(st.set_torque(True, gen=st.gen)))
    check(msg and "no arm data" in msg, f"Hold here with no arm data: {msg}")
    check(st.telemetry()["ok"] is False and st.pose()["ok"] is False and st.pose().get("available") is True, "joint table and picture say so too")
    st.arm.stale = False
    snap = look()
    check(snap["data"]["fresh"] and snap["data"]["back_clock"] and snap["advice"]["code"] == "ready", f"data back: {snap['data']}")
    st.call(st.set_torque(False, gen=st.gen))
    snap = look()
    check(snap["advice"]["code"] == "limp" and "is back" in snap["advice"]["text"], f"limp after coming back says so: {snap['advice']}")
    st.call(st.set_torque(True, gen=st.gen))

    fresh_arm()                                             # joint table
    st.arm.load = {2: 700}
    t = st.telemetry()
    check(t["ok"] and [j["id"] for j in t["joints"]] == list(range(1, 9)), "telemetry lists joints 1-8")
    row = t["joints"][1]
    check(row["warn"] == ["load"] and row["holding"] and row["goal_error"] == 0 and {"pos", "current_a", "temp", "voltage"} <= set(row),
          f"joint 2 with a high load is flagged: {row}")
    check(all(j["warn"] == [] for j in t["joints"] if j["id"] != 2), "the others are not")
    st.arm.load = {}

    pose = st.pose()                                        # arm picture
    check(pose["ok"] and len(pose["chain"]) == 9 and len(pose["camera"]) == 3 and "transit" in pose["views"]
          and pose["clear_height_m"] == motion.CLEAR_HEIGHT_M and pose["lowest_m"] > 0, f"pose: { {k: pose[k] for k in ('ok', 'lowest_m', 'clearance_m')} }")

    defaults = dict(capture_server.SETTING_DEFAULTS)        # motion settings
    for bad in ({"BLEND": "yes"}, {"BLEND": 1}, {"CLEAR_HEIGHT_M": 0.1}, {"CLEAR_HEIGHT_M": 0.41}, {"CLEAR_HEIGHT_M": True},
                {"SPEED_TURBO": 799}, {"SPEED_TURBO": 3401}, {"SPEED_TURBO": 900.5}, {"SPEED_TURBO": "fast"}, {"NOPE": 1}, {},
                {"BLEND": False, "SPEED_TURBO": 5000}):
        try:
            capture_server.check_settings(bad)
            check(False, f"settings {bad} must be refused")
        except Refused as e:
            check(e.status == 400, f"settings {bad}: {e}")
    check({k: getattr(motion, k) for k in defaults} == defaults, "a refused change changes nothing")
    r = st.call(st.change_settings(capture_server.check_settings({"BLEND": False, "CLEAR_HEIGHT_M": 0.3, "SPEED_TURBO": 1200})))
    check(r["ok"] and (motion.BLEND, motion.CLEAR_HEIGHT_M, motion.SPEED_TURBO) == (False, 0.3, 1200), f"settings applied: {r['changed']}")
    check(capture_server.SPEEDS["turbo"] == 1200 and st.snapshot()["settings_changed"] and st.snapshot()["settings"]["SPEED_TURBO"] == 1200,
          "the Turbo button follows, and state says the settings differ from the defaults")
    go()
    msg = refusal(lambda: st.call(st.change_settings(capture_server.check_settings({"defaults": True}))))
    check(msg and "press STOP first" in msg and motion.SPEED_TURBO == 1200, f"settings are locked while moving: {msg}")
    msg = refusal(lambda: st.call(st.unlink()))
    check(msg and "press STOP first" in msg, f"so is unlink: {msg}")
    check(st.snapshot()["advice"]["code"] == "moving", "advice while moving")
    st.call(st.stop())
    r = st.call(st.change_settings(capture_server.check_settings({"defaults": True})))
    check({k: getattr(motion, k) for k in defaults} == defaults and capture_server.SPEEDS["turbo"] == defaults["SPEED_TURBO"]
          and not st.snapshot()["settings_changed"], "back to the defaults")

    h = st.moves()                                          # history and the time on the last result
    last = st.snapshot()["motion"]["last_result"]
    check(h["ok"] and h["source"] == "memory" and h["moves"] and h["moves"][0]["view"] == "top" and h["moves"][0]["error"] == "stopped"
          and h["moves"][0]["asked"] == "slow", f"history, newest first: {h['moves'][:1]}")
    check(all(a["t"] >= b["t"] for a, b in zip(h["moves"], h["moves"][1:])), "history is newest first")
    check(last["error"] == "stopped" and abs(last["at"] - time.time()) < 5 and last["at_clock"], f"the last result carries its time: {last}")

    was_checked = motion.view_checked                       # the speed a view will really run at
    motion.view_checked = lambda name: name != "left"
    try:
        check("not marked checked" in State.first_run_reason("left", {"where": "view", "view": "home"}), "an unchecked target runs at first-run speed")
        check('"left"' in State.first_run_reason("top", {"where": "view", "view": "left"}), "so does a move that starts from an unchecked view")
        check(State.first_run_reason("top", {"where": "view", "view": "home"}) is None, "checked to checked runs at the picked speed")
        check(State.first_run_reason(motion.TRANSIT, {"where": "hub"}) and State.first_run_reason(motion.TRANSIT, {"where": "view", "view": "top"}) is None,
              "to transit: only at the picked speed when it starts from a checked view")
    finally:
        motion.view_checked = was_checked
    fresh_arm()

    # --- which arm: never guess between several
    pick, T = capture_server.pick_bus, {"transit": {"bus": "F"}, "top": {"bus": "F"}}

    def refused_bus(*args):
        try:
            return pick(*args) and None
        except RuntimeError as e:
            return str(e)

    check(pick("auto", ["F"], [], {}) == "F", "one arm: that one")
    check(pick("auto", ["L", "F"], [], T) == "F", "two limp arms: the one the views were captured on")
    check(pick("auto", ["L", "F"], ["F"], T) == "F" and pick("auto", ["L", "F"], ["L"], {}) == "L", "torque decides when nothing contradicts it")
    check(pick("auto", ["L", "F"], [], {"top": {"bus": "F"}}) == "F", "no transit yet: any view's arm")
    msg = refused_bus("auto", ["L", "F"], [], {})
    check(msg and "L, F" in msg and "--bus" in msg, f"two limp arms, no views: refuse and list them: {msg}")
    msg = refused_bus("auto", ["L", "F"], ["L"], T)
    check(msg and "--bus" in msg, f"torque and views disagree: refuse: {msg}")
    check(refused_bus("auto", ["L", "F"], [], {"a": {"bus": "L"}, "b": {"bus": "F"}}), "views from both arms: refuse")
    check(refused_bus("X", ["L", "F"], [], T) and pick("L", ["L", "F"], [], T) == "L", "--bus is obeyed, or refused when absent")
    check(refused_bus("auto", [], [], T), "no arm at all")

    # --- arm selection goes through the owner and re-asks where the arm is
    fresh_arm()
    time.sleep(0.3); st.snapshot(); time.sleep(0.2)
    check(st.snapshot()["where"] is not None, "where is known")
    msg = refusal(lambda: st.call(st.select_bus("NOPE", gen=st.gen)))
    check(msg and "not connected" in msg and st.arm.bus_serial == "SIM", f"unknown arm refused: {msg}")
    check(st.call(st.select_bus("SIM", gen=st.gen))["bus"] == "SIM", "arm selected")
    time.sleep(0.2)
    check(st.snapshot()["where"] == {"where": "view", "view": "home"}, "describe() was refreshed after the switch")

    # --- views.json cannot change under a running or stopping move
    # (sim refuses view edits outright, so borrow the real-arm path; the writers are stubbed out
    #  so that a missing guard fails this test instead of touching the real views.json)
    touched = []
    real = capture_server.save_views, capture_server.capture, motion.mark_checked
    capture_server.save_views = lambda *a, **k: touched.append("save")
    motion.mark_checked = lambda *a, **k: touched.append("mark")

    async def no_capture(*a, **k):
        touched.append("capture")
        return True

    capture_server.capture = no_capture
    st.sim = False
    try:
        go()
        edits = [("delete", lambda: st.call(st.delete_view("zz_not_a_view"))),
                 ("capture", lambda: st.call(st.do_capture("zz_not_a_view", ""))),
                 ("mark checked", lambda: st.call(st.mark_checked("top", True)))]
        for what, fn in edits:
            msg = refusal(fn)
            check(msg and "press STOP first" in msg, f"{what} while moving is refused: {msg}")
        motion.hold = slow_hold
        stopper = threading.Thread(target=lambda: st.call(st.stop()))
        stopper.start()
        time.sleep(0.15)
        for what, fn in edits:
            msg = refusal(fn)
            check(msg and "stopped before it started" in msg, f"{what} during a STOP is refused: {msg}")
        stopper.join(5)
        check(touched == [], f"views.json was not touched: {touched}")
    finally:
        motion.hold = real_hold
        st.sim = True
        capture_server.save_views, capture_server.capture, motion.mark_checked = real

    # --- shutdown: refuse new work first, then stop and drain the move
    fresh_arm()
    go()
    time.sleep(0.5)
    out = both(lambda: st.call(st.shutdown()), lambda: refusal(lambda: go("wide")))
    check(isinstance(out[0], dict) and out[0]["was_moving"] and out[0]["hold"] == "confirmed", f"shutdown stopped the move: {out[0]}")
    check(out[1] is not None, f"a goto racing the shutdown is refused: {out[1]}")
    check(st.closing and not st.moving and still(), "drained, holding still")
    msg = refusal(lambda: go())
    check(msg and "shutting down" in msg, f"no new work after shutdown: {msg}")
    check(st.call(st.stop())["ok"], "STOP still works while shutting down")
    closed = []
    st.closing, st.arm.close = False, lambda: closed.append(True)
    check(st.call(st.shutdown()) is None and closed == [True], "shutdown closes the arm connection, also when idle")


# ------------------------------------------------------------------ part B: the real server over HTTP

def connect():
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.connect()
    return c


def send(conn, method, path, body=None):
    conn.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read() or b"{}")
    conn.close()
    return r.status, data


def post(path, body=None):
    return send(connect(), "POST", path, body or {})


def state():
    return send(connect(), "GET", "/api/state")[1]


def click(path, body):
    """A page click: carries the generation the page last saw in /api/state."""
    return post(path, {**body, "generation": state()["generation"]})


def start_server():
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit(f"port {PORT} is in use: stop whatever runs there first")
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    code = ("import os, sys, pathlib, motion, capture_server; motion.LOG_PATH = pathlib.Path(os.devnull); "
            f"sys.argv = ['capture_server.py', '--sim', '--port', '{PORT}']; capture_server.main()")
    proc = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        time.sleep(0.1)
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return proc, log
        if proc.poll() is not None:
            break
    raise SystemExit(f"server did not start: {Path(log.name).read_text()}")


def shutdown_during_move(proc, log, sig, views):
    """A signal during a move, with gotos still arriving: stop, hold, exit; nothing new starts."""
    r = click("/api/goto", {"view": views[0]})
    check(r[0] == 200, f"move before {sig.name}: {r}")
    time.sleep(0.8)
    seen = state()
    check(seen["motion"]["moving"], f"moving before {sig.name}")
    late = []

    def pester():
        for _ in range(30):
            try:
                late.append(post("/api/goto", {"view": views[1], "generation": seen["generation"]})[0])
            except OSError:
                late.append("closed")
            time.sleep(0.01)

    t = threading.Thread(target=pester)
    t.start()
    time.sleep(0.05)
    proc.send_signal(sig)
    code = proc.wait(10)
    t.join(10)
    out = Path(log.name).read_text()
    check(code == 0, f"{sig.name}: server exited cleanly ({code}):\n{out}")
    check("stopped what was running" in out and "'was_moving': True" in out and "'hold': 'confirmed'" in out,
          f"{sig.name}: shutdown stopped and held the move:\n{out}")
    check(200 not in late, f"{sig.name}: no goto started during shutdown: {late}")
    check("Traceback" not in out, f"{sig.name}: no tracebacks in the server log:\n{out}")
    print(f"  {sig.name} during a move: stopped, held, exited; late gotos answered {sorted(set(map(str, late)))}")


def part_b():
    proc, log = start_server()
    try:
        views = [c["name"] for c in state()["cards"]]
        check(len(views) >= 3, "sim server has views")

        # --- every click must say which stop generation the page had seen
        r = post("/api/goto", {"view": views[0]})
        check(r[0] == 400 and "generation" in r[1]["error"], f"goto without a generation is refused: {r}")
        r = post("/api/torque", {"on": False})
        check(r[0] == 400 and state()["torque"] == "holding", f"Go limp without a generation is refused: {r}")
        seen = state()["generation"]
        post("/api/stop")
        r = post("/api/goto", {"view": views[0], "generation": seen})
        check(r[0] == 409 and "stopped since this page last updated" in r[1]["error"], f"a click from before the STOP: {r}")
        r = post("/api/torque", {"on": False, "generation": seen})
        check(r[0] == 409 and state()["torque"] == "holding", f"a Go limp from before the STOP: {r}")
        check(not state()["motion"]["moving"], "nothing started")
        r = click("/api/goto", {"view": views[0]})
        check(r[0] == 200 and state()["motion"]["moving"], f"a click made after the STOP starts: {r}")

        # --- overlapping STOPs over HTTP share one cleanup
        time.sleep(0.5)
        stops = both(*[lambda: post("/api/stop")] * 4)
        check(all(r[0] == 200 and r[1]["hold"] == "confirmed" for r in stops), f"4 STOPs at once: {stops}")
        check(sum(r[1]["was_moving"] for r in stops) >= 1, "at least one of them stopped the move")

        def assert_stopped(i, what):
            s1 = state()
            check(not s1["motion"]["moving"] and not s1["stopping"], f"{what} #{i}: nothing is moving after STOP")
            check(comes_to_rest(lambda: state()["joints"]), f"{what} #{i}: the arm is still")
            check(not state()["motion"]["moving"], f"{what} #{i}: and still not moving")

        assert_stopped(0, "4 STOPs")

        # --- STOP vs a goto that is already on its way (connection open) when STOP is sent
        tally = {"started then stopped": 0, "refused": 0}
        for i in range(100):
            seen = state()["generation"]
            goto_conn, stop_conn = connect(), connect()
            time.sleep(0.02)        # both accepted: the goto is "in flight"
            body = {"view": views[i % len(views)], "speed": "normal", "generation": seen}
            g, s = both(lambda: send(goto_conn, "POST", "/api/goto", body),
                        lambda: send(stop_conn, "POST", "/api/stop", {}))
            check(not isinstance(g, Exception) and not isinstance(s, Exception), f"in flight #{i}: both answered: {g} {s}")
            check(s[0] == 200 and s[1]["hold"] == "confirmed", f"in flight #{i}: STOP held: {s}")
            check(g[0] in (200, 409), f"in flight #{i}: goto answered {g}")
            tally["started then stopped" if g[0] == 200 else "refused"] += 1
            assert_stopped(i, "in flight")
        print(f"  STOP vs in-flight goto x100: {tally}")

        # --- STOP vs goto, both connecting at the same instant. The click carries the generation from
        #     before the STOP, so whichever reaches the server first, no move may be left running
        tally = {"started then stopped": 0, "refused": 0}
        for i in range(100):
            body = {"view": views[i % len(views)], "speed": "normal", "generation": state()["generation"]}
            g, s = both(lambda: post("/api/goto", body), lambda: post("/api/stop"))
            check(s[0] == 200 and g[0] in (200, 409), f"same instant #{i}: {g} {s}")
            tally["started then stopped" if g[0] == 200 else "refused"] += 1
            assert_stopped(i, "same instant")
        print(f"  STOP vs goto at the same instant x100: {tally}")

        # --- Go limp vs goto at the same instant
        tally = {"limp first": 0, "move first": 0}
        click("/api/goto", {"view": "transit", "recover": True})
        for _ in range(300):
            time.sleep(0.2)
            if not state()["motion"]["moving"]:
                break
        for i in range(40):
            post("/api/stop")
            r = click("/api/torque", {"on": True})
            check(r[0] == 200, f"limp #{i}: Hold here: {r}")
            seen = state()["generation"]
            rel, g = both(lambda: post("/api/torque", {"on": False, "generation": seen}),
                          lambda: post("/api/goto", {"view": views[i % len(views)], "generation": seen}))
            time.sleep(0.3)
            s1 = state()
            if rel[0] == 200:
                tally["limp first"] += 1
                check(s1["torque"] == "limp" and not s1["motion"]["moving"], f"limp #{i}: limp arm, nothing moves: {g} {s1['motion']}")
            else:
                tally["move first"] += 1
                check(rel[0] == 409 and s1["torque"] == "holding", f"limp #{i}: Go limp refused while moving: {rel}")
        print(f"  Go limp vs goto over HTTP x40: {tally}")
        post("/api/stop")
        check(click("/api/torque", {"on": True})[0] == 200, "holding again")

        # --- the control centre's endpoints over HTTP
        def get(path):
            return send(connect(), "GET", path)

        r = get("/api/telemetry")
        check(r[0] == 200 and r[1]["ok"] and len(r[1]["joints"]) == 8 and r[1]["warn_at"]["temp"] == 55, f"GET /api/telemetry: {r[0]}")
        r = get("/api/pose")
        check(r[0] == 200 and r[1]["ok"] and len(r[1]["chain"]) == 9, f"GET /api/pose: {r[0]}")
        r = get("/api/history")
        check(r[0] == 200 and r[1]["ok"] and r[1]["source"] == "memory" and len(r[1]["moves"]) >= 5, f"GET /api/history: {r[0]} {len(r[1]['moves'])}")
        r = get("/api/extras")
        check(r[0] == 200 and isinstance(r[1]["mcp"]["up"], bool), f"GET /api/extras: {r}")
        r = get("/api/settings")
        check(r[0] == 200 and set(r[1]["settings"]) == {"DIRECT", "ARC", "IGNORE_OVERLOAD", "BLEND", "CLEAR_HEIGHT_M", "SPEED_TURBO"} and r[1]["settings"]["SPEED_TURBO"]["min"] == 1000,
              f"GET /api/settings: {r[0]}")
        for bad in ({"SPEED_TURBO": 100}, {"CLEAR_HEIGHT_M": 2}, {"BLEND": "on"}, {"UNKNOWN": 1}, {}):
            r = post("/api/settings", bad) if bad else send(connect(), "POST", "/api/settings", {})
            check(r[0] == 400, f"POST /api/settings {bad} is refused: {r}")
        r = post("/api/settings", {"SPEED_TURBO": 1500, "BLEND": False})
        check(r[0] == 200 and r[1]["settings"]["SPEED_TURBO"]["value"] == 1500 and r[1]["settings"]["BLEND"]["value"] is False, f"POST /api/settings: {r[0]}")
        s1 = state()
        check(s1["settings"] == {"DIRECT": motion.DIRECT, "ARC": motion.ARC, "BLEND": False, "CLEAR_HEIGHT_M": 0.25,
                                  "SPEED_TURBO": 1500} and s1["speed_options"]["turbo"] == 1500
              and s1["settings_changed"], f"state shows the settings: {s1['settings']}")
        check(click("/api/goto", {"view": views[0]})[0] == 200, "a move starts")
        r = post("/api/settings", {"defaults": True})
        check(r[0] == 409 and state()["settings"]["SPEED_TURBO"] == 1500, f"settings are refused while moving: {r}")
        post("/api/stop")
        r = post("/api/settings", {"defaults": True})
        check(r[0] == 200 and not state()["settings_changed"], f"defaults: {r[0]}")
        post("/api/sim", {"mirror": "5B61033001"})
        time.sleep(1.5); state(); time.sleep(0.3)
        s1 = state()
        check(s1["link"]["linked"] is True and s1["advice"]["action"] == "unlink", f"linked: {s1['link']} {s1['advice']['code']}")
        r = click("/api/unlink", {})
        check(r[0] == 200 and r[1]["linked"] is False, f"POST /api/unlink: {r}")
        time.sleep(0.3); state(); time.sleep(0.3)
        check(state()["link"]["linked"] is False, "no longer linked")
        r = post("/api/unlink", {"generation": -1})
        check(r[0] == 409, f"unlink with a stale generation is refused: {r}")
        post("/api/sim", {"stale": True})
        time.sleep(1.2)
        s1 = state()
        check(s1["advice"]["code"] == "stale" and not s1["data"]["fresh"] and s1["data"]["age_s"] >= 1, f"stale over HTTP: {s1['advice']['code']}")
        r = click("/api/goto", {"view": views[0]})
        check(r[0] == 503 and "no arm data" in r[1]["error"], f"goto with no arm data: {r}")
        check(get("/api/telemetry")[1]["ok"] is False and get("/api/pose")[1]["ok"] is False, "no joint table, no picture")
        post("/api/sim", {"stale": False})
        time.sleep(0.8)
        check(state()["data"]["fresh"] and state()["data"]["back_clock"], "data is back, and state says since when")

        # --- /api/state keeps the fields other tools read
        j = state()["joints"]
        check(j.startswith("j1=") and "  gripper=" in j and len(j.split()) == 8, f"joints string format: {j!r}")

        # --- an unconfirmed hold over HTTP
        post("/api/sim", {"stale": True})
        r = post("/api/stop")
        check(r[0] == 500 and r[1]["hold"] == "unconfirmed" and state()["hold"] == "unconfirmed", f"unconfirmed hold: {r}")
        post("/api/sim", {"stale": False})
        r = post("/api/stop")
        check(r[0] == 200 and r[1]["hold"] == "confirmed" and state()["hold"] == "confirmed", f"confirmed again: {r}")

        shutdown_during_move(proc, log, signal.SIGTERM, views)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.unlink(log.name)

    proc, log = start_server()      # closing the terminal (SIGHUP) must end the same way
    try:
        shutdown_during_move(proc, log, signal.SIGHUP, [c["name"] for c in state()["cards"]])
    finally:
        if proc.poll() is None:
            proc.kill()
        os.unlink(log.name)


if __name__ == "__main__":
    t0 = time.monotonic()
    part_a()
    print(f"part A (in-process) ok: {passed} checks")
    part_b()
    print(f"ALL OK ({passed} checks, {time.monotonic() - t0:.0f}s)")
