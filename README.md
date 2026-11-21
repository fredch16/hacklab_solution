# Local Vision Console — agent handoff

**Status:** implemented Android app and paired browser dashboard. See [current status](docs/STATUS.md) for device evidence and remaining checks.
**Prepared:** 21 November 2026.  
**Primary device:** Google Pixel 10 Pro XL (Android), with iOS as a supported architectural target.  
**Product:** a **foreground Flutter camera-and-voice app** on the phone and a **browser-only monitoring/control dashboard** on a PC. It works on a **local network with the public Internet disconnected**.

## Start here

1. Read [`AGENTS.md`](AGENTS.md) — mandatory behavior for *all* coding agents.
2. Read [`docs/STATUS.md`](docs/STATUS.md) for the current build, test and demo instructions.
3. Read [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
4. Read [`docs/SLICE_PLAN.md`](docs/SLICE_PLAN.md) and the detailed [implementation plan](docs/superpowers/plans/2026-11-21-phone-first-console-implementation.md).
5. Use [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md), [`ui/design-tokens.json`](ui/design-tokens.json), and the [static, nonfunctional UI preview](ui/reference-dashboard.html) as visual direction.
6. Use [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) as the definition of verified work, and record evidence.

## Coding-agent working mode

One agent implements everything itself (no subagents). The owner tests on real hardware. Current progress: [`docs/STATUS.md`](docs/STATUS.md).

## Non-negotiable architecture

```text
Phone — Flutter (always foreground during a session)
  camera ─┬─ high-quality VIDEO + AUDIO recording ─> local phone storage
          ├─ compressed low-res video ── WebRTC ───────────┐
          └─ local camera preview                         │
  mic ────────> single-owner audio capture                │
                 ├─ local recording audio                  │
                 └─ offline KWS -> VAD -> ASR -> events ───┤
  embedded local HTTPS + WSS server ──────────────────────┤
                                                         ▼
                                             PC browser dashboard
                                  final text -> recording intent -> phone RPC
                                  future complex requests -> dashboard MCP client
```

**Only the phone camera** in the MVP. Robot cameras and calibration are **out of scope**. Precise robot control would later require geometric camera-to-robot calibration even with one camera.

The Flutter app lives in [`apps/mobile`](apps/mobile) and the TypeScript browser dashboard lives in [`apps/dashboard`](apps/dashboard). The [architecture](docs/ARCHITECTURE.md#dashboard-command-routing-and-mcp-handoff) defines the boundary for the future MCP integration. The static HTML reference is for design work.

## File map

| File | Purpose |
|---|---|
| `AGENTS.md` | Mandatory agent rules, uncertainties, vertical-slice gates |
| `CLAUDE.md` | Claude entry point; defers to `AGENTS.md` |
| `docs/PRODUCT_SPEC.md` | Scope, product behavior, performance targets, non-goals |
| `docs/ARCHITECTURE.md` | Capture pipeline, native/Flutter boundary, local networking |
| `docs/SLICE_PLAN.md` | End-to-end delivery slices and stopping rules |
| `docs/VIDEO_AUDIO_MODELS.md` | Local recording, streaming, microphone, wake word, ASR |
| `docs/INTERFACES.md` | Proposed messages, state machines and API shapes |
| `docs/SECURITY_AND_TRANSFER.md` | Pairing, LAN-only TLS, file download and integrity |
| `docs/DESIGN_SYSTEM.md` | Professional UX layout, interactions, visual tokens |
| `docs/TEST_PLAN.md` | Tests, hardware checks, soak, offline and acceptance |
| `docs/DECISIONS_AND_QUESTIONS.md` | Locked decisions, unresolved product decisions |
| `docs/DEVELOPMENT_GUIDE.md` | Suggested code organization and developer setup |
| `docs/SOURCES.md` | Documentation links verified during planning |
| `docs/superpowers/specs/...` | Design document with alternatives and rationale |
| `docs/superpowers/plans/...` | Task-level implementation plan |
| `ui/design-tokens.json` | Initial tokens for Flutter and web implementations |
| `ui/reference-dashboard.html` | **Static reference only** for phone + PC visuals |

## Definition of success for the first milestone

An operator opens the Flutter app on the Pixel, sees live preview and controls, pairs a PC browser without a cloud service, receives a low-bandwidth WebRTC feed, records a high-quality original *on the phone*, says the chosen wake phrase, sees a local transcript on **both** screens, and downloads the completed original over the LAN with hash verification. The system must be demonstrated after public Internet access is removed. iOS compatibility is designed in, but cannot be claimed tested without an iPhone and macOS build environment.
