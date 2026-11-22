# AI Camera Director - Multi-Preset Best View and Cinematic Demo Specification

**Status:** Spec for a beta implementation, **not** a claim of deployed code or proven robot safety.
**Date:** 2026-11-22. **Demo:** 2026-11-23.
**Branch:** `beta/ai-camera-director` created safely from current main (Codex reported `8d06da1`; audit before acting).
**Primary source:** existing moving **robot camera** via current capture + WebRTC/laptop dashboard; phone is not needed.

## Intent

Make an autonomous camera director that can find the Hack Lab sticker from the verified `topview`, frame it, and find an improved shot near **every existing approved named camera position** (examples `topview`, `right` - enumerate actual presets rather than inventing names). The director keeps a best **measured joint pose + image + score** per preset, lets the operator choose the best of them, and builds a conservative cinematic camera sequence from valid shots. Any manually drawn box in the robot-camera dashboard should create an alternative target with precisely the same local tracking, scoring and motion behavior. Claude may start high-level jobs via MCP but is never necessary for a dashboard demo and is never the real-time controller.

## Approach decisions

- **Chosen: local 2D tracking + safe per-region viewpoint survey + measured-pose recall + verified shot playback.** Best quality with a practical demo-sized implementation; reuse current Station/localization/motion code.
- Rejected for demo: generic image-centering only (not a best-view director); full 3D reconstruction plus global collision-aware view planning (too large and needs verified camera extrinsics and collision scene).
- The phrase **'every position' means every existing named preset with an approved camera route/region**, not every mechanically reachable joint state or a promise that an occluded target is visible from every side.
- No extra robot camera calibration is required between streams: use the **verified moving camera** for visual feedback. Fixed external camera may be an additional observer, never silently mistaken for the controllable camera.
- The robot should be controlled by **one local motion owner** using actual measured feedback. The browser and Claude request jobs rather than pushing servo deltas.

## Preserved baseline (reported by user, must verify)

Main at reported commit `8d06da1` has laptop dashboard with Pixel + robot feeds, QR/automatic Pixel pairing, voice started after pairing, phone-master video and separate robot clips in ignored `Camera recordings/`, robot recording tied to tab/feed visibility, always-on arm lift-over, `.env` Claude key with dashboard override, and fixed-position MCP tools. Do not regress or silently redefine these contracts. Robot-only demo must work while Pixel is disconnected and Claude key is absent.

## Data flow and source selection

```text
Robot camera -> existing single capture/producer
                   |----> existing WebRTC -> laptop dashboard & overlay
                   `----> local decoded latest frames -> reference matcher / ROI tracker
                                                           |
                                                target observations + score
                                                           |
                                      local director: PER-PRESET survey + view catalogue
                                                           |
                                       exclusive local motion/safety/telemetry bridge
                                                           |
                                            existing NormaCore Station / ElRobot
                                                           |
                                       measured pose + new camera observation

Dashboard: select ROI, auto-sticker, optimize ALL approved views, cinematic, STOP
Claude MCP (optional): high-level start/status/cancel jobs, never raw joint commands
```

The computer-vision loop does not read frames from a robot clip file or wait for a cloud API. Branch image processing off the already-owned camera producer when possible; do not open a USB camera twice without proving it is supported.

## General target acquisition and identity

- Auto-sticker: in `topview`, match a real reference crop or locally recognize the actual Hack Lab sticker using OpenCV feature matching plus temporal validation. If reference is absent or uncertain, allow a one-time manually confirmed crop; no invented artwork, fixed pixel coordinates or Claude Vision fallback. Store the reference locally. Show what evidence produced a detection.
- Manual generic object: user draws an ROI in the live robot camera feed. Handle `object-fit: contain/cover`, rotation, mirroring, resize, letterbox and video-frame metadata. Bind normalized `[x1,y1,x2,y2]` to correct `camera_source_id` and buffered `frame_id`/timestamp. Allow reselect/cancel.
- **One tracker interface** for both sticker and manual ROI: `initialize(frame, roi, target_id)`, `update(frame)->TargetObservation`, `lost()`.
- Preserve target identity across preset changes by **reacquiring at each settled pose** from saved target reference/keyframes, validated features and confidence. Do not assume CSRT or another short-term tracker will work across a large camera movement. If identity cannot be verified, mark that preset `TARGET_NOT_VISIBLE` and skip it.
- Local tracker suggestions: OpenCV CSRT/KCF plus feature matching/ORB/AKAZE first; heavier ONNX/segmentation only if measured need. Use fresh timestamped frames, multi-frame consistency and explicit target-lost behavior.

## Per-preset best-view search (critical)

1. Enumerate **actual** existing approved camera presets, their approach/exit routes, safe local search neighborhoods and movable camera source. No newly invented preset joint arrays.
2. User chooses `All approved views` or a subset. Start from a known safe existing preset path; use the existing lift-over logic only after checking full route clearance.
3. At each preset: arrive and settle, reacquire target, record baseline score and measured joints, then search a small **preapproved** local neighborhood of candidates (e.g. operator-reviewed waypoints or safe tiny axis variations). No unrestricted global search.
4. At every candidate: check route/clearance, move with bounded acceleration and stop priority, await feedback + settle, capture several fresh frames, verify target identity, compute robust median quality, store thumbnail + measured joint telemetry + timestamps + quality components.
5. Select the best valid candidate in that region; return along an approved route and **re-evaluate** its score. If that recheck fails, mark it unverified. Keep top N candidate shots per region if useful.
6. Proceed to next preset **only through an approved route**. If the target is unavailable from that preset, show `SKIPPED: target not visible` and do not force visibility via unsafe moves.
7. The output is a catalogue like `{preset_id, status, best_measured_pose, score, thumbnail, safe_route_id, target_id, camera_source_id, pose_timestamp, verified_at}` for every visited or skipped preset. Show `best per position` in the dashboard.
8. `Recall best` may move only if the stored pose, robot configuration, camera and route are still valid; verify the image on arrival. Re-scan or ask instead of executing stale poses.

### Score (proposed, tune on repeatable footage)

Hard gates: correct target, fresh frame, measured stable pose, permitted route, verified table clearance. These are not weighted rewards.

Within the gates, score quality on [0,1] using:

`Q = .30*composition + .25*edge_clearance + .20*desired_target_size + .15*sharpness + .10*track_stability`

Use median of several post-settle frames; compute confidence per component. `desired_target_size` favors useful occupancy rather than simply maximizing object area. For a flat sticker, view stability and legibility may matter. For generic 3D objects, a bounding box cannot prove how much of the hidden surface is visible: don't call this score full 3D occlusion coverage. An optional bonus feature may use segmentation or apparent shape/texture diversity with proper validation.

### Local visual correction

For center normalized `c=(cx,cy)` and desired position `d=(dx,dy)`, image error `e=c-d`. Reuse only compatible calibrated transform(s) from current robot code. Table-pixel-to-grasp calibration is NOT automatically the wrist-camera image-to-joint Jacobian. After physical approval, learn local `delta_image ~= J*delta_joint` with tiny bounded, measured perturbations. Apply damped corrections, deadband, settle checks and freshness watchdog. Re-estimate or invalidate when camera, mount, region, target depth or optical crop changes.

## Cinematic mode (high priority, after per-preset verified shots)

- Input: only a subset of `valid` saved best viewpoints from the current session and robot configuration. Sequence follows **existing approved preset-to-preset safe transitions**, not unchecked straight-line interpolation in joint space.
- Capture one satisfying held shot per region. Use local smooth speed/acceleration/jerk constraints where supported on already-approved segments. Between positions, keep the target visible when the verified path permits it; otherwise use a **planned cut or intentional transition** between stable shots instead of claiming an impossible continuous orbit.
- For each destination, settle, reacquire the same object, optionally apply a tiny permitted composition correction, hold shot for capture, and annotate the score/label. STOP or manual override always preempts. On target lost during an unverified portion, HOLD; no blind pursuit.
- Trigger robot recording with **existing semantics** (clips on laptop, tab/feed must stay visible); do not alter how Pixel master recording works. On failure, preserve completed footage and show the exact reason for stopping. If the physical motion path is unapproved, a **shot gallery / compiled still montage** is an honest fallback, labeled not a moving cinematic take.
- A smooth **object-centered true 3D orbit** is a later separate experiment requiring camera extrinsics, IK and verified collision geometry. Do not fake one with yaw-only camera motion.

## Priority demo enhancements after core

1. **One-click Director Tour**: auto-sticker or selected target -> find per-preset best -> show results -> cinematic -> stop/save; available from dashboard without Claude.
2. **Before/after view cards** by preset with baseline/best score, target occupancy and `verified/skipped` indicator.
3. **Shot storyboard** with 3-5 best approved snapshots; select/recall any shot, preview cinematic order.
4. **Live visual diagnostics**: bounding box, desired framing guide, target confidence and age, center error, active mode, per-preset progress, current vs measured joint pose, table/overload/stop status.
5. **Replay demo** for offline development or no hardware, clearly labeled replay; never equate replay with a physically safe robot test.
6. Optional Claude MCP `start_director_tour`, `optimize_preset_views`, `start_cinematic`, `get_director_status`, `stop_director`; all call the *same local jobs* as dashboard. Optional voice is not a dependency.

## Safety, resource & permission gates

**Physical:** audit robot camera mount, real servo limits/units, motor feedback, NormaCore Station/URDF/forward kinematics, table plane, swept geometry of arm/gripper/camera/cables, allowed side-region volumes, preexisting lift-over route, overload and operator stop. These are not supplied by raw joint limits alone. If anything essential is unknown, do not enable motion. No collision safety claims without verified geometry and hardware tests. The operator must be present during first physical moves.

**Concurrency:** exclusive motion lease. STOP > human manual commands > existing presets > local director job > optional Claude high-level planning. Stale camera/joints, target loss, disconnect, fault or expired lease -> HOLD. Use command IDs and bounded TTL.

**Data:** no image/video uploaded to Claude by default; local vision only. Do not commit `.env`, video recordings, reference images containing private content or user datasets by accident. Preserve gitignored `Camera recordings/` behavior.

**Dependencies:** prefer already-installed OpenCV and the current web/robot stack. Evaluate external repositories for ideas; don't transplant other robot's joint values or copyleft source without license review.

## Slice acceptance and graceful downgrades

- Vision works with `CLAUDE_API_KEY` unset, phone disconnected and no voice. Manual ROI works even if sticker match is unavailable.
- No physical motion until the user-visible safety gate is signed off. In dry-run, generate the same job states and candidate scoring on replay, but mark them `SIMULATED`.
- Every named approved preset is **visited, verified or explicitly skipped with reason**; don't declare a tour complete by ignoring unreachable/occluded positions.
- For valid presets, compare before/after scores on post-settle frames and show measured poses. If there is no safe better view, the baseline is retained and reported honestly.
- Cinematic must use validated routes and record/hold appropriate clips, or clearly report `BLOCKED` and provide shot storyboard fallback.
- Existing QR, voice, camera streams, recording, arm controls, presets and API key behavior remain usable. Keep green tests and a manual regression checklist after each slice.

## External references (inspect, do not assume API compatibility)

- NormaCore Station/ElRobot: https://github.com/norma-core/norma-core
- Hackathon pixel-to-joint grid example: https://github.com/Jaseelkt007/Robotics_berlin_hack (e.g. docs/13-grid-control-implemented.md, station_mcp/calibrate.py)
- Alternate local control/feedback: https://github.com/tejavadlamudi03/norma-robot
- Active viewpoint search: https://github.com/S-SAMRINA/Find_the_viewpoint
- Image-based servoing concepts: https://github.com/lagadic/visp
- Smooth trajectory segment concepts: https://github.com/pantor/ruckig
- Local OpenCV tracking: https://docs.opencv.org/4.x/d2/da2/classcv_1_1TrackerCSRT.html

These are research references, not confirmation of what exists inside the current user's repository. The current repo remains the authoritative source of actual modules, calibration, constraints and routes.
