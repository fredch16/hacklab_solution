# Protocol v1 — locked contract (Slice 0)

Both `apps/mobile` (server) and `apps/dashboard` (browser client) implement exactly this. Change it only by editing this file first.

## Transport

- Phone serves **HTTPS + WSS on port 8443**, bound to `0.0.0.0`, using a **self-signed certificate** (ADR 0001). Connections whose remote address is not loopback, RFC1918 private (10/8, 172.16/12, 192.168/16), link-local (169.254/16) or CGNAT (100.64/10) are dropped.
- Certificate: generated on the phone, stored in app-private storage, SAN = every current non-loopback IPv4 of the phone. Regenerated only when the IP set is no longer covered. The phone shows its **SHA-256 fingerprint** (uppercase hex, colon-separated) on the Link tab and in the snapshot.
- Server runs **only while a session is active** (phone "Start session"); "End session" stops the server and revokes the viewer.
- Static dashboard files are served from Flutter assets `assets/dashboard/` (`/` → `index.html`). All responses carry `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `Content-Security-Policy: default-src 'self'; connect-src 'self' wss:; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'`.

## Auth

- Cookie `lvc_session` = 32 random bytes (`Random.secure`), base64url, no padding. Set with `HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200`.
- Session token lifetime **12 h**; stored server-side in memory only (never logged, never in snapshots).
- **One viewer.** A successful pairing replaces any existing viewer: old token invalidated, its WebSocket closed with code `4001` reason `replaced`.
- Revoke from phone: token invalidated, WebSocket closed with `4003` reason `revoked`. Expiry: close `4002` reason `expired`. Session end: `4004` reason `session_ended`.
- **Origin check:** every `POST` and the WS upgrade must carry `Origin` equal to `https://<Host header>`; otherwise `403 {"error":"ORIGIN_REJECTED"}`.

## QR pairing (primary) — browser shows QR, phone scans and approves

1. Browser (unpaired) calls `POST /api/v1/pair/request` (Origin checked, empty body). Server creates a pending request: `request_id` = 16 random bytes base64url (shown in QR) and a separate `secret` = 32 random bytes base64url set as cookie `lvc_pending` (`HttpOnly; Secure; SameSite=Strict; Path=/api/v1/pair; Max-Age=120`). Response `200 {"request_id":"…","qr":"lvc-pair:1:<request_id>","expires_utc":"…"}`. Pending requests live **120 s**; at most **3** exist at once (oldest evicted); max **10 creations per minute** per remote IP → `429 {"error":"RATE_LIMITED"}`. Requires an active session (server only runs then).
2. Browser renders `qr` as a QR code (bundled encoder, no CDN), with countdown, and polls `GET /api/v1/pair/request/status` every 1 s with its `lvc_pending` cookie → `{"state":"pending|approved|denied|expired"}`. Missing/unknown cookie → `{"state":"expired"}`. On `expired` the browser automatically requests a new QR.
3. Phone **Link → Scan to pair** opens the camera scanner only while that screen is open (camera released on exit). A scanned string that is not `lvc-pair:1:<id>` or an unknown/expired id → clear error on phone. Valid → confirm sheet "Pair this browser? Request from <remote IPv4> · replaces current viewer if any" with **Approve / Deny**.
4. On Approve the request becomes `approved`. The **next status poll carrying the matching `lvc_pending` secret** receives `{"state":"approved"}` together with `Set-Cookie: lvc_session=…` (same rules as code pairing, replaces existing viewer → 4001) and `lvc_pending` cleared. Knowing `request_id` alone (e.g. photographing the QR) is not enough to obtain the session. Single use.
5. In-app API: `PairingService.createPairRequest(remoteIp)`, `.pairRequestStatus(secret)`, `.approveRequest(requestId)`, `.denyRequest(requestId)`, `.lookupRequest(requestId)` (for the confirm sheet).

## Pairing code (fallback)

- Browser shows it behind a small "Can't scan? Use a code" link. Phone: **Link → Use a code instead**.
- Phone user taps **Pair with code** → 6-digit numeric code (`Random.secure`, zero-padded), valid **120 s**, single use. Generating a new code invalidates the previous one. Phone UI makes clear that pairing replaces the current viewer.
- Max **5 wrong attempts** per code → code invalidated (phone shows "Too many attempts — generate a new code").
- Code comparison is constant-time.

## Endpoints

| Method/path | Auth | Response |
|---|---|---|
| `GET /` and static files | none | dashboard assets (contain no session data) |
| `GET /api/v1/session` | none | `200 {"paired": true}` if cookie valid, else `200 {"paired": false}` |
| `POST /api/v1/pair` body `{"code":"123456"}` | none (Origin checked) | `200 {"paired":true}` + Set-Cookie; `400 {"error":"BAD_REQUEST"}`; `401 {"error":"PAIRING_CODE_INVALID"}` (wrong/expired/no active code); `429 {"error":"PAIRING_LOCKED"}` (attempts exhausted) |
| `POST /api/v1/unpair` | cookie | `200 {"paired":false}`, clears cookie, invalidates token |
| `GET /api/v1/snapshot` | cookie | `200` envelope `session.snapshot`; `401 {"error":"PAIRING_REQUIRED"}` |
| `GET /api/v1/events` (WSS upgrade) | cookie + Origin | server sends `session.snapshot` immediately, then a new full `session.snapshot` on every state change; `401` before upgrade if unauthenticated |

Error body is always `{"error": "<CODE>", "message": "<human readable>"}` (message optional). Unknown `/api/*` → `404 {"error":"NOT_FOUND"}`. Body limit 4 KiB on POST.

## Envelope

```json
{"version":1,"type":"session.snapshot","session_id":"<opaque, new per phone session>","sequence":7,"timestamp_utc":"2026-11-21T14:00:00.000Z","payload":{...}}
```

`sequence` increases by 1 for every envelope emitted in a session (shared counter). Client discards envelopes with a different `session_id` than its latest snapshot unless it is a newer snapshot, and ignores lower `sequence`.

## `session.snapshot` payload (Slice 0)

Every field is present; unknown/unavailable values are `null` (never fake zeros).

```json
{
  "app": "active",                              // idle|requesting_permissions|ready|active|interrupted|error
  "session_started_utc": "2026-11-21T14:00:00.000Z",
  "keep_screen_on": true,
  "device": {"model": "Pixel 10 Pro XL", "manufacturer": "Google", "os": "Android 17", "sdk_int": 37, "app_version": "0.1.0+1"},
  "permissions": {"camera": "granted", "microphone": "denied"},   // granted|denied|permanently_denied|restricted|limited|not_requested
  "camera": {"state": "not_started"},          // Slice 0 always not_started
  "recording": {"state": "not_started"},       // Slice 0 always not_started
  "voice": {"state": "not_started"},           // Slice 0 always not_started
  "battery": {"level_percent": 86, "charging": false, "plugged": false},  // any may be null; plugged = external power connected; charging = actively charging
  "thermal": {"status": "none"},               // none|light|moderate|severe|critical|emergency|shutdown|null
  "storage": {"free_bytes": 92000000000, "total_bytes": 256000000000},
  "network": {"addresses": ["10.19.20.246"], "port": 8443, "tls": "self_signed", "cert_sha256": "AB:CD:..."},
  "viewer": {"paired": true, "connected": true, "paired_since_utc": "2026-11-21T14:01:00.000Z", "expires_utc": "2026-11-22T02:01:00.000Z"}
}
```

Battery/thermal/storage/device values are re-read by the phone every **5 s** while a session is active; a new snapshot is emitted only if something changed.

## Browser close codes → UI

`4001` replaced → "Another browser was paired. Pair again from the phone."; `4002` expired; `4003` revoked → "Access revoked on the phone."; `4004` session ended. Any other close / network error → reconnect with backoff (1, 2, 4, 8, max 15 s), showing "Reconnecting…" and marking every value **stale**; on reconnect, take the fresh snapshot.

## Slice 1 — camera + WebRTC (locked)

**Owner decisions:** camera preview starts automatically with **Start session** (camera permission requested then; denial leaves session running with camera `error`/`PERMISSION_CAMERA`). PC video starts only when the user clicks **Start video** in the browser; after any disconnect the browser shows "Video stopped" and waits for another click (no auto-restart).

### Snapshot `camera` (replaces Slice 0 shape)
```json
"camera": {"state": "previewing", "error": null, "lens": "back"}
```
`state`: `not_started|initializing|previewing|transmitting|paused|interrupted|error` (`paused` = scanner page open; `transmitting` = WebRTC peer connected and sending). `error`: null or `PERMISSION_CAMERA|DEVICE_IN_USE|CAPABILITY_UNSUPPORTED|CAMERA_FAILED`.
Snapshot also gains `"stream": {"state": "idle|connecting|connected|failed", "error": null|"ICE_FAILED"|"NEGOTIATION_FAILED"|"CAMERA_UNAVAILABLE"}`.

### Messages on `/api/v1/events` (browser → phone)
- `{"version":1,"type":"webrtc.offer","request_id":"<random>","payload":{"sdp":"<full SDP after ICE gathering complete>"}}` — browser offer, one `recvonly` video transceiver, H.264 preferred via `setCodecPreferences`, `iceServers: []`. A new offer replaces any existing peer.
- `{"version":1,"type":"webrtc.stop","request_id":"…","payload":{}}`

### Phone → browser
- `webrtc.answer` `{"request_id","sdp"}` (complete gathering, host candidates only).
- `webrtc.error` `{"request_id","code","message"}` codes: `CAMERA_UNAVAILABLE|NEGOTIATION_FAILED|BAD_REQUEST`.
- `webrtc.stopped` `{"reason": "browser_stop|camera_stopped|session_ended|replaced|ice_failed|error"}`.
- `camera.stats` every 1 s while camera state is `previewing|transmitting`:
```json
{"capture": {"width": 640, "height": 360, "fps_measured": 29.9},
 "stream": {"codec": "H264", "encoder": "c2.google.avc.encoder", "width": 640, "height": 360,
            "fps": 29.7, "bitrate_bps": 912000, "quality_limitation": "none", "rtt_ms": 4.0,
            "local_candidate": "host", "remote_candidate": "prflx"}}
```
`capture` = frames delivered by ImageAnalysis (measured); `stream` = null unless a peer is connected; any field unavailable → null. Never requested/target values.

Envelopes all carry the shared `sequence`. Max 1 offer per second accepted (else `webrtc.error BAD_REQUEST`). SDP max 64 KiB.

### Targets (not guarantees)
640×360, 30 FPS, max bitrate 1.2 Mbps set via sender `RtpParameters`, `degradationPreference = BALANCED`.
