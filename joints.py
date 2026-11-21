"""M0.7: print live joint positions from Station, for every arm it sees.

    python joints.py                 # localhost, all buses
    python joints.py --server 192.168.1.20

Tip: wiggle one arm by hand and watch which serial changes. With mirroring on,
both change; the leader is the one marked "limp".
"""

import argparse
import asyncio

from station_arm import Arm, format_joints, run


async def main(args):
    arm = await Arm.connect(args.server, bus="any")
    for serial in arm.all_buses():
        torque = "ON (holds pose)" if arm._torque_on(serial) else "off (limp)"
        print(f"bus {serial}: torque {torque}")
    try:
        while True:
            for serial, joints in arm.all_buses().items():
                print(f"{serial}  {format_joints(joints)}")
            print(flush=True)
            await asyncio.sleep(1 / args.hz)
    finally:
        arm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="localhost")
    p.add_argument("--hz", type=float, default=2)
    run(main(p.parse_args()))
