# AI Camera Director — Beta Research & Design Proposal

**Status:** Proposed design, not implemented or hardware-validated\
**Date:** 2026-11-21\
**Target branch:** `beta/ai-camera-director`\
**Main hardware assumption:** Pixel 10 Pro XL supplies the single camera; robotic camera mount is driven through the already-working MCP/NormaCore-style servo integration. Verify mounting and motor feedback in the existing project before commanding movement.

## Goal

Add contextual viewpoints, user-selected object tracking, intelligent local viewpoint improvement, and smooth cinematic shots on top of the existing functioning voice transcription -> Claude -> MCP fixed-position system (`topview`, `right`, etc.). Preserve current working behavior. Build vertical slices; test every slice end-to-end; do not implement uncertain motion behavior without asking.

## Non-negotiable constraints

- **One camera:** Pixel only. Do not add or require the robot's built-in cameras or camera-to-camera calibration.
- **Phone-first:** Flutter app stays open in foreground, shows the live preview, local status, target overlay, recording and microphone status. On-device camera processing, wake word, transcription and high-quality local recording continue as already designed.
- **Compressed live feed:** Low-bitrate WebRTC for dashboard/monitoring. Do not stream the 4K master recording through WebRTC. Save original-quality recordings on the Pixel; transfer later.
- **Existing integrations:** Preserve named MCP presets and working voice commands. New autonomous modes should be additive and feature-flagged.
- **Control roles:** Claude plans high-level camera actions. A local, deterministic controller performs every fast servo correction; Claude is never called at video frame rate or entrusted with unbounded joint velocities.
- **Robot hardware:** Run the existing hardware-access service wherever USB/Station currently lives. The PC dashboard remains a browser UI; a lightweight motor driver/bridge is not a computer-vision or model-processing backend.
- **Offline boundary:** The visual tracker and the local voice pipeline can be offline. If Claude is accessed through its hosted API, Claude-based planning or vision is **not fully offline**; label it honestly. Without Claude connectivity, local tracking, preset recall and recording should remain available.
- **No assumed 3D:** A camera-only RGB image plus joint readings does not automatically give exact object depth, collision-free trajectories or full object visibility.
- **User approval:** Ask about meaningful ambiguities, especially whether the Pixel is on the ElRobot's end effector versus a pan/tilt mount, safe joint ranges, collision clearance and whether Claude may receive snapshots.

## Approaches and trade-offs

1. **Image-centering PID only:** Easiest. Keeps a target in frame but cannot discover a better viewpoint or reveal an occluded side.
2. **Selected-object tracking + local joint-space viewpoint survey + closed-loop image correction (RECOMMENDED):** Achievable with existing MCP presets and motor state. Finds and recalls better observed views. Does not require full camera extrinsics or object reconstruction to rank observed poses.
3. **3D target localization + inverse kinematics + collision-aware orbit:** Stronger geometry and cinematic motion, but requires camera-to-robot calibration, reachability, accurate arm model, safe-path planning and potentially depth or multiview reconstruction. Future stretch goal.

## Architecture

```text
PIXEL 10 PRO XL (foreground Flutter app)
  Camera native single owner / timestamps
       |-- high-quality HEVC master recording -> phone storage
       |-- downscale + hardware H.264 encoder -> WebRTC -> PC dashboard
       `-- local analysis frames (latest-frame-only) -> track target, visibility metrics
                                                   -> target observations + camera timestamps
  Microphone -> local wake-word + local ASR -> existing Claude intent integration
  Target selection <- local authenticated session channel <- browser rectangle selection
  Local high-level director -> camera tracking / scan / best-view / cinematic state machine
  Robot targets -------- authenticated low-latency LAN RPC ---------> MOTION BRIDGE

MOTION BRIDGE (co-located with existing NormaCore Station/robot driver)
  Read current joint-state, faults and limits; retain the existing MCP preset implementation
  Enforce joint and speed/acceleration limits, watchdog, side bounds, physical safe regions
  Execute short bounded steps / safe trajectories and report measured positions
  MCP high-level tools are a separate planning entry point, not the servo control loop

CLAUDE (existing integration)
  Infrequent: understand user request, optionally inspect selected JPEG keyframe,
  choose target/intent and call high-level camera-director MCP tools.
  NEVER: every-frame inference, direct unchecked joint-level commands, safety overrides.

WEB DASHBOARD
  Live WebRTC preview; overlay target box/mask; selecting a target freezes/anchors a frame
  Modes, progress, score and confidence; candidate-view filmstrip and 'Return to best'
  Operator stop, manual control, recording status and downloaded masters
```

**Data-flow note:** Camera frames used for inference must be taken from the phone's capture/analysis pipeline, not from a re-decoded PC browser video. WebRTC is for monitoring. Use a common camera frame ID and monotonic timestamp; the PC's drawn box references a corresponding phone frame to avoid latency-induced ROI mismatch. Display transforms must account for video rotation, cropping (`cover`), letterboxing (`contain`) and mirroring.

## Prior code / techniques worth reusing

| Source | Role | Engineering note |
|---|---|---|
| https://github.com/HonglinChu/NanoTrack | Primary Android experiment for arbitrary box-initialized object tracking | Apache-2.0; NCNN Android demo; benchmark on Pixel rather than assuming repo FPS. |
| https://github.com/chayanforyou/OpenCvObjectTracking | Classical Android ROI tracking UI / CSRT/KCF prototype | Verify OpenCV contrib binary and tracking quality during camera viewpoint changes. |
| https://github.com/facebookresearch/EdgeTAM | Later on-device box/prompt segmentation + tracking | Apache-2.0, reported ~15.7 FPS iPhone 15 Pro Max; mobile port not plug-and-play on Pixel. |
| https://github.com/google-ai-edge/mediapipe | Optional hand landmarks to reason about 'what I am working on' | Do not pretend hand proximity proves object identity. |
| https://github.com/S-SAMRINA/Find_the_viewpoint | Closest reference for reachable viewpoint search and refinement | UR5e and sophisticated vision/IK: adapt concept, not the whole dependency stack. |
| https://github.com/ethz-asl/active_grasp | Active next-best-view / occlusion-aware viewpoints | Reuses idea of sensing and iterating rather than guessing one pose. |
| https://github.com/lagadic/visp | Image-based visual servoing math/control concepts | GPL-2.0: review license before incorporating code into an incompatible project. |
| https://github.com/pantor/ruckig | Smooth jerk-constrained state-to-state trajectories | MIT. Community multi-intermediate waypoint feature invokes cloud: avoid for offline pipeline. |
| https://github.com/facebookresearch/sam2 | Advanced GPU/video segmentation reference | Heavy default; do not make it a Phase-1 phone dependency. |
| https://github.com/norma-core/norma-core | Actual ElRobot / Station reference | Validate current `st3215/inference` motor feedback and `commands` contract in installed version. |
| https://github.com/tejavadlamudi03/norma-robot | Example of MCP and Station safety clamps | Study safety and actual telemetry, not necessarily its task model. |

**Native mobile alternatives:** Android ML Kit Object Detection & Tracking provides on-device video tracking IDs for automatically detected objects, but it is not a drop-in tracker for an arbitrary user-drawn ROI. The Flutter `google_mlkit_object_detection` package is useful for auto-detect mode. iOS Vision `VNTrackObjectRequest` directly supports a manually initialized bounding box. Keep a common `TargetTracker` interface over platform-native implementations.

## State machine and priorities

```text
DISCONNECTED -> IDLE -> TARGET_SELECTED -> TRACKING
                         |                |
                         v                v
                       SURVEY          TARGET_LOST
                         |                | -> HOLD, ask to reacquire
                         v
                     BEST_VIEW_FOUND -> LOCAL_REFINE -> FRAMED -> IDLE/TRACKING

TRACKING or FRAMED -> CINEMATIC -> FRAMED
ANY MODE -> MANUAL_OVERRIDE or STOP -> HOLD
```

Priority: emergency stop > physical joint and power safety > manual override > tracking/cinematic > Claude requests. Only one motion mode may own the robot at a time. Each command gets a `command_id`, mode lease and bounded TTL; duplicate and stale commands are rejected.

## Target selection and perception

1. User draws a box around an object in the PC dashboard or Pixel UI; the selected **actual frame** is paused/identified.
2. Browser sends normalized `[x1,y1,x2,y2]` and `frame_id`, camera lens ID, rotation/mirror metadata; phone initializes the local tracker against the same buffered capture frame.
3. Tracker emits bounding box, confidence, object ID and timestamp at a measured, sustainable rate (e.g. 10-30 Hz target, not guarantee).
4. A cheap local quality assessor computes image position, occupied area, edge clearance, sharpness, segmentation completeness if available, motion blur and tracking confidence.
5. If tracking confidence falls below an empirically tuned threshold for several frames, stop automated motion and enter `TARGET_LOST`. Never continue using a stale box or quietly swap to a similar object.
6. Optional `auto-select` begins with one Claude snapshot (or entirely local detected proposals), but asks the user to confirm ambiguous objects; 'the thing I'm working on' may use MediaPipe hand proximity as a clue, not ground truth.

## Closed-loop target framing (visual servoing)

For target center `(cx, cy)` normalized to 0..1 and desired center `(tx, ty)`, define pixel error `e=[cx-tx, cy-ty]`.

**Initial minimum:** make small, bounded changes to one or two relevant motor axes, observe whether the box moves in the intended image direction, and tune a damped proportional controller plus deadband and rate limits. Test sign/direction with the robot stationary at a safe location.

**Better:** estimate local image Jacobian from measurements: `delta_image ~= J * delta_joint`. Use a few safe joint perturbations, solve a regularized least-squares inverse for `delta_joint`, and constrain each step by motor and geometry limits. Estimate only in the local neighborhood; camera perspective and target distance change the Jacobian. Do not confuse repositioning a target in the image with achieving a 3D camera orbit.

The robot's low-level Station-side loop enforces limits and executes trajectories. Phone sends slower, fresh target offsets; the bridge drops stale updates and holds on disconnect. Servo feedback uses measured state, not just the last commanded target.

## View memory and best-angle search

Goal: move from a named side/preset to **a better view on the same side**, not across unplanned reachable space.

1. Start from the current known-safe side preset, e.g. `right`.
2. Define an explicit allowed joint-space neighborhood/whitelist of safe waypoints for that side. Limit exploration to small incremental moves along approved axes.
3. Sample a coarse grid or short sweep. At each candidate: move safely, allow camera vibration to settle, collect multiple frames, use a robust median score, read **actual** joint angles, and save a thumbnail/keyframe and metadata.
4. Track a persistent selected target; do not rank candidate frames if target identity is uncertain or out of view.
5. Score target quality against the task. For 'more in frame', optimize centeredness, target size near a **desired occupancy range** (not maximum area), margin from all crop edges, sharpness, and confirmed target visibility. For 'show the other side', favor diverse object appearance but still require safe reachability; RGB alone does not prove that the unseen backside became visible.
6. Select the highest valid candidate; optionally refine a few small steps around it using the observed local Jacobian or a coordinate search (coarse -> fine).
7. Return to the selected **measured** joint state through the predefined safe route; settle; verify the new camera image still scores as expected. If not, reframe within allowed bounds or hold for operator decision.
8. Keep a per-side session catalogue of `Top 3 views` and explicit failure reasons.

A simple scoring starting point (all components normalized to [0,1], adjust after test):

`Q = 0.25*composition + 0.25*framing + 0.20*visibility + 0.15*sharpness + 0.15*tracking_reliability`

The composition term measures 2D center and intentional off-center framing. Framing penalizes targets that are tiny or clipped. Visibility is provisional if only a bounding box exists; a segmentation mask and temporal occlusion cues improve it. Safety/side eligibility is a hard filter, not a term that can be traded away for a better picture.

**Suggested view record:**

```json
{
  "id": "view-0032",
  "session_id": "shoot-001",
  "preset_region": "right",
  "lens_id": "rear-main",
  "camera_frame_id": 19487,
  "camera_timestamp_ns": 8234200382,
  "robot_timestamp_ns": 8234221941,
  "actual_joint_positions": [0.44, 0.63, 0.51],
  "target_id": "selected-object-1",
  "bbox_normalized": [0.29, 0.22, 0.69, 0.73],
  "track_confidence": 0.91,
  "view_score": 0.86,
  "thumbnail_uri": "local://view-0032.jpg",
  "motion_path_id": "safe-right-local-survey-1"
}
```

The sample joint array is **illustrative only**: actual ElRobot state likely has more joints and uses hardware-specific normalized or raw units. Keep source units and calibration metadata; never execute this example.

## Cinematic mode: first a safe sequence, later a true orbit

**MVP cinematic:** Play a small user-approved path through known-safe camera viewpoints, interpolate each state-to-state segment smoothly (with dwell/hold at endpoints), and continuously verify the target remains in the frame. Record full quality locally. If framing degrades, slow/hold and perform only small allowed reframe corrections; don't inject large sudden joint changes into a cinematic shot.

**Later true target orbit:** Requires the Pixel to physically translate around the object, not just rotate in place. With a pan/tilt mount one can pan, tilt and reframe, but cannot reveal another side. On an ElRobot arm, a 3D orbit needs known approximate target location, camera-to-end-effector transform, IK, collision/reachability checks and safe camera-mount model. Keep this a separate experimental slice.

## Claude planning and MCP contract

High-level tools (names illustrative; integrate with existing namespace conventions):

- `camera_snapshot` -> JPEG keyframe and selected-target metrics; MCP tools can return image content.
- `select_target` -> require clear ROI or confirmed object hypothesis.
- `start_tracking({target_id, framing})` -> returns job ID.
- `survey_view({target_id, region_id, max_steps})` -> only a preapproved safe region.
- `refine_best_view({job_id, radius})` -> bounded local improvement.
- `recall_view({view_id})` -> validates route and moves through bridge.
- `start_cinematic({target_id, path_id, duration_s})` -> safe predefined path; no free-form raw joint trajectories.
- `stop_camera_motion()` -> takes priority over all jobs.
- `get_camera_director_state()` -> mode, confidence, score, position, alarms.

**No MCP tool should permit Claude to send unvalidated, unbounded raw joint targets.** Existing fixed-pose tools continue to work. Claude receives only event summaries, a small number of deliberate snapshots (e.g., before action, after survey, on ambiguity), and the final view catalogue; not a continuous stream of video frames.

## Safety and robot integration

- Audit existing `topview`, `right` etc. MCP tools and verify whether their servo values are calibrated positions or merely targets. Preserve and reuse their known-safe paths.
- Check whether the Station motor feedback stream `st3215/inference` exists and what its fields/units mean in the installed version; distinguish commanded, measured, stale and faulted data.
- Keep joint-space limits, max per-step delta, speed/acceleration/jerk budget and timeout at the hardware bridge layer. Reserve workspace clearance around the phone and nearby hands/tools.
- Predefine safe side regions/waypoint sets and reject all candidates not in them. Joint limits alone **do not** prove collision avoidance.
- The Pixel 10 Pro XL itself weighs **232 g** before any mount/cable; verify real mechanical payload, cantilever torque and servo load before placing it at the end of a lightweight ElRobot arm (Google Pixel hardware specs: https://support.google.com/pixelphone/answer/7158570?hl=en).
- Require explicit manual control / large Stop button in phone and dashboard; any user interaction overrides autonomous mode.
- Hold on stale robot telemetry, stale target frames, target loss, high servo load/current/temperature, network loss or an unreachable/colliding target. Physical E-stop capability remains the final safety boundary.
- If the Pixel changes lens, zoom, digital stabilization behavior, mounting orientation or camera resolution, invalidate the image-to-motor Jacobian and related view scores until revalidated.

## Dashboard design changes

The existing professional dark dashboard keeps live WebRTC video, with a canvas overlay mapped to the true frame coordinates (not the CSS box). New controls: `Select target`, `Track`, `Improve angle`, `Survey this side`, `Best views`, `Cinematic`, `Pause`, prominent `STOP`.

Right sidebar: `Target lock` and confidence, current preset region, frame score, area %, edge clipping, tracking FPS, robot actual vs commanded joint state, motor fault and network age. View-history strip with before/after thumbnails and measured pose. During survey show status `candidate 3/8`, `best so far 0.84` rather than hiding progress. The phone screen shows the same selected target and mode, plus recording master status; no PC inference is required.

## Vertical slice plan (each independently demonstrable)

### Slice 0 — Inventory and safe beta isolation

- Create beta branch/worktree from current functioning main, preserve all existing workflows, add feature flag `AI_CAMERA_DIRECTOR=false` by default.
- Inspect actual MCP tools, motor IDs/ranges, joint telemetry and hardware mount. Agree safe ranges and operator stop before issuing autonomous commands.
- Build deterministic replay fixtures for representative camera streams and recorded motor states.
- **Gate:** Existing voice `topview/right` functionality still passes with feature disabled; no robot movement from disabled feature.

### Slice 1 — Select and track without motion

- Browser draws an ROI on an anchored WebRTC frame; phone receives normalized ROI + frame ID and starts local tracker. Draw matched overlay on both phone and web dashboard.
- Prototype on Android using NanoTrack/NCNN versus classical OpenCV CSRT with realistic occlusion and moving-camera footage. Choose on measured Pixel latency/robustness. iOS interface remains pluggable (Vision tracker candidate).
- **Gate:** target maintains a stable ID and bounding box on realistic camera movements; loss stops tracking; correct coordinate mapping for orientation, aspect ratio and mirrored views; master recording unaffected.

### Slice 2 — Servo readback and safe tiny correction

- Wire existing NormaCore motor state readback to the phone (or the bridge) with timestamps and units. Add a motion job lease with watchdog and manual override.
- From one safe preset, perform small, signed, bounded axis perturbations; estimate image response and center selected target without losing it.
- **Gate:** correct steering direction with logged `delta image / delta joint`; no overshoot into edge/limit; hold on stale data; stop works during every movement.

### Slice 3 — Discover and recall the best observed view

- Enumerate ~5–10 operator-approved local candidate viewpoints around the current region only. At each stop capture several frames, score composition/visibility and save actual joint state + thumbnail.
- Rank, return to chosen joint state and verify image score after settling.
- **Gate:** one click/voice call `improve angle` consistently produces a documented improvement or a transparent `no better reachable view` result; never moves to another side without permission.

### Slice 4 — Contextual commands via Claude

- Add MCP high-level tools and snapshot/scene summary. Claude interprets `focus on the object I'm working on` as `select/ask -> survey/refine -> frame` and uses existing preset vocabulary.
- **Gate:** a short voice demo goes from user command to validated camera shot; ambiguous target gets a question rather than an arbitrary dangerous motion.

### Slice 5 — Smooth cinematic mode

- Author at least one safe 3–5 pose camera path, generate bounded state-to-state motions, pause/hold on lost tracking, and keep full-quality Pixel recording uninterrupted. Prefer repeatability over a speculative 3D orbit.
- **Gate:** visually smooth playback without sudden jumps, target in frame for the majority of the clip, operator stop, clip file verified on phone.

### Optional Slice 6 — Advanced object masks and semantic selection

- Integrate EdgeTAM/interactive segmentation if measured Pixel throughput supports it, optional hand-landmark association, and possibly learned target reacquisition. Retain a classical fallback.
- **Gate:** objective improvement on baseline real clips; no critical FPS, thermal, recording or battery regression.

## Testing matrix / observable thresholds

Initial **proposed** acceptance targets; calibrate to real camera, motor speeds and wireless setup rather than promise these figures:

- Phone video still streams (e.g. 640x360 30 FPS) with no increasing queuing delay while tracker runs and 4K recording is enabled (supported combination must be tested).
- Tracked target update rate target >= 10 Hz on Pixel; end-to-end camera-to-target-box age tracked and displayed.
- More than 80% of frames valid over a 30-s camera movement test with an unobstructed distinct object. Explicitly log degraded/lost cases, not fake confidence.
- Starting from a safe preset, target settles within +/- 0.07 normalized screen coordinates of desired center on a stationary test target, with no sustained oscillation (adjust tolerance to motor hardware).
- Survey uses only allowed joint positions; score improvement is measured on held-out repeat captures and never traded for physical limits.
- Replay identifies stale-data stop, tracking loss hold, conflicting motion modes and manual override within one local controller cycle.
- At least three before/after examples and one 15–30 s safe cinematic recording; all original-quality masters stored on Pixel.

**Record:** camera capture timestamps, received packet age, inference duration, box/mask confidence, per-view scores, commanded and measured joints, motor temperature/current (if available), command rates, frame drops and stop reasons. This evidence is the most valuable demo material if autonomous motion is not fully reliable.

## Engineering style for Claude / coding agents

- Most coding in **Sonnet**: independent bounded Flutter UI, mobile tracking wrappers, telemetry schema, test fixtures, dashboard overlays, score functions, motor bridge tests; parallel work only with non-overlapping files.
- Reserve larger reasoning models for joint constraints, uncertain motion kinematics, camera-to-motor Jacobian design and critical safety review. Return implementation to Sonnet afterward.
- One vertical slice at a time; test on physical Pixel and robot; do not call a mock-only test a completed feature.
- Preserve working baseline. No autonomous motion until after motion limits, motor feedback and user-approved test workspace are verified.
- If a requirement or API is unclear, ask the user, especially about mounting geometry, allowed region, and role of cloud Claude. Never silently invent robot kinematics or adjust physical safety bounds.
- Do not pull in large GPU-only repos as required phone dependencies. Use them as reference implementations where appropriate.
- Cite the exact prior work or docs for copied algorithms; review licenses before reuse (ViSP is GPL-2.0, NanoTrack and EdgeTAM Apache-2.0, Ruckig MIT).

## Critical questions to resolve before Slice 2 movement

1. Is the Pixel attached to the moving **end effector of the ElRobot arm**, or a separate 2-axis pan/tilt system? This determines whether 'see the other side' is even possible.
2. What are the actual motor axes, known-safe preset positions, joint readback quality, range and reachable collision-free workspace?
3. Where is NormaCore Station/robot driver running and can the Pixel contact its local command/telemetry bridge from the current hotspot topology?
4. Is Claude allowed to receive camera snapshots via its API, or should semantic target selection be fully offline? Do not call a Claude API based mode completely local.

## Source links

- https://github.com/norma-core/norma-core
- https://github.com/HonglinChu/NanoTrack
- https://github.com/chayanforyou/OpenCvObjectTracking
- https://github.com/facebookresearch/EdgeTAM
- https://github.com/S-SAMRINA/Find_the_viewpoint
- https://github.com/ethz-asl/active_grasp
- https://github.com/lagadic/visp
- https://github.com/pantor/ruckig
- https://github.com/facebookresearch/sam2
- https://github.com/IDEA-Research/Grounded-SAM-2
- https://github.com/tejavadlamudi03/norma-robot
- https://developers.google.com/ml-kit/vision/object-detection/android
- https://developer.apple.com/documentation/vision/vntrackobjectrequest
- https://developer.android.com/media/camera/camerax/analyze
- https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx
