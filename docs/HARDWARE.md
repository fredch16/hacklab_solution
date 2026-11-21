# Hardware & Station setup

Everything we found out about the venue hardware on Sat 21 Nov, so nobody has to rediscover it.

## The arms are ElRobots, not SO-101s

Each arm has **8 ST3215 servos** (NormaCore ElRobot, 7+1 DoF), not the 6 of an SO-101.
The code handles both and takes the **last motor id as the gripper** (`station_arm.gripper_id`).

| Motor | Joint (from the ElRobot manual + URDF axes) | Follower calibrated range (steps) |
|---|---|---|
| 1 | base yaw | 1105 – 2991 |
| 2 | shoulder pitch | 1071 – 3025 |
| 3 | elbow pitch | 924 – 3172 |
| 4 | pitch | 897 – 3199 |
| 5 | forearm roll (~336°) | 141 – 3955 |
| 6 | wrist pitch | 1017 – 3079 |
| 7 | wrist roll (~340°), camera turns with it | 164 – 3932 |
| 8 | gripper (follower) / trigger (leader) | 1263 – 2833 |

The joint names for 2–4 are read from the URDF axes; the manual has no text naming them. Positions are raw encoder steps, **4096 per turn (0.088°/step)**. After calibration Station centres each arc around ~2048. Ranges come live from Station (`Arm.ranges()`); the table is a snapshot from 13:20.

## Which arm is which

| Serial | Role | Notes |
|---|---|---|
| **5B79016329** | **Follower**: carries the camera, the arm our code drives | `--bus 5B79016329` |
| **5B61033001** | **Leader**: moved by hand, handle + trigger instead of a gripper | 5 V supply, so Auto Calibrate is greyed out on it |

Each arm has **one servo-board USB cable + one camera USB cable**, plus its own power supply (don't swap the supplies, the voltages differ).

## Cameras

Two "KD-USB Cameras". In Station they show up as queues `usbvideo/<md5 of camera id>`:

| Queue | Device | State |
|---|---|---|
| `usbvideo/74e4d17bc903e40ebc78117539fca31f` | `0x21340002…` | **Works**: the follower's gripper camera (jaws visible at the bottom of the frame) |
| `usbvideo/286d912869164356b0e079598e7fcc09` | `0x21330002…` | Black image (lens covered or facing something dark; not investigated) |

`camera.py --list` shows them; the capture UI picks the one with the most detailed picture automatically.
The camera sits on top of the gripper behind the jaws, looking forward, and rolls with motor 7.
**Ask a mentor before touching the mount** (hackathon rule).

## Station

- Version **v0.1.0-beta.10** (`station-macos-arm64.zip` from the norma-core releases), installed in `~/station` on Fred's Mac.
- Start: `cd ~/station && ./station --web --tcp`. Web UI on **:8889**, Python API on **:8888**.
- Our config is in [`config/station.yaml`](../config/station.yaml). It differs from the default in two ways:
  - `usb-video.resize_target: 720`: stored frames are 1280×720 (default 224 px is useless for Claude/video).
  - `usb-video.formats`: a preference list of `{format: "<res>@<fps>fps,<fourcc>"}` objects. Without it, Station picked 320×240. On this Mac the cameras end up capturing 1920×1080 `420v`.
- Station listens on 0.0.0.0, so anyone on the venue Wi-Fi can reach it.

### Station web UI with our layout (:5173)

A patched copy of Station's viewer swaps the two arm panels and rotates the 3D models 90° so the screen matches the desk.
It shows the same live data (it proxies `/api` to :8889). Rebuild instructions: [`patches/README.md`](../patches/README.md).

## Calibration

Both arms were calibrated by hand in Station on 21 Nov (web UI → arm card → **Calibrate** → move every joint slowly to both mechanical stops → **Save**).
Station links leader and follower **by percentage of each joint's calibrated range**, with no offset or invert option, so both arms must hit the same physical stops.

Known issues:
- **Leader M7 (wrist roll) was under-swept** (≈3212 steps vs ≈3768 on the follower), so the follower's wrist sits at a different angle than the leader's. Recalibrating only the leader's M7 to both stops should fix it. If it doesn't, it's an assembly offset and needs our own teleop with a per-joint correction.
- Follower M2 rests ~11 steps below its recorded minimum (Station shows −0.6%). Harmless; `motion.py` widens arcs slightly to cover poses the arm has really been in.

Tips: [calibration best practices](#calibration-best-practices) below, and `python calib_watch.py` shows live per-joint progress.

### Running the same arms from another computer

Do not calibrate again there: a new calibration moves every joint's zero, and every saved view in `views.json` stops being valid. Carry the calibration over instead. It has two halves: each servo's zero offset is stored in the servo and travels with the arm; the calibrated ranges are one small file in Station's data folder, kept in this repo as [`config/station-calibration.wal`](../config/station-calibration.wal) (both arms, from 21 Nov 13:13).

1. Install Station v0.1.0-beta.10 in `~/station` with [`config/station.yaml`](../config/station.yaml), start it once (`./station --web --tcp`) so it creates its data folder, and stop it.
2. `python3 calib_file.py install` (keeps a backup of the file it replaces; `--station-dir` if Station is elsewhere).
3. Start Station, plug in the arms, then `python3 go.py --where`: it must not report any view as "recalibrated since it was captured".

`python3 calib_file.py show --station` compares the repo's copy with the local Station; `export` refreshes the repo's copy after a new calibration. Not yet tried on a second computer: if Station ignores the copied file, its arm card shows the joints as uncalibrated and step 3 fails.

### Calibration best practices

1. Unlink the arms (follower → "(Self-controlled)"); check the page header shows the arm in front of you.
2. **Reset** first when redoing an arm: saved ranges only ever grow.
3. One joint at a time, slowly, to the real mechanical stop in both directions. Gently, don't force.
4. Give each joint room: reposition the others so the table or the arm itself doesn't block it early.
5. M5 and M7 turn almost a full circle: go all the way round both ways, and watch the cables.
6. Every motor must read 0% and 100% at its stops before **Save**.
7. Same routine on both arms.

## Leader → follower mirroring

Follower card → control source → leader's serial. Station runs it at 50 Hz with distance-proportional speed (300–3300 steps/s) and ignores changes under 20 steps.
**Before linking, roughly match the two arms' poses** (especially M5/M7): the follower jumps to the leader's pose immediately and fast.
**Set the follower back to "(Self-controlled)" before running `go.py` / `director.py`**, or Station and our code fight over the motors.

We stopped using the leader to capture views: posing the follower directly (capture UI "Go limp" / "Hold here") avoids the leader's calibration offset.
