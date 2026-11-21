"""How long do moves take, and how much of that time is one joint group moving on its own?

    python3 tests/bench_moves.py                          # every ordered pair, normal / fast / turbo
    python3 tests/bench_moves.py --json after.json        # ...and keep the numbers
    python3 tests/bench_moves.py --compare docs/bench/baseline.json     # table against an earlier run
    python3 tests/bench_moves.py --pairs right:top right:transit --speeds fast
    python3 tests/bench_moves.py --set DIRECT=0 --set BLEND=0           # motion.py flags for this run

Runs on the simulated arm only (sim_arm.SimArm), never on Station. By default the simulated arm
answers like the real one (lag, acceleration limit, shoulder sag: SimArm(realistic=True)) and the
clock is virtual (tests/virtual_time.py), so the whole table takes seconds and is the same on
every run. `--ideal` uses the lag-free arm, `--realtime` the real clock.

Every view counts as checked here (otherwise unchecked views crawl at first-run speed);
`--as-saved` uses the flags in views.json instead.

Per move:
  time    wall time of go_to_view (preflight, all segments, settling)
  solo    time with only one joint group moving (pitch = M2 M3 M4 M6, turn = M1 M5 M7) while the
          other stands still; `idle` is time with nothing moving at all
  turn-h  lowest height (fk.py, forearm..camera, lower of both mappings) at which M1 or M5 was moving
  min-h   lowest point of the whole arm during the move (fk.py with link meshes, lower of both
          mappings); `dip` = how far that is below the lower of the two end poses
"Moving" = faster than 40 steps/s (3.5 deg/s), the speed below which motion.py calls a joint still.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fk
import motion
import virtual_time
from sim_arm import SimArm

SPEEDS = {"normal": motion.SPEED_NORMAL, "fast": motion.SPEED_FAST, "turbo": motion.SPEED_TURBO}
MOVING = 40.0          # steps/s
SAMPLE_S = 0.02
TURN_MAIN = (1, 5)     # the joints that sweep the arm sideways; M7 only spins the camera


def _poses():
    raw = motion.load_views()
    env = motion.envelope(SimArm().ranges())
    return {n: {**motion.clamp_pose(p, env), motion.GRIPPER: p.get(motion.GRIPPER, 1279)} for n, p in raw.items()}


_height_cache: dict = {}


def _heights(pose: dict, ranges) -> tuple[float, float]:
    """(clear-point height, lowest point of the arm with meshes), lower of both mappings."""
    key = tuple(sorted(pose.items()))
    if key not in _height_cache:
        _height_cache[key] = (motion._height(pose, ranges), fk.min_height(pose, ranges, mapping="worst")[0])
    return _height_cache[key]


async def run_move(a: str, b: str, speed: float, realistic: bool) -> dict:
    poses = _poses()
    arm = SimArm(start=dict(poses[a]), realistic=realistic)
    ranges = arm.ranges()
    await asyncio.sleep(1.0)                      # let the shoulder sag onto its resting place
    samples = []                                  # (moving pitch, moving turn, moving M1/M5, pose)

    async def watch():
        prev, prev_t = dict(arm._pos), time.monotonic()
        while True:
            await asyncio.sleep(SAMPLE_S)
            arm.joints()
            now = time.monotonic()
            dt = max(now - prev_t, 1e-6)
            vel = {m: abs(arm._pos[m] - prev[m]) / dt for m in prev}
            prev, prev_t = dict(arm._pos), now
            samples.append((any(vel[m] > MOVING for m in motion.PITCH), any(vel[m] > MOVING for m in motion.TURN),
                            any(vel[m] > MOVING for m in TURN_MAIN), dt,
                            {m: int(round(p)) for m, p in arm._pos.items()}))

    start_pose = arm.joints()
    watcher = asyncio.ensure_future(watch())
    t0 = time.monotonic()
    r = await motion.go_to_view(arm, b, max_speed=speed)
    took = time.monotonic() - t0
    watcher.cancel()
    end_pose = arm.joints()
    solo_pitch = sum(dt for p, t, _, dt, _ in samples if p and not t)
    solo_turn = sum(dt for p, t, _, dt, _ in samples if t and not p)
    idle = sum(dt for p, t, _, dt, _ in samples if not p and not t)
    turn_h = min((_heights(pose, ranges)[0] for _, _, main, _, pose in samples if main), default=None)
    min_h = min([_heights(pose, ranges)[1] for *_, pose in samples] + [_heights(start_pose, ranges)[1]])
    ends = min(_heights(start_pose, ranges)[1], _heights(end_pose, ranges)[1])
    arrived = all(abs(end_pose[m] - poses[b][m]) <= motion.ARRIVE_TOL for m in motion.MOVABLE)
    return {"from": a, "to": b, "ok": bool(r["ok"] and arrived), "error": r["error"], "route": r["route"],
            "time": round(took, 2), "solo_pitch": round(solo_pitch, 2), "solo_turn": round(solo_turn, 2),
            "idle": round(idle, 2), "turn_h": None if turn_h is None else round(turn_h, 3),
            "min_h": round(min_h, 3), "dip": round(max(0.0, ends - min_h), 3)}


def all_pairs() -> list[tuple[str, str]]:
    names = [n for n in motion.load_views() if n != motion.TRANSIT]
    pairs = [(a, b) for a in names for b in names if a != b]
    return pairs + [(a, motion.TRANSIT) for a in names] + [(motion.TRANSIT, b) for b in names]


async def bench(pairs, speeds, realistic: bool, quiet: bool = False) -> dict:
    out = {}
    for label in speeds:
        out[label] = []
        for a, b in pairs:
            out[label].append(await run_move(a, b, SPEEDS[label], realistic))
            if not quiet:
                print(".", end="", flush=True, file=sys.stderr)
    if not quiet:
        print(file=sys.stderr)
    return out


def _cm(v) -> str:
    return "   -" if v is None else f"{v * 100:4.0f}"


def table(results: dict, baseline: dict | None = None, markdown: bool = False) -> str:
    """One row per move with every speed side by side, then totals per speed."""
    speeds = list(results)
    rows = {}
    for label in speeds:
        for r in results[label]:
            rows.setdefault((r["from"], r["to"]), {})[label] = r
    base = {(label, r["from"], r["to"]): r for label in (baseline or {}) for r in baseline[label]}
    sep = " | "
    head = ["move".ljust(20)] + [f"{label}: " + ("was -> now" if baseline else "time") + "  solo idle turn-h dip  route"
                                 for label in speeds]
    lines = [sep.join(head)]
    if markdown:
        lines.append(sep.join("---" for _ in head))
    for (a, b), per in rows.items():
        cells = [f"{a} -> {b}".ljust(20)]
        for label in speeds:
            r = per.get(label)
            if r is None:
                cells.append("")
                continue
            was = base.get((label, a, b))
            t = f"{was['time']:5.1f} -> {r['time']:5.1f}" if was else f"{r['time']:5.1f}"
            mark = "" if r["ok"] else " FAILED: " + str(r["error"])
            cells.append(f"{t}  {r['solo_pitch'] + r['solo_turn']:4.1f} {r['idle']:4.1f} {_cm(r['turn_h'])} "
                         f"{r['dip'] * 100:3.0f}  {'>'.join(r['route'])}{mark}")
        lines.append(sep.join(cells))
    lines.append("")
    for label in speeds:
        rs = results[label]
        views_only = [r for r in rs if motion.TRANSIT not in (r["from"], r["to"])]
        line = (f"{label:6s} total {sum(r['time'] for r in rs):7.1f} s over {len(rs)} moves"
                f" (view to view: {sum(r['time'] for r in views_only):7.1f} s over {len(views_only)});"
                f" one group alone {sum(r['solo_pitch'] + r['solo_turn'] for r in rs):6.1f} s"
                f" (pitch {sum(r['solo_pitch'] for r in rs):.1f}, turn {sum(r['solo_turn'] for r in rs):.1f}),"
                f" nothing moving {sum(r['idle'] for r in rs):6.1f} s;"
                f" lowest M1/M5 turn on a hub route"
                f" {min((r['turn_h'] for r in rs if r['turn_h'] is not None and 'rise' in r['route']), default=0) * 100:.0f} cm;"
                f" worst dip below the end poses {max(r['dip'] for r in rs) * 100:.1f} cm;"
                f" failed {sum(not r['ok'] for r in rs)}")
        if baseline and label in baseline:
            bs = {(r["from"], r["to"]): r for r in baseline[label]}
            common = [r for r in rs if (r["from"], r["to"]) in bs]
            was = sum(bs[(r["from"], r["to"])]["time"] for r in common)
            now = sum(r["time"] for r in common)
            if was:
                line += f"; was {was:.1f} s -> {now:.1f} s ({(now - was) / was * 100:+.0f}%)"
        lines.append(line)
    if markdown:
        return "\n".join("| " + ln + " |" if sep in ln or set(ln) <= {"-", " ", "|"} and ln else ln for ln in lines)
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--speeds", nargs="+", default=list(SPEEDS), choices=list(SPEEDS))
    p.add_argument("--pairs", nargs="+", metavar="FROM:TO", help="only these moves (default: all)")
    p.add_argument("--ideal", action="store_true", help="lag-free simulated arm instead of the realistic one")
    p.add_argument("--realtime", action="store_true", help="real clock (slow) instead of the virtual one")
    p.add_argument("--as-saved", action="store_true", help="use the checked flags in views.json")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="set a motion.py constant")
    p.add_argument("--json", metavar="FILE", help="write the results here")
    p.add_argument("--compare", metavar="FILE", help="show times against an earlier --json file")
    p.add_argument("--markdown", action="store_true")
    args = p.parse_args()

    motion.LOG_PATH = Path("/dev/null")
    if not args.as_saved:
        motion.view_checked = lambda name: True
    for item in args.set:
        name, value = item.split("=", 1)
        if not hasattr(motion, name):
            p.error(f"motion.py has no {name}")
        setattr(motion, name, json.loads(value.lower()) if value.lower() in ("true", "false") else float(value)
                if "." in value else int(value))
    pairs = [tuple(x.split(":", 1)) for x in args.pairs] if args.pairs else all_pairs()
    job = bench(pairs, args.speeds, realistic=not args.ideal)
    virtual = not args.realtime and virtual_time.works()
    t0 = time.perf_counter()
    results = virtual_time.run(job) if virtual else asyncio.run(job)
    baseline = json.loads(Path(args.compare).read_text())["results"] if args.compare else None
    print(f"simulated arm ({'ideal' if args.ideal else 'realistic response'}), "
          f"{'virtual' if virtual else 'real'} clock, {'checked flags from views.json' if args.as_saved else 'every view counted as checked'}"
          f"{', ' + ' '.join(args.set) if args.set else ''}; computed in {time.perf_counter() - t0:.0f} s")
    print("times in s, heights in cm; turn-h = lowest height at which M1 or M5 was moving")
    print(table(results, baseline, args.markdown))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({"realistic": not args.ideal, "virtual_clock": virtual,
                                               "set": args.set, "results": results}, indent=1) + "\n")
    return 1 if any(not r["ok"] for rs in results.values() for r in rs) else 0


if __name__ == "__main__":
    sys.exit(main())
