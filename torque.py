"""M1.1: switch arm torque on or off so it can be posed by hand.

    python torque.py off    # arm goes limp; HOLD IT FIRST or it drops
    python torque.py on     # arm holds its current pose (joints 1-7; the gripper stays limp)

Station's web UI may also offer this; either works.
"""

import argparse
import asyncio

import motion
from station_arm import Arm, format_joints, run


async def main(args):
    arm = await Arm.connect(args.server, args.bus)
    try:
        if args.state == "off" and not args.yes:
            answer = await asyncio.to_thread(
                input, "Torque OFF: the arm will go limp. Is someone holding it? [y/N] "
            )
            if answer.strip().lower() != "y":
                print("aborted")
                return
        if args.state == "on":
            await motion.engage(arm)      # goal := present first; a bare torque-on jumps to a stale goal
        else:
            await motion.release(arm)
        await asyncio.sleep(0.3)
        print(f"torque {args.state} on bus {arm.bus_serial}; joints: {format_joints(arm.joints())}")
    finally:
        arm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--server", default="localhost")
    p.add_argument("--bus", default="auto")
    p.add_argument("-y", "--yes", action="store_true", help="skip the 'holding it?' prompt")
    run(main(p.parse_args()))
