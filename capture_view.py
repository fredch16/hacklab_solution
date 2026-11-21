"""M1: save the follower arm's current pose as named camera views in views.json.

    python capture_view.py           # guided: walks through top, wide, left, ... in order
    python capture_view.py --free    # free mode: pose, type any name, repeat
    python capture_view.py top       # save the current pose as "top" and exit

Pose the follower by moving the leader arm (Station web UI: follower card ->
control source -> leader's serial), or by hand with `python torque.py off`.
Each capture waits for the arm to stop moving, then stores the joints and,
if Station has a camera, a snapshot in views/<name>.jpg.

Guided mode keys:   <Enter> save   s skip   r redo previous   q quit
Free mode commands: <name> save   <Enter> show joints   list   rm <name>   q quit
"""

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from station_arm import Arm, format_joints, logger, run

ROOT = Path(__file__).parent
VIEWS_PATH = ROOT / "views.json"
SNAPSHOT_DIR = ROOT / "views"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

GUIDED_VIEWS = [
    ("home", "safe rest pose, arm folded"),
    ("top", "top-down over the workspace"),
    ("wide", "whole desk in frame"),
    ("left", "about 45 degrees from the left"),
    ("right", "about 45 degrees from the right"),
    ("close", "close-up of the workspace centre"),
    ("side_low", "low side angle"),
]


def load_views(path: Path = VIEWS_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"views": {}}


def save_views(data: dict, path: Path = VIEWS_PATH):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


async def open_camera(arm: Arm):
    """Camera for snapshots, or None if Station has no camera right now."""
    from camera import Camera, list_camera_queues
    try:
        cams = await list_camera_queues(arm.client, timeout_s=2.0)
        return Camera(arm.client, cams[0]) if cams else None
    except Exception as e:
        logger.warning("no camera snapshots: %s", e)
        return None


async def capture(arm: Arm, data: dict, name: str, cam=None, description: str = "",
                  confirm_overwrite: bool = True) -> bool:
    if not NAME_RE.match(name):
        print(f"bad name {name!r}: use lowercase letters, digits and _, starting with a letter")
        return False
    if name in data["views"] and confirm_overwrite:
        if (await ask(f"'{name}' exists, overwrite? [y/N] ")).lower() != "y":
            return False
    joints = await arm.settled()
    view = {
        "joints": {str(mid): pos for mid, pos in sorted(joints.items())},
        "description": description or data["views"].get(name, {}).get("description", ""),
        "bus": arm.bus_serial,
        # Saved poses are raw encoder steps: only valid for the calibration they were captured with.
        "ranges": {str(m): list(r) for m, r in sorted(arm.ranges().items())},
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if cam is not None:
        try:
            SNAPSHOT_DIR.mkdir(exist_ok=True)
            (SNAPSHOT_DIR / f"{name}.jpg").write_bytes(await cam.get_frame(timeout_s=2.0))
            view["snapshot"] = f"views/{name}.jpg"
        except RuntimeError as e:
            logger.warning("no snapshot for %s: %s", name, e)
    data["views"][name] = view
    if name == "transit":       # every route passes through it: re-teaching it un-checks all views
        for other in data["views"].values():
            other.pop("checked", None)
    save_views(data)
    print(f"saved '{name}': {format_joints(joints)}" + (" + snapshot" if "snapshot" in view else ""))
    return True


def print_views(data: dict):
    if not data["views"]:
        print("(no views yet)")
    for name, view in data["views"].items():
        joints = {int(k): v for k, v in view["joints"].items()}
        print(f"  {name:<10} {format_joints(joints)}")


async def guided(arm: Arm, data: dict, cam):
    print("Guided capture. For each view: move the leader until the camera looks right, then press Enter.")
    print("Keys: <Enter> save   s skip   r redo previous   q quit\n")
    i = 0
    while i < len(GUIDED_VIEWS):
        name, desc = GUIDED_VIEWS[i]
        done = " (already saved, Enter overwrites)" if name in data["views"] else ""
        key = (await ask(f"[{i + 1}/{len(GUIDED_VIEWS)}] {name}: {desc}{done} > ")).lower()
        if key == "q":
            break
        if key == "s":
            i += 1
        elif key == "r":
            i = max(0, i - 1)
        elif key == "":
            if await capture(arm, data, name, cam, desc, confirm_overwrite=False):
                i += 1
        else:
            print("Enter, s, r or q")
    print()
    print_views(data)


async def free(arm: Arm, data: dict, cam):
    print("Free mode: <name> save   <Enter> show joints   list   rm <name>   q quit")
    print_views(data)
    while True:
        line = await ask("view> ")
        if line in ("q", "quit", "exit"):
            return
        if line == "":
            print(format_joints(arm.joints()))
        elif line == "list":
            print_views(data)
        elif line.startswith("rm "):
            name = line[3:].strip()
            if data["views"].pop(name, None) is not None:
                save_views(data)
                print(f"removed '{name}'")
            else:
                print(f"no view '{name}'")
        else:
            await capture(arm, data, line, cam)


async def main(args):
    arm = await Arm.connect(args.server, args.bus)
    print(f"connected to {args.server}, saving poses of bus {arm.bus_serial}")
    data = load_views()
    cam = None if args.no_snapshots else await open_camera(arm)
    print(f"snapshots: {'on, from ' + cam.queue if cam else 'off'}")
    try:
        if args.name:
            await capture(arm, data, args.name, cam, confirm_overwrite=not args.force)
        elif args.free:
            await free(arm, data, cam)
        else:
            await guided(arm, data, cam)
    finally:
        if cam:
            cam.close()
        arm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", nargs="?", help="save the current pose under this name and exit")
    p.add_argument("--free", action="store_true", help="free mode: type your own view names")
    p.add_argument("--server", default="localhost")
    p.add_argument("--bus", default="auto", help="follower bus serial (auto: the arm with torque on)")
    p.add_argument("--no-snapshots", action="store_true")
    p.add_argument("-f", "--force", action="store_true", help="overwrite an existing view without asking")
    run(main(p.parse_args()))
