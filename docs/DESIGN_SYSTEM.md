# Design language — Local Vision Console

## Design intent

A **premium camera operator console** with the clarity of a professional cinema monitor and the trustworthiness of an engineering instrument. **Not** a sci-fi HUD, gamified robot UI, generic Bootstrap admin panel, or wall of neon graphs. The user needs to understand *what is recording, what is listening, what is transmitting, whether the computer is connected, and where the original is saved* in less than two seconds.

Style name (internal only): **Quiet Precision**. Neutral graphite and ink surfaces, restrained teal for healthy/interactive states, unambiguous red for recording, amber for degraded performance. Subtle borders and ample whitespace; purposeful information hierarchy; precise numerical types.

**Preview:** `../ui/reference-dashboard.html` is a **static** dual-device visual reference, not a working interface. Adapt designs to actual content and platform constraints rather than screenshot-copying blindly.

## Foundations

### Color tokens

| Token | Value | Usage |
|---|---|---|
| `ink.950` | `#080D14` | Outer background / true dark |
| `ink.900` | `#101824` | Main surfaces |
| `ink.800` | `#172334` | Elevated surfaces |
| `ink.700` | `#253549` | Lines / borders |
| `text.primary` | `#EAF2F9` | Titles and key information |
| `text.secondary` | `#A2B4C5` | Labels and descriptive content |
| `text.tertiary` | `#71859B` | Supporting metadata only |
| `accent.teal` | `#5EDBC8` | Connected / actionable |
| `accent.blue` | `#79B9F2` | Neutral telemetry / focus |
| `state.record` | `#FF6873` | Live recording **only** |
| `state.warning` | `#F4BD6A` | Degraded or user attention |
| `state.danger` | `#F27B7B` | Errors and destructive controls |
| `state.success` | `#73DCB0` | Verified success |

Contrast must meet WCAG AA; do not rely on color alone. Always add text/icon and use shape/label.

### Typography and spacing

- System-first typography (SF Pro on iOS, Roboto on Android, Inter or system sans on browser), avoiding any network-fetched fonts.
- Headings: ~24–28 px desktop; 20–24 px phone, semibold. Module labels 11–12 px uppercase with slight tracking; body 14–16 px; readable in bright environments.
- Technical values in tabular-number font variant (e.g. `03:24`, `29.97 FPS`, `4K · HEVC`, `731 MB`). Avoid changing glyph widths in live metrics.
- 4/8 px base grid; spacing steps 4, 8, 12, 16, 24, 32; control heights 44–52; cards with 14–18 px radius, fine 1 px borders; shadows minimal.
- Iconography: consistent 20/24 px line icon set (offline-bundled), no excessive novelty glyphs; camera/record/mic/connection statuses recognizable without tooltips.

## Android/iPhone app layout — foreground and always useful

### Portrait (default phone)

```text
┌────────────────────────────────────────┐
│ LOCAL VISION  ● LIVE    86%   38°C    │  compact top status
│ Phone · 1 viewer · LAN only           │
├────────────────────────────────────────┤
│                                        │
│          CAMERA PREVIEW                │  large aspect-ratio surface
│      framing guides optional           │  avoid busy overlaid labels
│                                        │
│  [360p WEBRTC]      [4K LOCAL READY]   │  two paths explicitly separate
├────────────────────────────────────────┤
│ ● REC 03:24   1.2 GB        [STOP]     │  pinned recording action strip
├────────────────────────────────────────┤
│ MIC / WAKE    LISTENING · OFFLINE      │  voice state card
│ “Turn towards the window”              │  latest final transcript
│ [Test voice] [Mute]                    │
├────────────────────────────────────────┤
│  Capture     Voice     Files     Link  │  phone navigation
└────────────────────────────────────────┘
```

The **preview is first**, but the phone is not a dumb transmitter. Recording, listening, transcript, battery/thermal/storage and viewer states remain visible. The primary record/stop button stays reachable with one hand, never buried in settings. In landscape, move the summary into a **right-side rail** to maximize preview area. Honor safe areas, notch and camera holes; 48 dp tap targets on Android, 44 pt on iOS; optional prevent accidental stop via press-and-hold or confirmation *only after user approves the interaction*.

### Phone detail tabs

- **Capture:** preview, direct record controls, separate live-stream / master chips, profile selector, duration, storage and warning banners.
- **Voice:** microphone active, wake state, rolling transcript with timestamps, full local/offline indicator, test activation, mute, detected language and model load state.
- **Files:** recordings sorted newest first, ready/finalizing/failed states, file details, transfer controls, local storage remaining, **never silently delete**.
- **Link:** large pairing QR, URL, certificate trust guidance, 1 viewer, peer identity, revoke/disconnect, codec and network diagnostics. QR contains only time-limited pairing bootstrap info, never persistent admin token.

## PC web dashboard layout

Desktop grid: content max width ~1440 px; outer padding 24–32 px; 12-column grid (video 8 columns, status/voice rail 4 columns) at `>=1100px`. Under `1100px` stack side rail under video. Under ~640 px, show mobile-like one-column browser viewer layout.

```text
┌───────────────────────────────────────────────────────────────┐
│ LOCAL VISION  | PIXEL 10 PRO XL  ·  CONNECTED  ●  LAN ONLY    │
├───────────────────────────────────────────────┬───────────────┤
│                                               │ SESSION       │
│          LIVE CAMERA FEED                     │ ● REC 03:24   │
│                                               │ 4K30 HEVC     │
│                                               │ 1 viewer      │
│  [640×360 H264] [29.8 FPS] [RTT 18ms]         │               │
├───────────────────────────────────────────────┤ VOICE         │
│ RECORDINGS                                     │ ● LISTENING   │
│ 2026-11-21 · 1.2GB  [Download original]        │ Transcript…   │
├───────────────────────────────────────────────┤               │
│ CONNECTION / HEALTH / DEVICE INFO             │ RECENT EVENTS │
└───────────────────────────────────────────────┴───────────────┘
```

Video is the **dominant surface**. Controls don't overlap the subject unnecessarily. Performance info is available in a compact bottom strip and detailed diagnostics drawer; video status (H.264 360p) visibly differs from local recording (HEVC 4K). Voice panel shows discrete states with authentic transcripts, not simulated magic AI replies.

## User flows & interaction states

1. **First launch:** explain camera+mic+LAN use with specific permission rationale, show capability check, local models installed/absent, offer configure/pair.
2. **Ready:** phone preview visible, no recording by default (user chooses initial behavior); browser can join after pairing. Real `No viewer` is okay.
3. **Streaming:** distinct teal `LIVE TO PC`, record status remains `NOT RECORDING` until button pressed; stream can pause/reconnect without affecting record.
4. **Recording:** large clear red indicator + elapsed time and actual master profile; local original path/state; option to stop from phone (remote authority user-defined). Support safe finalize loading state.
5. **Wake word:** when listening, subtle mic pulse; on detection, brief highlighted `WAKE DETECTED` and then clear `TRANSCRIBING` state; local partial transcript evolves into final text. No fake feedback sounds by default.
6. **File ready:** actionable `Download original (1.2 GB)` with checksum/progress; browser permission limitations shown if automatic save requested.
7. **Disconnected:** phone still previews/records/transcribes; PC shows frozen last image only if explicitly labeled stale (otherwise blank with reconnect action), not misleading `LIVE`.
8. **Degraded:** thermal, low disk, mic interruption, unsupported profile, HTTPS trust error and client-isolated Wi-Fi are precise warnings with next steps. Avoid generic `Something went wrong`.

## Components

- `StatusPill(label, severity, icon)`: connected, offline, warning, recording; allow text wrapping.
- `CameraViewport`: real video/preview surface, small unobtrusive framing guides, responsive aspect-ratio; black with state info when unavailable.
- `RecordControl`: huge primary circular/bordered record action, red only if active; elapsed timer and stop state.
- `PipelinePair`: two horizontally grouped tags **WebRTC 360p H.264** vs **LOCAL 4K HEVC**; never conflate.
- `VoiceCard`: wake state, mic mute, last utterance; expandable transcript timeline.
- `MetricStrip`: FPS, codec, RTT, packet loss, storage, temperature; hide values when unavailable, do not show `0 ms` as substitute.
- `RecordingRow`: thumbnail (optional and local), file size, duration, finalized state, verified download.
- `PairingPanel`: URL QR and explicit trust/pairing instructions, paired viewer/revoke.
- `ErrorBanner`: clear impact, cause, recommended fix, retry action.

## Motion, accessibility and efficiency

- 120–180 ms understated state transitions; no constant animations except critical active status. Respect reduced-motion OS preference.
- Use no continuous expensive blur over camera surfaces; camera preview should feel 60 Hz smooth if hardware supports it and without redrawing all UI on each frame.
- Important actions have keyboard labels on PC, tooltips for icons, screen reader semantics, focus rings and sufficient contrast. Video is not the only indication of a permission or connection state.
- Foreground session requests screen-on/keep-awake. Manual **dim UI** can reduce OLED power while preserving visible record/wake status. Avoid hidden dimming that makes the phone look stopped.
- Do not load remote fonts, icon CDNs, stock photos or external stylesheets: all dashboard assets bundled locally.

## Visual QA acceptance

Test phone 1080×2400 class, tablet landscape, PC 1366×768, 1920×1080 and browser widths 390/768/1100/1440. No clipped buttons, overlapping camera overlays, horizontal scroll, unreadable stats or low-contrast disabled states. Capture real UI screenshots after each slice; compare against the static reference, then improve as necessary.
