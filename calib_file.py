"""Carry Station's arm calibration to another computer. Standard library only; never talks to
Station or the arm, it only reads and copies one small file.

    python3 calib_file.py show              # the calibrated ranges in config/station-calibration.wal
    python3 calib_file.py show --station    # ...and in this computer's Station, to compare
    python3 calib_file.py export            # this computer's Station -> config/station-calibration.wal
    python3 calib_file.py install           # config/station-calibration.wal -> this computer's Station

Why this matters: saved views (views.json) are raw encoder steps and only mean the same poses
with the calibration they were captured with. Calibrating again on another computer would shift
every joint's zero and make all saved views invalid (motion.py then refuses them).

A calibration has two halves:
  * the calibrated range of every joint, which Station keeps in one file,
        <station folder>/station_data/<instance id>/st3215/meta/wal/001.wal
    This is the file this script copies.
  * each servo's own zero offset, which Station writes into the servo when a calibration is
    saved. That half lives in the arm, so it travels with the arm.
So the file is only right for the SAME two arms (the serial numbers are inside it).

`install` wants Station stopped, keeps a backup of the file it replaces, and needs Station to
have run once on the new computer (that creates the folders). Not verified: nobody has started
Station on a second computer with a copied file yet. After the first start, check that
`python3 go.py --where` reports no "recalibrated since ... was captured" for the saved views.
"""
import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_COPY = Path(__file__).resolve().parent / "config" / "station-calibration.wal"
STATION_DIR = Path.home() / "station"
WAL_IN_INSTANCE = Path("st3215") / "meta" / "wal" / "001.wal"


def _varint(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, i


def read_ranges(path: Path) -> dict[str, dict[int, tuple[int, int]]]:
    """{arm serial: {motor id: (min, max)}} from a Station calibration file: for each arm the
    newest record that carries ranges (a reset is a record without any)."""
    data = Path(path).read_bytes()
    out: dict[str, dict[int, tuple[int, int]]] = {}
    for match in re.finditer(rb"\x32\x0a([0-9A-Za-z]{10})", data):
        i, motors = match.end(), {}
        while i < len(data) and data[i] == 0x52:               # one motor: id, min, max
            length, j = _varint(data, i + 1)
            end, fields = j + length, {}
            while j < end:
                tag, j = _varint(data, j)
                value, j = _varint(data, j)
                fields[tag >> 3] = value
            if not {2, 3, 4} <= set(fields):
                break
            motors[fields[2]] = (fields[3], fields[4])
            i = end
        if motors:
            out[match.group(1).decode()] = dict(sorted(motors.items()))
    return out


def station_file(station_dir: Path) -> Path:
    """The calibration file of the Station installed in `station_dir`."""
    data = Path(station_dir) / "station_data"
    instances = [p for p in data.glob("*") if p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name)]
    if not instances:
        raise SystemExit(f"no Station data in {data}. Start Station there once (./station --web --tcp), "
                         "stop it again, then repeat this command.")
    if len(instances) > 1:
        raise SystemExit(f"several Station instances in {data}: {[p.name[:8] for p in instances]}; "
                         "remove the ones that are not in use")
    return instances[0] / WAL_IN_INSTANCE


def station_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "station"], capture_output=True).returncode == 0
    except OSError:
        return False


def show(path: Path, title: str) -> dict:
    ranges = read_ranges(path)
    print(f"{title}: {path}")
    if not ranges:
        print("  no calibration in this file")
    for serial, motors in ranges.items():
        print(f"  arm {serial}: " + "  ".join(f"M{m} {lo}-{hi}" for m, (lo, hi) in motors.items()))
    return ranges


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("action", choices=["show", "export", "install"])
    p.add_argument("--station", action="store_true", help="show: also show this computer's Station")
    p.add_argument("--station-dir", type=Path, default=STATION_DIR, help=f"where Station is installed (default {STATION_DIR})")
    p.add_argument("--force", action="store_true", help="install even though Station seems to be running")
    args = p.parse_args()

    if args.action == "show":
        if not REPO_COPY.exists():
            print(f"{REPO_COPY} is missing; run 'export' on the computer the arms were calibrated on")
            return 1
        ours = show(REPO_COPY, "in the repo")
        if args.station:
            theirs = show(station_file(args.station_dir), "in this computer's Station")
            print("same calibration" if ours == theirs else "DIFFERENT calibration")
        return 0

    if args.action == "export":
        source = station_file(args.station_dir)
        if not source.exists() or not read_ranges(source):
            raise SystemExit(f"{source} holds no calibration; calibrate the arms in Station first")
        REPO_COPY.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, REPO_COPY)
        show(REPO_COPY, "copied into the repo")
        print("commit and push config/station-calibration.wal")
        return 0

    if not REPO_COPY.exists() or not read_ranges(REPO_COPY):
        raise SystemExit(f"{REPO_COPY} is missing or holds no calibration")
    if station_running() and not args.force:
        raise SystemExit("Station is running. Stop it first: it only reads this file when it starts, "
                         "and may overwrite it while running.")
    target = station_file(args.station_dir)
    if target.exists() and target.read_bytes() == REPO_COPY.read_bytes():
        print(f"{target} already holds this calibration")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"001.wal.before-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(target, backup)
        print(f"kept the old file as {backup}")
    shutil.copyfile(REPO_COPY, target)
    show(target, "installed")
    print("Start Station, then check: python3 go.py --where  (the saved views must not be reported as recalibrated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
