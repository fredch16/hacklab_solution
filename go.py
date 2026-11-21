"""M2.5: move the follower to saved views from the command line.

    python go.py top                 # one view
    python go.py home top home       # a sequence
    python go.py --tour 5            # M2.6/2.7: home -> every view -> home, 5 rounds
    python go.py --list

Ctrl-C holds the arm where it is. Set the follower to "(Self-controlled)" in the
Station web UI first, or mirroring will fight these moves.

While control_server.py runs, use ctl.py instead: only one process may write to the arm.
"""

import argparse
import asyncio
import socket

from motion import go_to_view, hold, view_names
from station_arm import Arm, run


def control_service_running(port: int = 8100) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


async def main(args):
    if not args.list and control_service_running():
        raise RuntimeError("control_server.py is running and owns the arm; use ctl.py (e.g. python ctl.py go top --wait)")
    if args.list:
        print("\n".join(view_names()) or "no views yet: run capture_view.py")
        return
    names = list(args.views)
    if args.tour:
        stops = [v for v in view_names() if v != "home"]
        names = ["home"] + (stops + ["home"]) * args.tour if "home" in view_names() else stops * args.tour
    if not names:
        raise RuntimeError("give view names, --tour N or --list")
    arm = await Arm.connect(args.server, args.bus)
    print(f"bus {arm.bus_serial}")
    try:
        for name in names:
            r = await go_to_view(arm, name, include_gripper=args.gripper, max_speed=args.speed)
            print(f"{'ok  ' if r['ok'] else 'FAIL'} {name:<10} {r.get('duration', '')}s {r['error'] or ''}")
            if not r["ok"]:
                break
            await asyncio.sleep(args.pause)
    except asyncio.CancelledError:
        await hold(arm)
        raise
    finally:
        arm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("views", nargs="*")
    p.add_argument("--tour", type=int, default=0, metavar="N")
    p.add_argument("--list", action="store_true")
    p.add_argument("--speed", type=float, default=500, help="max steps/s (default 500, ~44 deg/s)")
    p.add_argument("--pause", type=float, default=1.0, help="seconds to wait at each view")
    p.add_argument("--gripper", action="store_true", help="also move the gripper (off: camera may be on it)")
    p.add_argument("--server", default="localhost")
    p.add_argument("--bus", default="auto")
    run(main(p.parse_args()))
