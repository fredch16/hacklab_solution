"""Stand-in for station_arm.Arm with no hardware: an 8-motor ElRobot whose joints glide toward
their goals. Lets motion, the web UI, the director and voice run end to end without the arm:

    python capture_server.py --sim        uv run python director.py --sim

Faults can be injected for tests: `stuck` (motors that don't move), `stale` (no fresh data),
`fail_writes`, `mirror` (pretend Station is mirroring a leader onto this arm).
"""

import asyncio
import time

# Follower 5B79016329 calibration, snapshot from docs/HARDWARE.md.
SIM_RANGES = {1: (1105, 2991), 2: (1071, 3025), 3: (924, 3172), 4: (897, 3199),
              5: (141, 3955), 6: (1017, 3079), 7: (164, 3932), 8: (1263, 2833)}


def _default_start() -> dict[int, int]:
    """Start at the saved home view (or transit) so moves have a known starting pose."""
    try:
        import motion
        views = motion.load_views()
        for name in ("home", motion.TRANSIT):
            if name in views and set(views[name]) == set(SIM_RANGES):
                return dict(views[name])
    except Exception:
        pass
    return {m: 2048 for m in SIM_RANGES}


class SimArm:
    bus_serial = "SIM"
    is_sim = True

    def __init__(self, start: dict[int, int] | None = None, speed: float = 1500):
        self._pos = {m: float(p) for m, p in (start or _default_start()).items()}
        self._goal = dict(self._pos)
        self._speed = {m: speed for m in self._pos}   # steps/s ceiling per motor
        self._t = time.monotonic()
        self._torque = {m: True for m in self._pos}
        self.writes = 0
        self.written_ids: set[int] = set()            # every motor id a goal was ever written to
        self.stuck: set[int] = set()
        self.stale = False
        self.fail_writes = False
        self.mirror: str | None = None

    @classmethod
    async def connect(cls, *args, **kwargs) -> "SimArm":
        return cls()

    @property
    def torque(self) -> bool:
        """Holding = joints 1-7 energised; the gripper is left limp on purpose."""
        return all(on for m, on in self._torque.items() if m != max(self._torque))

    def _advance(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        for m, g in self._goal.items():
            if m in self.stuck or not self._torque[m]:
                continue
            step = self._speed[m] * dt
            self._pos[m] += max(-step, min(step, g - self._pos[m]))

    def joints(self, max_age_s: float = 0.5) -> dict[int, int]:
        if self.stale:
            raise RuntimeError("joint data is stale (sim)")
        self._advance()
        return {m: int(round(p)) for m, p in self._pos.items()}

    def telemetry(self, max_age_s: float = 0.5) -> dict[int, dict]:
        pos = self.joints(max_age_s)
        return {m: {"pos": pos[m], "goal": int(round(self._goal[m])), "load": 0, "current": 0,
                    "status": 0, "torque": self._torque[m]} for m in pos}

    def ranges(self) -> dict[int, tuple[int, int]]:
        return {m: SIM_RANGES.get(m, (0, 4095)) for m in self._pos}

    async def mirroring_source(self, timeout_s: float = 2.0) -> str | None:
        return self.mirror

    async def set_torque(self, enable: bool, motor_ids=None):
        self._advance()
        for m in (motor_ids if motor_ids is not None else self._pos):
            self._torque[m] = enable

    async def write_goals(self, goals: dict[int, int], speed=None, accel=None):
        if self.fail_writes:
            raise RuntimeError("write failed (sim)")
        self._advance()
        self.writes += 1
        self.written_ids |= set(goals)
        for m, g in goals.items():
            self._goal[m] = float(g)
            self._torque[m] = True          # like the real servo: a goal write energises the joint
            if speed is not None:
                self._speed[m] = speed

    async def settled(self, tolerance: int = 8, timeout_s: float = 3.0) -> dict[int, int]:
        await asyncio.sleep(0.1)
        return self.joints()

    # What capture_server.py asks of station_arm.Arm beyond the motion contract.
    def all_buses(self) -> dict[str, dict[int, int]]:
        return {self.bus_serial: self.joints()}

    def _torque_on(self, serial: str | None = None) -> bool:
        return self.torque

    def close(self):
        pass
