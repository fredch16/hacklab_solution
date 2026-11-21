"""capture_server.py on the simulated arm: who may command the arm, and when.

    python3 tests/test_server_sim.py            # needs port 8891 free; takes about two minutes
    python3 tests/test_server_sim.py 8899       # or another port (also: SERVER_TEST_PORT=8899)

Part A drives capture_server.State in this process (no HTTP): goto during a STOP, stale stop
generation, STOP not queueing behind a slow command, Go limp vs goto, unconfirmed holds, shutdown.
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
    check(st.stopping == 1, "stop is in progress")
    for what, fn in [("goto", lambda: go()), ("Hold here", lambda: st.call(st.set_torque(True, gen=st.gen))),
                     ("Go limp", lambda: st.call(st.set_torque(False, gen=st.gen))),
                     ("arm switch", lambda: st.call(st.select_bus("SIM", gen=st.gen)))]:
        msg = refusal(fn)
        check(msg and "stopped before it started" in msg, f"{what} during a STOP is refused: {msg}")
    stopper.join(5)
    motion.hold = real_hold
    check(results and results[0]["ok"] and results[0]["was_moving"] and results[0]["hold"] == "confirmed",
          f"the STOP itself completed: {results}")
    check(not st.moving and st.stopping == 0 and still(), "stopped, holding still")
    check(st.snapshot()["motion"]["last_result"]["error"] == "stopped", "state says stopped")

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
    real_engage = motion.engage

    async def slow_engage(arm):
        await asyncio.sleep(3)

    motion.engage = slow_engage
    holder = []
    t = threading.Thread(target=lambda: holder.append(refusal(lambda: st.call(st.set_torque(True, gen=st.gen)))))
    t.start()
    time.sleep(0.15)
    check(st.command_task is not None, "the slow command owns the arm")
    t0 = time.monotonic()
    r = st.call(st.stop())
    took = time.monotonic() - t0
    t.join(5)
    motion.engage = real_engage
    check(r["ok"] and took < 0.5, f"STOP answered in {took:.2f}s while a 3 s command was running")
    check(holder and holder[0] and "interrupted by STOP" in holder[0], f"the slow command was interrupted: {holder}")
    check(st.command_task is None and not st.owner.locked(), "ownership is free again")

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
        check(not st.moving and st.stopping == 0, f"#{i}: nothing is moving after STOP")
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
    r = post("/api/goto", {"view": views[0]})
    check(r[0] == 200, f"move before {sig.name}: {r}")
    time.sleep(0.8)
    check(state()["motion"]["moving"], f"moving before {sig.name}")
    late = []

    def pester():
        for _ in range(30):
            try:
                late.append(post("/api/goto", {"view": views[1]})[0])
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

        def assert_stopped(i, what):
            s1 = state()
            check(not s1["motion"]["moving"] and not s1["stopping"], f"{what} #{i}: nothing is moving after STOP")
            check(comes_to_rest(lambda: state()["joints"]), f"{what} #{i}: the arm is still")
            check(not state()["motion"]["moving"], f"{what} #{i}: and still not moving")

        # --- STOP vs a goto that is already on its way (connection open) when STOP is sent
        tally = {"started then stopped": 0, "refused": 0}
        for i in range(120):
            goto_conn, stop_conn = connect(), connect()
            time.sleep(0.02)        # both accepted: the goto is "in flight"
            g, s = both(lambda: send(goto_conn, "POST", "/api/goto", {"view": views[i % len(views)], "speed": "normal"}),
                        lambda: send(stop_conn, "POST", "/api/stop", {}))
            check(not isinstance(g, Exception) and not isinstance(s, Exception), f"in flight #{i}: both answered: {g} {s}")
            check(s[0] == 200 and s[1]["hold"] == "confirmed", f"in flight #{i}: STOP held: {s}")
            check(g[0] in (200, 409), f"in flight #{i}: goto answered {g}")
            tally["started then stopped" if g[0] == 200 else "refused"] += 1
            assert_stopped(i, "in flight")
        print(f"  STOP vs in-flight goto x120: {tally}")

        # --- STOP vs goto, both connecting at the same instant: a move may only survive if its
        #     request reached the server after the STOP did (then it is simply a later click)
        tally = {"started then stopped": 0, "refused": 0, "arrived after the STOP": 0}
        for i in range(120):
            g, s = both(lambda: post("/api/goto", {"view": views[i % len(views)], "speed": "normal"}),
                        lambda: post("/api/stop"))
            check(s[0] == 200 and g[0] in (200, 409), f"same instant #{i}: {g} {s}")
            if state()["motion"]["moving"]:
                check(g[0] == 200 and g[1]["generation"] >= s[1]["generation"],
                      f"same instant #{i}: a move survived a STOP that came after it: {g} {s}")
                tally["arrived after the STOP"] += 1
                check(post("/api/stop")[1]["was_moving"], "and the next STOP stops it")
            else:
                tally["started then stopped" if g[0] == 200 else "refused"] += 1
            assert_stopped(i, "same instant")
        print(f"  STOP vs goto at the same instant x120: {tally}")

        # --- Go limp vs goto at the same instant
        tally = {"limp first": 0, "move first": 0}
        post("/api/goto", {"view": "transit", "recover": True})
        for _ in range(300):
            time.sleep(0.2)
            if not state()["motion"]["moving"]:
                break
        for i in range(40):
            post("/api/stop")
            r = post("/api/torque", {"on": True})
            check(r[0] == 200, f"limp #{i}: Hold here: {r}")
            rel, g = both(lambda: post("/api/torque", {"on": False}), lambda: post("/api/goto", {"view": views[i % len(views)]}))
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
        check(post("/api/torque", {"on": True})[0] == 200, "holding again")

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
