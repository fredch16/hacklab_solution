"""The owner's servo settings for the ElRobot follower (22 Nov), on top of Station's calibration presets
(norma-core software/drivers/st3215/src/presets.rs):

- overload threshold 25 -> 60 on the arm motors 1-7: Station's 25 % for 2 s tripped motor 2 (status 0x20)
  whenever the arm reached out. Max torque stays 50 %; the gripper keeps 25 (LeRobot's SO-101 follower limits
  only its gripper).
- shoulder and elbow (2, 3) with Station's own SO-101 gains P 16 / I 8 / D 32 instead of the ElRobot's P-only
  16 / 0 / 0: a P-only servo holds short of its goal under gravity (the shoulder sagged 69 steps below `top`, more
  than motion's arrival tolerance, and follow saved the sag as its next goal); the integral term removes that.

Station writes its presets again at every calibration freeze; the arm server (capture_server.py) puts these back
within SERVO_CHECK_S. Never moves the arm: EEPROM is written the way Station's freeze does it (unlock, reg_write +
action, lock), then read back.

    .venv/bin/python servo_limits.py          # apply
    .venv/bin/python servo_limits.py --show   # only print the current values
"""
import argparse
import asyncio

from station_arm import Arm, drivers, send_commands, st3215, station_commands

P_COEF, D_COEF, I_COEF, OVERLOAD_TORQUE, LOCK = 0x15, 0x16, 0x17, 0x24, 0x37
ARM_MOTORS = (1, 2, 3, 4, 5, 6, 7)
SETTINGS = [(OVERLOAD_TORQUE, 60, ARM_MOTORS), (P_COEF, 16, (2, 3)), (I_COEF, 8, (2, 3)), (D_COEF, 32, (2, 3))]
NAMES = {P_COEF: "P", D_COEF: "D", I_COEF: "I", OVERLOAD_TORQUE: "overload"}


def _cmd(arm, **body):
    return station_commands.DriverCommand(type=drivers.StationCommandType.STC_ST3215_COMMAND,
                                          body=st3215.Command(target_bus_serial=arm.bus_serial, **body).encode())


def needs_apply(arm) -> list[tuple[int, int, int]]:
    """(motor, register, value) still to write (Station put its preset back at a calibration)."""
    state = {m.get_id(): bytes(m.get_state()) for m in arm._fresh_bus(1.0).get_motors()}
    return [(m, addr, value) for addr, value, ids in SETTINGS for m in ids if m in state and state[m][addr] != value]


async def apply(arm, todo: list[tuple[int, int, int]]):
    """Write each (motor, register, value) to EEPROM. Never moves the arm."""
    for m, addr, value in todo:
        for cmd in (_cmd(arm, write=st3215.ST3215WriteCommand(motor_id=m, address=LOCK, value=b"\x00")),
                    _cmd(arm, reg_write=st3215.ST3215RegWriteCommand(motor_id=m, address=addr, value=bytes([value]))),
                    _cmd(arm, action=st3215.ST3215ActionCommand(motor_id=m)),
                    _cmd(arm, write=st3215.ST3215WriteCommand(motor_id=m, address=LOCK, value=b"\x01"))):
            await send_commands(arm.client, [cmd])
            await asyncio.sleep(0.15)
    await asyncio.sleep(1.0)


def show(arm):
    for m in sorted(arm._fresh_bus(1.0).get_motors(), key=lambda m: m.get_id()):
        s = bytes(m.get_state())
        print(f"motor {m.get_id()}: " + ", ".join(f"{NAMES[a]} {s[a]}" for a in (OVERLOAD_TORQUE, P_COEF, I_COEF, D_COEF)))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus", default="5B79016329")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    arm = await Arm.connect("localhost", a.bus)
    await asyncio.sleep(1.0)
    try:
        for _ in range(0 if a.show else 3):   # a read-back can lag Station's EEPROM cache: write what is left again
            if not needs_apply(arm):
                break
            await apply(arm, needs_apply(arm))
        show(arm)
        if not a.show:
            left = needs_apply(arm)
            print("verified" if not left else f"NOT applied: {left}")
    finally:
        arm.close()


if __name__ == "__main__":
    asyncio.run(main())
