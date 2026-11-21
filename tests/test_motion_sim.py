"""Motion layer on the simulated arm: arc math, smooth moves, gripper untouched, cancel holds.

    python3 tests/test_motion_sim.py
"""
import asyncio, json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import motion
from motion import JointArc, go_to_view
from sim_arm import SimArm

# arc math, incl. wraparound arc 3500..500
a = JointArc(3500, 500)
assert a.size == 1096 and a.calibrated
assert a.to_rel(3500) == 30 and a.to_rel(4000) == 500 and a.to_rel(100) == 696
assert a.to_rel(2000) in (30, 1066)       # outside arc snaps to an end
assert a.to_pos(696) == 100
u = JointArc(0, 0); assert not u.calibrated and u.to_pos(u.to_rel(1234)) == 1234

tmp = Path(tempfile.mkdtemp()) / "views.json"
tmp.write_text(json.dumps({"views": {
    "home": {"joints": {"1": 2048, "2": 1000, "3": 3000, "4": 2048, "5": 2048, "6": 2000}},
    "left": {"joints": {"1": 1400, "2": 1800, "3": 2200, "4": 2500, "5": 2048, "6": 3000}},
}}))
motion.VIEWS_PATH = tmp

async def main():
    arm = SimArm()
    for name in ["home", "left", "home", "nope"]:
        t = time.monotonic()
        r = await go_to_view(arm, name)
        print(f"{name:5} {r}  wall={time.monotonic()-t:.2f}s  joints={arm.joints()}")
    assert arm.joints()[6] == 2048, "gripper must not move by default"
    print("writes:", arm.writes)
    # cancel mid-move -> holds
    task = asyncio.create_task(go_to_view(arm, "left"))
    await asyncio.sleep(0.5); task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    j1 = arm.joints(); await asyncio.sleep(0.5); j2 = arm.joints()
    print("after cancel, still:", j1 == j2, j2)
    assert j1 == j2, "cancel must hold position"
asyncio.run(main())

print("ALL OK")
