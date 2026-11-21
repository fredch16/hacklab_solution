"""Live calibration helper: which arm is moving, and how much range each joint has covered.

    python calib_watch.py

Move a joint slowly from one end stop to the other; its bar fills as Station learns the range.
A joint is "ok" once it has covered MIN_OK steps (about 70 degrees). Save in the web UI
when every joint you can move shows ok and you've hit both ends of each.
"""

import argparse
import asyncio

from station_arm import Arm, gripper_id, run

MIN_OK = 800      # steps; 4096 = one full turn
BAR = 24


async def main(args):
    arm = await Arm.connect(args.server, bus="any")
    prev = arm.all_buses()
    try:
        while True:
            await asyncio.sleep(0.5)
            now = arm.all_buses()
            lines = []
            for serial, joints in now.items():
                arm.bus_serial = serial
                moving = max((abs(joints[m] - prev.get(serial, {}).get(m, joints[m])) for m in joints), default=0)
                frozen = bool(arm._bus(serial)) and all(
                    m.get_range_freezed() for m in (arm._bus(serial).get_motors() or []))
                ranges = arm.ranges()
                grip = gripper_id(joints)
                status = "SAVED" if frozen else ("<<< MOVING NOW" if moving > 15 else "")
                lines.append(f"arm {serial}  {status}")
                for m in sorted(joints):
                    lo, hi = ranges.get(m, (0, 0))
                    width = (hi - lo) % 4096
                    fill = min(BAR, width * BAR // 2048)
                    name = "gripper" if m == grip else f"motor {m}"
                    mark = "ok" if width >= MIN_OK else "  "
                    lines.append(f"  {name:<8} [{'#' * fill}{'.' * (BAR - fill)}] {width:>4} steps {mark}")
                lines.append("")
            print("\033[2J\033[H" + "\n".join(lines), flush=True)
            prev = now
    finally:
        arm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="localhost")
    run(main(p.parse_args()))
