# ADR 0001 — Self-signed HTTPS with browser click-through

**Date:** 2026-11-21 · **Status:** accepted by owner · **Slice:** 0

## Decision
The phone serves the dashboard over HTTPS/WSS with a phone-generated **self-signed** certificate. The PC user accepts the browser warning ("Advanced → Proceed") once per browser and again whenever the phone's IP changes (certificate regenerated). Owner chose this over a phone-generated local CA imported once on each PC.

## Consequences
- Address bar shows "Not secure". The connection is still TLS-encrypted, but the browser cannot authenticate the phone by itself. **Mitigation:** the phone's Link tab shows the certificate SHA-256 fingerprint; the user can compare it with the browser's certificate viewer before proceeding.
- Access control relies on the 6-digit pairing code (120 s, single use, 5 attempts) + HttpOnly/Secure/SameSite=Strict session cookie + Origin check, not on certificate trust.
- Features that need a trusted secure context may be unavailable or behave differently (notably File System Access API for app-managed resumable downloads in Slice 5). Must be verified in Chrome and Firefox when those slices land; fall back to standard downloads.
- Revisit (local CA option) if Slice 1/5 browser testing shows a blocking limitation.

## Android local-network permission
App targets SDK 36 (Flutter 3.50.1 default), so Android 17's `ACCESS_LOCAL_NETWORK` runtime permission is not enforced. Re-evaluate when raising targetSdk to 37.
