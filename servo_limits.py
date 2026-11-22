"""Raise the ElRobot follower's servo overload threshold (owner decision, 22 Nov): 25 -> 60 on the arm
motors; max torque stays 50 %, and the gripper keeps Station's 25 (as LeRobot's SO-101 follower limits only
its gripper). Station's defaults (norma-core presets.rs: max torque 50 %, overload 25 % for
2 s) tripped motor 2 (status 0x20) whenever the arm reached out.

Station writes its defaults again at every calibration freeze: run this again after a calibration.
Never moves the arm; it writes EEPROM (unlocked, written, locked again), then reads the registers back.

    .venv/bin/python servo_limits.py                # overload threshold 60 on motors 1-7
    .venv/bin/python servo_limits.py --show         # only print the current values
"""
import argparse
import asyncio

from station_arm import Arm, RAM_TORQUE_LIMIT, drivers, send_commands, st3215, station_commands

MAX_TORQUE, OVERLOAD_TORQUE, LOCK = 0x10, 0x24, 0x37


def _cmd(arm, **body):
    return station_commands.DriverCommand(type=drivers.StationCommandType.STC_ST3215_COMMAND,
                                          body=st3215.Command(target_bus_serial=arm.bus_serial, **body).encode())


def show(arm) -> dict:
    out = {}
    for m in sorted(arm._fresh_bus(1.0).get_motors(), key=lambda m: m.get_id()):
        s = bytes(m.get_state())
        out[m.get_id()] = {"max_torque": s[MAX_TORQUE] | s[MAX_TORQUE + 1] << 8, "overload_torque": s[OVERLOAD_TORQUE],
                           "torque_limit": s[RAM_TORQUE_LIMIT] | s[RAM_TORQUE_LIMIT + 1] << 8, "lock": s[LOCK]}
        print(f"motor {m.get_id()}: {out[m.get_id()]}")
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus", default="5B79016329")
    ap.add_argument("--percent", type=int, default=60)
    ap.add_argument("--motors", default="1,2,3,4,5,6,7")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    if not 25 <= a.percent <= 80:
        raise SystemExit("percent must be 25-80")
    arm = await Arm.connect("localhost", a.bus)
    await asyncio.sleep(1.0)
    try:
        if a.show:
            show(arm)
            return
        ids = [int(x) for x in a.motors.split(",")]
        await apply(arm, a.percent, ids)
        now = show(arm)
        bad = [m for m in ids if (now[m]["overload_torque"], now[m]["lock"]) != (a.percent, 1)]
        print("verified" if not bad else f"NOT applied on motors {bad}")
    finally:
        arm.close()


ARM_MOTORS = (1, 2, 3, 4, 5, 6, 7)
OVERLOAD_PERCENT = 60


def needs_apply(arm, percent: int = OVERLOAD_PERCENT, ids=ARM_MOTORS) -> list[int]:
    """Arm motors whose overload threshold is not `percent` (Station wrote its 25 at a calibration)."""
    return [m.get_id() for m in arm._fresh_bus(1.0).get_motors()
            if m.get_id() in ids and bytes(m.get_state())[OVERLOAD_TORQUE] != percent]


async def apply(arm, percent: int = OVERLOAD_PERCENT, ids=ARM_MOTORS):
    """Write the overload threshold to each motor's EEPROM. Never moves the arm."""
    # Station's own EEPROM method (freeze_calibration): unlock, reg_write + action, lock. A reg_write also
    # makes Station drop its cached copy of that motor's EEPROM, so the read-back shows the servo's value.
    for m in ids:
        for cmd in (_cmd(arm, write=st3215.ST3215WriteCommand(motor_id=m, address=LOCK, value=b"\x00")),
                    _cmd(arm, reg_write=st3215.ST3215RegWriteCommand(motor_id=m, address=OVERLOAD_TORQUE,
                                                                   value=bytes([percent]))),
                    _cmd(arm, action=st3215.ST3215ActionCommand(motor_id=m)),
                    _cmd(arm, write=st3215.ST3215WriteCommand(motor_id=m, address=LOCK, value=b"\x01"))):
            await send_commands(arm.client, [cmd])
            await asyncio.sleep(0.15)
    await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
