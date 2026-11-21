"""Stand-in for station_arm.Arm with no hardware: an 8-motor ElRobot whose joints glide toward
their goals. Lets motion, the web UI, the director and voice run end to end without the arm:

    python capture_server.py --sim        uv run python director.py --sim

Faults can be injected for tests: `stuck` (motors that don't move), `stale` (no fresh data),
`fail_writes`, `mirror` (pretend Station is mirroring a leader onto this arm), `load` (pretend
PWM load per motor), `hijack` (another writer keeps overwriting these goals) and `echo_delay`
(written goals show up in telemetry late, as they do on the real bus).

By default a joint glides to its goal at its speed ceiling with no lag, which is optimistic.
`SimArm(realistic=True)` makes it answer like the real follower did on 21 Nov (see
docs/real-session-2026-11-21-motion_log.jsonl): speed is proportional to the distance from the
goal (`gain` per second, so a streamed goal is followed with a lag of speed / gain), limited by
the speed register and by the acceleration register (units of 100 steps/s^2), and the loaded
shoulder rests `sag` steps away from its goal. Timing only: no gravity, no load or current.
"""

import asyncio
import math
import time

# Follower 5B79016329 calibration, snapshot from docs/HARDWARE.md.
SIM_RANGES = {1: (1105, 2991), 2: (1071, 3025), 3: (924, 3172), 4: (897, 3199),
              5: (141, 3955), 6: (1017, 3079), 7: (164, 3932), 8: (1263, 2833)}

# Realistic response, fitted to the real follower's streamed moves: it lagged 26-60 steps at
# 100 steps/s and ~77 at 200, i.e. speed = 2.6-3 x position error.
REAL_GAIN = 2.8            # 1/s
REAL_SAG = {2: 20}         # steps a joint rests past its goal (the loaded shoulder: 20-35 measured)
REAL_ACCEL = 15            # acceleration register until a write sets one (motion.SERVO_ACCEL)
_SUBSTEP_S = 0.002


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

    def __init__(self, start: dict[int, int] | None = None, speed: float = 1500, realistic: bool = False,
                 gain: float = REAL_GAIN, sag: dict[int, float] | None = None):
        self._pos = {m: float(p) for m, p in (start or _default_start()).items()}
        self._goal = dict(self._pos)
        self._speed = {m: speed for m in self._pos}   # steps/s ceiling per motor
        self.realistic = realistic                    # lag, acceleration limit and sag (see the module text)
        self.gain = gain
        self.sag = dict(REAL_SAG if sag is None else sag) if realistic else {}
        self._accel = {m: REAL_ACCEL for m in self._pos}   # acceleration register per motor
        self._vel = {m: 0.0 for m in self._pos}            # steps/s, realistic mode only
        self._t = time.monotonic()
        self._torque = {m: True for m in self._pos}
        self.writes = 0
        self.written_ids: set[int] = set()            # every motor id a goal was ever written to
        self.torque_written_ids: set[int] = set()     # ...and every id a torque command went to
        self.stuck: set[int] = set()
        self.stale = False
        self.fail_writes = False
        self.mirror: str | None = None
        self.load: dict[int, int] = {}                # pretend PWM load per motor (0-1000)
        self.hijack: dict[int, int] = {}              # another writer keeps forcing these goals
        self.echo_delay = 0.0                         # seconds until a written goal shows in telemetry
        self._echo: list[tuple[float, dict[int, float]]] = [(0.0, dict(self._goal))]

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
        if self.realistic:
            return self._advance_realistic(dt)
        for m, g in self._goal.items():
            if m in self.stuck or not self._torque[m]:
                continue
            step = self._speed[m] * dt
            self._pos[m] += max(-step, min(step, g - self._pos[m]))

    def _advance_realistic(self, dt: float):
        """Speed follows gain x distance to (goal + sag), capped by the speed register and by what
        can still be braked before the goal; it changes no faster than the acceleration register
        allows. Integrated in small fixed steps so the result does not depend on who reads when."""
        steps = max(1, math.ceil(dt / _SUBSTEP_S - 1e-9)) if dt > 0 else 0
        h = dt / steps if steps else 0.0
        for m, g in self._goal.items():
            if m in self.stuck or not self._torque[m]:
                self._vel[m] = 0.0
                continue
            target, a = g + self.sag.get(m, 0.0), self._accel[m] * 100.0
            pos, vel, top = self._pos[m], self._vel[m], self._speed[m]
            for _ in range(steps):
                err = target - pos
                want = math.copysign(min(top, self.gain * abs(err), math.sqrt(2.0 * a * abs(err))), err)
                vel += max(-a * h, min(a * h, want - vel))
                pos += vel * h
            self._pos[m], self._vel[m] = pos, vel

    def joints(self, max_age_s: float = 0.5) -> dict[int, int]:
        if self.stale:
            raise RuntimeError("joint data is stale (sim)")
        self._advance()
        return {m: int(round(p)) for m, p in self._pos.items()}

    def telemetry(self, max_age_s: float = 0.5) -> dict[int, dict]:
        pos = self.joints(max_age_s)
        # The goal register as telemetry shows it: the newest write that is at least echo_delay old.
        cutoff = time.monotonic() - self.echo_delay
        while len(self._echo) > 1 and self._echo[1][0] <= cutoff:
            self._echo.pop(0)
        shown = self._echo[0][1] if self.echo_delay else self._goal
        return {m: {"pos": pos[m], "goal": int(round(shown[m])), "load": self.load.get(m, 0),
                    "current": 0, "status": 0, "torque": self._torque[m], "temp": 30, "voltage": 12.0,
                    "read_error": "", "stamp": time.monotonic_ns()} for m in pos}

    def ranges(self) -> dict[int, tuple[int, int]]:
        return {m: SIM_RANGES.get(m, (0, 4095)) for m in self._pos}

    async def mirroring_source(self, timeout_s: float = 2.0) -> str | None:
        return self.mirror

    async def set_torque(self, enable: bool, motor_ids=None):
        self._advance()
        for m in (motor_ids if motor_ids is not None else self._pos):
            self._torque[m] = enable
            self.torque_written_ids.add(m)

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
                self._speed[m] = speed[m] if isinstance(speed, dict) else speed
            if accel is not None:
                self._accel[m] = accel[m] if isinstance(accel, dict) else accel
        for m, g in self.hijack.items():
            self._goal[m] = float(g)
        self._echo.append((time.monotonic(), dict(self._goal)))

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
