# Interface contracts — versioned proposal

These are **proposed schemas**, not implemented endpoints. The agent may adjust field names during Slice 0 after documenting changes in an ADR; do not start writing incompatible components independently. All endpoints except initial pairing need authenticated session credentials. Origin checks and rate limiting are required.

## Networking

- Phone hosts local **HTTPS** endpoint e.g. `https://<phone-lan-ip>:<port>/`. Use valid browser trust configured through the pairing/onboarding flow; see `SECURITY_AND_TRANSFER.md`.
- Dashboard static assets ship **inside mobile app**, served by phone; never rely on a CDN or public router.
- WSS route `/api/v1/events` (status/transcripts, bidirectional authorized commands) and `/api/v1/webrtc` (SDP/ICE signaling); alternatively combine into one versioned event socket with explicit message `kind`.
- REST: `/api/v1/snapshot` (current states + diagnostics), `/api/v1/capabilities` (camera/mic/transcoder models), `/api/v1/recordings` (file list), `/api/v1/recordings/{id}` (manifest), `/api/v1/recordings/{id}/download` (original, Range support).
- LAN peer connection uses phone and browser **host ICE candidates** and `iceServers: []` in MVP. ICE failure needs a human-readable explanation (`different_subnet`, `client_isolation`, `firewall`, `network_changed`).
- **One viewer maximum** at first. New browser sessions request an explicit replace/deny confirmation if someone else is paired.

## Message envelope

```json
{
  "version": 1,
  "type": "session.snapshot",
  "session_id": "opaque-session-id",
  "sequence": 42,
  "timestamp_utc": "2026-11-21T14:00:00Z",
  "payload": {
    "app": "active",
    "camera": "transmitting",
    "recording": "recording",
    "voice": "listening",
    "viewer_count": 1
  }
}
```

Every event has an incrementing `sequence`; after reconnect the client requests a snapshot and discards events from old sessions. Never send actual recording file paths or secrets in events.

## Suggested app event types

| Event | Core payload | Emitted by |
|---|---|---|
| `session.snapshot` | all current state, profile, active recording id, permissions | phone |
| `camera.stats` | actual size, FPS, codec, bitrate, dropped frames, thermal warning | phone |
| `webrtc.connection` | state, selected ICE candidate type, RTT where measured | phone |
| `recording.state` | state, duration, bytes, current id or error reason | phone |
| `voice.state` | model loaded, `listening/wake_detected/recognizing/error/muted` | phone |
| `voice.wake_detected` | timestamp, model id, optional score | phone |
| `voice.transcript.partial` | utterance id, text, elapsed audio time | phone |
| `voice.transcript.final` | utterance id, text, language, start/end | phone |
| `recording.available` | id, name, duration, bytes, sha256 if ready | phone |
| `transfer.progress` | file id, bytes sent/total, state | phone |
| `health.warning` | source, severity, recovery advice | phone |

## Remote commands (only with explicit authority)

```json
{
  "version": 1,
  "type": "recording.start",
  "request_id": "random-idempotency-key",
  "session_id": "opaque-session-id",
  "payload": {"profile_id": "selected-profile-id"}
}
```

Response has `request_id`, `ok`, `state` and an error code/message if rejected. Commands must be **idempotent**; repeated network requests cannot start duplicate recordings or overwrite files. Proposed commands: `recording.start`, `recording.stop`, `voice.mute`, `voice.unmute`, `camera.profile.select`, `session.end`, `transfer.request`, `pairing.revoke`. Whether remote users may stop recording or mute recording audio requires the owner's decision before implementing these controls.

## WebRTC signaling

Use JSON `webrtc.offer`, `webrtc.answer`, `webrtc.ice_candidate`, `webrtc.renegotiate`, `webrtc.disconnect` with session/peer ids. Keep direct media peer connection and proper track lifecycle. Test browser play-policy, video `autoplay`/`playsinline`, receiving-only transceiver, codec negotiation and viewer reconnects.

## Recording manifest example

```json
{
  "id": "rec-opaque-id",
  "filename": "local-20261121T140000Z.mp4",
  "mime": "video/mp4",
  "video_codec": "hevc",
  "width": 3840,
  "height": 2160,
  "fps_measured": 29.97,
  "duration_ms": 120000,
  "size_bytes": 650000000,
  "sha256": "<hex-digest-when-ready>",
  "finalized": true
}
```

Illustrative values **must not be shown as actual Pixel results**. `/download` honors `Range`, `If-Range`, ETag, `Content-Range`, 206/416 and safe Content-Disposition. Hash is computed on the completed local original, not on the compressed WebRTC stream.

## Error codes / semantics

`PERMISSION_CAMERA`, `PERMISSION_MIC`, `DEVICE_IN_USE`, `CAPABILITY_UNSUPPORTED`, `MODEL_MISSING`, `MODEL_LICENSE_BLOCKED`, `VOICE_INTERRUPTED`, `LOW_STORAGE`, `RECORDING_FINALIZE_FAILED`, `NETWORK_UNAVAILABLE`, `PAIRING_REQUIRED`, `CERT_UNTRUSTED`, `ICE_FAILED`, `TRANSFER_CHECKSUM_FAILED`, `THERMAL_DEGRADED`, `UNSUPPORTED_BROWSER`, `ACTION_NOT_AUTHORIZED`.

Errors are user-readable on **both** screens where applicable and do not swallow native diagnostics. Never reuse a success event to represent degraded states.
