"""Thin wrapper around the NormaCore Station Python client for one SO-101 arm.

Shared by joints.py, capture_view.py, torque.py and (later) go_to_view.
Positions are raw ST3215 encoder steps, 0-4095 (2048 is roughly centre).
"""

import asyncio
import logging
import struct
import sys
import time
from pathlib import Path

# The vendored SDK uses imports rooted at the norma-core repo root.
sys.path.insert(0, str(Path(__file__).parent / "vendor" / "normacore"))

from software.station.shared.station_py import new_station_client, send_commands  # noqa: E402
from target.gen_python.protobuf.drivers.st3215 import st3215  # noqa: E402
from target.gen_python.protobuf.station import commands as station_commands, drivers  # noqa: E402

logger = logging.getLogger("station_arm")

# ST3215 register addresses (same as norma-core's teleop example).
RAM_TORQUE_ENABLE = 0x28
RAM_ACC = 0x29
RAM_GOAL_POSITION = 0x2A
RAM_GOAL_SPEED = 0x2E
RAM_TORQUE_LIMIT = 0x30
RAM_PRESENT_POSITION = 0x38
RAM_PRESENT_LOAD = 0x3C      # 0-1000 = 0-100% of stall torque, bit 10 = direction
RAM_STATUS = 0x41            # error bits; bit 0 = voltage (often set on a 5 V leader, harmless)
RAM_PRESENT_CURRENT = 0x45   # raw units of 6.5 mA

MAX_ANGLE_STEP = 4095
SIGN_BIT_MASK = 0x8000

SO101_JOINTS = {
    1: "shoulder_pan",
    2: "shoulder_lift",
    3: "elbow_flex",
    4: "wrist_flex",
    5: "wrist_roll",
    6: "gripper",
}


def _normal_position(raw: int) -> int:
    """Strip the sign bit so positions are always 0-4095."""
    if raw & SIGN_BIT_MASK:
        magnitude = raw & MAX_ANGLE_STEP
        return (MAX_ANGLE_STEP + 1 - magnitude) & MAX_ANGLE_STEP
    return raw & MAX_ANGLE_STEP


def _u16(state: bytes, addr: int) -> int:
    if len(state) < addr + 2:
        return 0
    return struct.unpack_from("<H", state, addr)[0]


class Arm:
    """One ST3215 bus on one Station. Keeps the latest inference frame fresh."""

    def __init__(self, client, bus: str = "auto"):
        self.client = client
        self.requested_bus = bus
        self.bus_serial: str | None = None
        self.latest = None
        self.latest_stamp = 0.0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._errors = client.follow("st3215/inference", self._queue)
        self._task: asyncio.Task | None = None

    @classmethod
    async def connect(cls, server: str = "localhost", bus: str = "auto") -> "Arm":
        client = await new_station_client(server, logger)
        arm = cls(client, bus)
        arm._task = asyncio.create_task(arm._drain())
        await arm._wait_for_first_frame()
        arm.bus_serial = arm._resolve_bus()
        return arm

    async def _drain(self):
        while True:
            if not self._errors.empty():
                raise RuntimeError(f"inference stream error: {self._errors.get_nowait()}")
            entry = await self._queue.get()
            if entry is None:
                raise RuntimeError("inference stream closed")
            try:
                self.latest = st3215.InferenceStateReader(entry.Data)
                self.latest_stamp = time.monotonic()
            except Exception:
                logger.exception("failed to decode inference frame")

    async def _wait_for_first_frame(self, timeout_s: float = 10.0):
        deadline = time.monotonic() + timeout_s
        while self.latest is None:
            if self._task.done():
                self._task.result()
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"no st3215/inference frames after {timeout_s}s; "
                    "is the arm plugged in and visible in the Station web UI?"
                )
            await asyncio.sleep(0.05)

    def _buses(self):
        return self.latest.get_buses() or []

    def _resolve_bus(self) -> str:
        serials = [b.get_bus().get_serial_number() for b in self._buses() if b.get_bus()]
        if not serials:
            raise RuntimeError("Station sees no ST3215 bus; check USB and the web UI")
        if self.requested_bus == "any":
            return serials[0]
        if self.requested_bus == "auto":
            if len(serials) == 1:
                return serials[0]
            # Leader + follower: the leader is limp (moved by hand), the follower holds torque.
            torqued = [s for s in serials if self._torque_on(s)]
            if len(torqued) == 1:
                logger.warning("buses %s: using %s (torque on, so the follower); pass --bus to override",
                               serials, torqued[0])
                return torqued[0]
            raise RuntimeError(f"several buses found {serials}; pass --bus <serial>")
        if self.requested_bus not in serials:
            raise RuntimeError(f"bus {self.requested_bus!r} not found, Station sees {serials}")
        return self.requested_bus

    def _bus(self, serial: str | None = None):
        serial = serial or self.bus_serial
        for b in self._buses():
            info = b.get_bus()
            if info and info.get_serial_number() == serial:
                return b
        return None

    def _torque_on(self, serial: str) -> bool:
        bus = self._bus(serial)
        motors = (bus.get_motors() or []) if bus else []
        return bool(motors) and all(
            len(s := bytes(m.get_state())) > RAM_TORQUE_ENABLE and s[RAM_TORQUE_ENABLE] != 0
            for m in motors
        )

    def all_buses(self) -> dict[str, dict[int, int]]:
        """Present positions for every bus Station sees, keyed by serial."""
        out = {}
        for b in self._buses():
            info = b.get_bus()
            if info:
                out[info.get_serial_number()] = {
                    m.get_id(): _normal_position(_u16(bytes(m.get_state()), RAM_PRESENT_POSITION))
                    for m in (b.get_motors() or [])
                }
        return out

    async def settled(self, tolerance: int = 8, timeout_s: float = 3.0) -> dict[int, int]:
        """Joints once the arm stops moving (follower lags the leader a little)."""
        deadline = time.monotonic() + timeout_s
        prev = self.joints()
        while True:
            await asyncio.sleep(0.25)
            cur = self.joints()
            if all(abs(cur[m] - prev.get(m, cur[m])) <= tolerance for m in cur):
                return cur
            if time.monotonic() > deadline:
                logger.warning("arm still moving after %.0fs, capturing anyway", timeout_s)
                return cur
            prev = cur

    def joints(self, max_age_s: float = 0.5) -> dict[int, int]:
        """Present position per motor id, from the latest frame."""
        age = time.monotonic() - self.latest_stamp
        if age > max_age_s:
            raise RuntimeError(f"joint data is stale ({age:.1f}s old)")
        bus = self._bus()
        if bus is None:
            raise RuntimeError(f"bus {self.bus_serial} missing from latest frame")
        return {
            m.get_id(): _normal_position(_u16(bytes(m.get_state()), RAM_PRESENT_POSITION))
            for m in (bus.get_motors() or [])
        }

    def telemetry(self, max_age_s: float = 0.5) -> dict[int, dict]:
        """Per motor: present/goal position, load (0-1000), current (raw), status bits, torque flag."""
        age = time.monotonic() - self.latest_stamp
        if age > max_age_s:
            raise RuntimeError(f"joint data is stale ({age:.1f}s old)")
        bus = self._bus()
        if bus is None:
            raise RuntimeError(f"bus {self.bus_serial} missing from latest frame")
        out = {}
        for m in bus.get_motors() or []:
            st = bytes(m.get_state())
            out[m.get_id()] = {
                "pos": _normal_position(_u16(st, RAM_PRESENT_POSITION)),
                "goal": _normal_position(_u16(st, RAM_GOAL_POSITION)),
                "load": _u16(st, RAM_PRESENT_LOAD) & 0x3FF,
                "current": _u16(st, RAM_PRESENT_CURRENT) & 0x7FFF,
                "status": st[RAM_STATUS] if len(st) > RAM_STATUS else 0,
                "torque": len(st) > RAM_TORQUE_ENABLE and st[RAM_TORQUE_ENABLE] != 0,
            }
        return out

    def ranges(self) -> dict[int, tuple[int, int]]:
        """Calibrated (min, max) per motor id, as Station reports them."""
        bus = self._bus()
        return {
            m.get_id(): (m.get_range_min(), m.get_range_max())
            for m in (bus.get_motors() or [])
        } if bus else {}

    def _sync_write(self, address: int, values: dict[int, bytes]):
        cmd = st3215.Command(
            target_bus_serial=self.bus_serial,
            sync_write=st3215.ST3215SyncWriteCommand(
                address=address,
                motors=[
                    st3215.ST3215SyncWriteCommand_MotorWrite(motor_id=mid, value=val)
                    for mid, val in sorted(values.items())
                ],
            ),
        )
        return station_commands.DriverCommand(
            type=drivers.StationCommandType.STC_ST3215_COMMAND,
            body=cmd.encode(),
        )

    async def set_torque(self, enable: bool, motor_ids: list[int] | None = None):
        ids = motor_ids if motor_ids is not None else sorted(self.joints())
        value = b"\x01" if enable else b"\x00"
        await send_commands(self.client, [self._sync_write(RAM_TORQUE_ENABLE, {mid: value for mid in ids})])

    async def write_goals(self, goals: dict[int, int], speed: int | None = None, accel: int | None = None):
        """Goal positions (0-4095) in one pack; speed/accel go first so they apply to this move."""
        pack = []
        if speed is not None:
            pack.append(self._sync_write(RAM_GOAL_SPEED, {mid: speed.to_bytes(2, "little") for mid in goals}))
        if accel is not None:
            pack.append(self._sync_write(RAM_ACC, {mid: bytes([accel]) for mid in goals}))
        pack.append(self._sync_write(RAM_GOAL_POSITION, {mid: g.to_bytes(2, "little") for mid, g in goals.items()}))
        await send_commands(self.client, pack)

    def close(self):
        if self._task:
            self._task.cancel()
        self.client.close_connection()


def run(coro):
    """asyncio.run for our CLI scripts: clean message instead of a traceback."""
    try:
        asyncio.run(coro)
    except (KeyboardInterrupt, EOFError):
        pass
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def gripper_id(motor_ids) -> int:
    """The gripper is the last motor: 6 on an SO-101, 8 on a 7+1 DoF ElRobot."""
    return max(motor_ids)


def format_joints(joints: dict[int, int]) -> str:
    if len(joints) == len(SO101_JOINTS):
        return "  ".join(f"{SO101_JOINTS.get(mid, mid)}={pos}" for mid, pos in sorted(joints.items()))
    grip = gripper_id(joints)
    return "  ".join(f"{'gripper' if mid == grip else f'j{mid}'}={pos}" for mid, pos in sorted(joints.items()))
