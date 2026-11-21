"""Run asyncio code on a virtual clock, so simulated moves that "take" ten seconds finish in
milliseconds and give the same numbers on every run. Standard library only.

    from virtual_time import run
    result = run(main())          # like asyncio.run(main()), but nothing ever really waits

Inside `run`, `time.monotonic()` and the event loop's clock are the same virtual clock. It only
moves when every task is waiting: then it jumps straight to the next timer. Code that computes
takes no virtual time at all. Only for the simulated arm: never use this with real hardware.

It leans on three private attributes of asyncio's event loop (`_ready`, `_scheduled`, `_run_once`).
`works()` says whether they are there; callers fall back to real time when it returns False.
"""
import asyncio
import time


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
    return all(hasattr(asyncio.BaseEventLoop, a) for a in ("_run_once",)) and \
        all(hasattr(asyncio.new_event_loop(), a) for a in ("_ready", "_scheduled"))


def run(coro):
    """Run `coro` to completion on a fresh virtual-time loop; returns its result."""
    loop = VirtualLoop()
    real = time.monotonic
    time.monotonic = loop.time
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        time.monotonic = real
        asyncio.set_event_loop(None)
        loop.close()
