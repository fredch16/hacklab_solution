# HackLab agent entry point

Read [the current agent handoff](docs/AGENT_HANDOFF.md) before changing or restarting anything. It records the working setup, the behavior agreed with the owner, the evidence, and the remaining acceptance checks.

- This repository is the HackLab integration. Edit the laptop gateway and arm services at the root. Edit the Android client and TypeScript dashboard under `dashboard/`. For changes there, also read [dashboard/AGENTS.md](dashboard/AGENTS.md).
- Use [the laptop dashboard guide](docs/LAPTOP_DASHBOARD.md) for startup and pairing, [the hardware guide](docs/HARDWARE.md) for Station and calibration, and [the voice guide](docs/VOICE.md) for Claude credentials. The active architecture is the first section of [the architecture guide](docs/ARCHITECTURE.md). The Safari gateway section is historical.
- Run automated arm motion tests against the simulator. On real hardware, the owner tests movement. Agents may inspect read-only status and confirm camera frames. Preserve the existing motion checks and direct arm stop path.
- Keep `.env`, camera recordings, calibration backups and machine state out of commits. Commit the placeholder `.env.example` when its documented interface changes. Never print a real API key to verify it.
- Check for active recording or arm motion before restarting a service. An in-memory Claude override disappears when its voice bridge is restarted. Pull with a merge or verified fast-forward before an authorized push. Keep the owner's local edits and avoid force pushes.
