"""M2: move the follower to saved views from the command line (the web UI at /control does the same).

    python go.py --where             # where is the arm on the route network? (moves nothing)
    python go.py --engage            # limp arm -> hold its present pose (support the arm first)
    python go.py top                 # one view (unchecked views run at first-run speed)
    python go.py home top home       # a sequence
    python go.py --tour 5            # home -> every view -> home, 5 rounds
    python go.py --recover           # unknown pose -> transit, slow; someone must watch the arm
    python go.py --mark-checked top  # you watched top's move run cleanly; allows normal speed
    python go.py --list

Ctrl-C holds the arm where it is. Set the follower to "(Self-controlled)" in Station first.
Add --sim to try any of this on the simulated arm.
"""

import argparse
import asyncio

import motion
from station_arm import run


async def main(args):
    if args.list:
        for name in motion.view_names():
            print(f"{name:<10} {'checked' if motion.view_checked(name) else 'unchecked (slow only)'}")
        return
    if args.mark_checked:
        motion.mark_checked(args.mark_checked)
        print(f"{args.mark_checked}: marked as checked")
        return
    if args.sim:
        from sim_arm import SimArm
        arm = await SimArm.connect()
    else:
        from station_arm import Arm
        arm = await Arm.connect(args.server, args.bus)
    print(f"bus {arm.bus_serial}")
    try:
        if args.where:
            d = await motion.describe(arm)
            print("where:", d["where"] or "cannot tell")
            for b in d["blockers"]:
                print("cannot move yet:", b)
            if d["ok"]:
                print("ready to move")
            for name, notes in motion.check_views(arm.ranges()).items():
                print(f"note: {name} is saved at a joint limit and will be clamped: {'; '.join(notes)}")
            return
        if args.engage:
            answer = await asyncio.to_thread(input, "The arm will stiffen where it is. Is it supported and clear? [y/N] ")
            if answer.strip().lower() == "y":
                await motion.engage(arm)
                print("holding")
            return
        names = list(args.views)
        if args.tour:
            stops = [v for v in motion.view_names() if v != "home"]
            names = ["home"] + (stops + ["home"]) * args.tour
        if args.recover:
            names = [motion.TRANSIT]
        if not names:
            raise RuntimeError("give view names, --tour N, --recover, --where, --engage or --list")
        speed = motion.SPEED_NORMAL if args.normal else motion.SPEED_SLOW
        for name in names:
            r = await motion.go_to_view(arm, name, max_speed=speed, allow_unknown_start=args.recover)
            print(f"{'ok  ' if r['ok'] else 'FAIL'} {name:<10} {r['duration']:>5}s  {' > '.join(r['route'])}  {r['error'] or ''}")
            if not r["ok"]:
                break
            await asyncio.sleep(args.pause)
    finally:
        arm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("views", nargs="*")
    p.add_argument("--tour", type=int, default=0, metavar="N")
    p.add_argument("--where", action="store_true", help="show where the arm is; moves nothing")
    p.add_argument("--engage", action="store_true", help="make a limp arm hold its present pose")
    p.add_argument("--recover", action="store_true", help="from an unknown pose, go up to transit (slow)")
    p.add_argument("--mark-checked", metavar="VIEW", help="record that this view's move was watched and is clean")
    p.add_argument("--list", action="store_true")
    p.add_argument("--normal", action="store_true", help="normal speed (checked views only; default is slow)")
    p.add_argument("--pause", type=float, default=1.0, help="seconds to wait at each view")
    p.add_argument("--sim", action="store_true", help="simulated arm, no hardware")
    p.add_argument("--server", default="localhost")
    p.add_argument("--bus", default="auto")
    run(main(p.parse_args()))
