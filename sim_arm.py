"""Stand-in for station_arm.Arm with no hardware: joints glide toward their goals.

Lets the director, voice and overlay run end to end before the real arm is ready:
    python director.py --sim
"""

import asyncio
import time

SIM_RANGES = {1: (700, 3400), 2: (800, 3200), 3: (900, 3100), 4: (800, 3300), 5: (0, 4095), 6: (1900, 3200)}


class SimArm:
    bus_serial = "SIM"

    def __init__(self, start: dict[int, int] | None = None, speed: float = 1500):
        self._pos = {m: float(p) for m, p in (start or {m: 2048 for m in SIM_RANGES}).items()}
        self._goal = dict(self._pos)
        self._speed = speed  # steps/s
        self._t = time.monotonic()
        self.torque = True
        self.writes = 0

    @classmethod
    async def connect(cls, *args, **kwargs) -> "SimArm":
        return cls()

    def _advance(self):
        now = time.monotonic()
        step = self._speed * (now - self._t)
        self._t = now
        if not self.torque:
            return
        for m, g in self._goal.items():
            d = g - self._pos[m]
            self._pos[m] += max(-step, min(step, d))

    def joints(self, max_age_s: float = 0.5) -> dict[int, int]:
        self._advance()
        return {m: int(round(p)) for m, p in self._pos.items()}

    def ranges(self) -> dict[int, tuple[int, int]]:
        return dict(SIM_RANGES)

    async def set_torque(self, enable: bool, motor_ids=None):
        self._advance()
        self.torque = enable

    async def write_goals(self, goals: dict[int, int], speed=None, accel=None):
        self._advance()
        self.writes += 1
        self._goal.update({m: float(g) for m, g in goals.items()})

    async def settled(self, tolerance: int = 8, timeout_s: float = 3.0) -> dict[int, int]:
        await asyncio.sleep(0.1)
        return self.joints()

    def close(self):
        pass
