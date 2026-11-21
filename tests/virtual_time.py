"""Simulated time for tests: asyncio code that "takes" ten seconds finishes in milliseconds and
gives the same numbers on every run. Standard library only.

    from virtual_time import run, now
    result = run(main())          # like asyncio.run(main()), but nothing ever really waits
    t = now()                     # the clock to measure with inside main(): simulated seconds

`run` uses an event loop whose clock is virtual: it only moves when every task is waiting, and
then jumps straight to the next timer, so asyncio.sleep / wait_for never really wait and code
that computes takes no simulated time at all. For the duration of the run motion.py and
sim_arm.py read that same clock (their module-level `_now`); both are put back afterwards.
Only for the simulated arm: never use this with real hardware.

It leans on three private attributes of asyncio's event loop (`_ready`, `_scheduled`,
`_run_once`). `works()` says whether they are there; callers fall back to real time
(`run(coro, realtime=True)` is plain asyncio.run) when it returns False.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def _run_once(self):
        if not self._ready:
            # Nothing can run right now: jump to the next timer that is still wanted.
            pending = [h._when for h in self._scheduled if not h._cancelled]
            if pending:
                self.now = max(self.now, min(pending))
        super()._run_once()


def works() -> bool:
    if not hasattr(asyncio.BaseEventLoop, "_run_once"):
        return False
    loop = asyncio.new_event_loop()
    try:
        return hasattr(loop, "_ready") and hasattr(loop, "_scheduled")
    finally:
        loop.close()


def now() -> float:
    """Seconds on the clock of the running loop: simulated inside run(), real otherwise."""
    return asyncio.get_running_loop().time()


def run(coro, realtime: bool = False):
    """Run `coro` to completion in simulated time and return its result. With realtime=True (or
    when this Python has no room for a virtual loop) it is asyncio.run(coro) on the real clock."""
    if realtime or not works():
        return asyncio.run(coro)
    import motion
    import sim_arm
    loop = VirtualLoop()
    saved = (motion._now, sim_arm._now)
    motion._now = sim_arm._now = loop.time
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        motion._now, sim_arm._now = saved
        asyncio.set_event_loop(None)
        loop.close()
