# Local-only security, pairing and original-video transfer

## Security model

**Local != automatically trusted.** A public/guest Wi-Fi user must not be able to view video, access transcripts or download files merely by opening the phone's IP. The phone must be the authoritative session owner. Store data locally on phone. Default to **one approved browser viewer**. No outside services after setup.

### Pairing and browser trust

1. Phone operator chooses **Enable local dashboard**. Mobile app displays LAN address plus one-time QR/link for dashboard pairing and a short-lived pairing code.
2. Serve assets and APIs over **browser-trusted HTTPS and WSS**. For an offline LAN, the bootstrap may need a locally generated CA/certificate, explicit import/trust on the PC, and certificates valid for the actual LAN hostname/IP (SAN). Agents must build a clear, secure and documented trust workflow and prove it in Chrome/Firefox; do not hide certificate warnings or propose broad browser security exceptions.
3. Before pairing, expose at most an intentionally limited landing page; do not expose stream, transcripts, files or controls. Authenticate the paired viewer; session tokens expire and can be revoked on phone.
4. Device operator can see and disconnect paired viewer from mobile UI. On network change, renegotiate WebRTC and regenerate/display the reachable address; if service cannot be reached explain likely causes (different Wi-Fi, AP/client isolation, firewall, expired trust, revoked session).
5. Limit `Origin`, prevent CSRF/replay, rate-limit pairing/control, use scoped permissions, random token generation and approved TLS libraries. Do not log pairing secrets in plaintext.
6. No public external STUN/TURN/hosted signaling. Use local ICE host candidates. A separate VLAN or guest Wi-Fi with client isolation is a known limitation; show it plainly, don't silently dial cloud infrastructure.

**Decision gate:** selecting the concrete offline HTTPS certificate trust strategy affects user onboarding, so present one or two secure options to the user if the implementation has a significant tradeoff. Prefer a one-time pairing experience over manual dev-only browser flags.

## Local-network platform permissions

- Android: camera, microphone and access to local networking. Review current Android target SDK and **Android 17 / API 37 `ACCESS_LOCAL_NETWORK`** runtime permission when targeting API 37+. Prior targets have different rules; do not request unsupported permissions on older targets.
- iOS: camera, microphone, `NSLocalNetworkUsageDescription` and Bonjour service registration/discovery information where needed; test local network permission prompts and local server reachability.
- Foreground requirement is explicit. App should keep screen on in active session and handle OS interruptions and battery/thermal conditions truthfully.

## Original recording transfer

**Don't implement SMB or FTP first.** Phone already has an authenticated local HTTPS server. It can expose original finalized files directly; PC browser can download without installing extra programs. LocalSend can be an optional manual interoperability option later, not required for product architecture.

### Transfer protocol, Slice 5

- Mark recording `available` only after video container finalized. Build a manifest including file size, content type, opaque file id, duration, creation time and SHA-256 digest.
- Browser requests `GET /api/v1/recordings/{id}/download` with download token/session auth. Phone streams file chunks from disk with bounded memory. Support `Accept-Ranges: bytes`, 206 Partial Content, ETag, `If-Range`, good Content-Disposition and cancel.
- Verify local copy with SHA-256, preferably in browser on supported devices/size limits, or report checksum to user for offline verification. Never claim verification succeeded if only file size matches.
- For cross-browser behavior, test Chrome and Firefox. Browser-native downloads may resume by themselves using HTTP Range but **cannot be relied upon to silently auto-save/resume with arbitrary path access**. Where File System Access API is available, an explicit destination handle can support app-managed chunked/resumable downloads; on unsupported browsers use standard download and clearly label resume limitations.
- Optional **auto-transfer after stop** only if the user explicitly enables it and browser destination permissions are granted. Never auto-delete original from phone; handle PC absent/offline by queuing on phone with visible status (not a background task; transfers need the app foreground).
- If two large files are queued, serialize or rate-limit transfers to prevent reducing camera/voice responsiveness. Do not transfer an incomplete master while recording.
- Timeout, network loss, out-of-space, insufficient destination permissions and checksums mismatch have visible retry/recovery paths.

### Transfer state

`not_requested -> preparing -> transferring -> verifying -> completed`, with `paused | cancelled | failed` and re-try. Track `bytes_sent`, `bytes_total`, speed and ETA **only from real measurements**.

## Privacy / retention

- Video recording and microphone use are visibly indicated on mobile, including when no PC is connected.
- Wake and ASR are on-device; no audio is sent to the PC by default (only text/status events). If future robot commands or speech output are added, user approval is required.
- Transcript history stores locally according to a user-selected retention policy; do not silently upload or keep raw speech logs. Clarify whether transcripts should be embedded in recording metadata or exported separately.
- A paired PC may locally see transcript events and original files **only** while authorized. Remote stop/start/mute permissions must follow a clear user-approved policy.
