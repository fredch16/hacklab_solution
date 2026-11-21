import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../dist/state.js";

const snap = (o = {}) => ({
  app: "active", session_started_utc: "2026-11-21T14:00:00.000Z", keep_screen_on: true,
  device: { model: "P", manufacturer: "G", os: "A", sdk_int: 37, app_version: "0" },
  permissions: { camera: "denied", microphone: "granted" },
  camera: { state: "not_started" }, recording: { state: "not_started" }, voice: { state: "not_started" },
  battery: { level_percent: 86, charging: false }, thermal: { status: "none" },
  storage: { free_bytes: 92e9, total_bytes: 256e9 },
  network: { addresses: ["10.0.0.2"], port: 8443, tls: "self_signed", cert_sha256: "AB" },
  viewer: { paired: true, connected: true, paired_since_utc: null, expires_utc: null }, ...o,
});
const env = (seq, p = snap(), sid = "s1", ts = "2026-11-21T14:00:00.000Z") =>
  ({ version: 1, type: "session.snapshot", session_id: sid, sequence: seq, timestamp_utc: ts, payload: p });

test("sequence rules", () => {
  let s = S.applyEnvelope(S.initialState(), env(5), 1);
  assert.equal(s.sequence, 5);
  assert.equal(S.applyEnvelope(s, env(4), 2), s);
  assert.equal(S.applyEnvelope(s, env(5), 2), s);
  assert.equal(S.applyEnvelope(s, env(6), 2).sequence, 6);
});

test("session_id change: newer accepted, older ignored, history reset", () => {
  let s = S.applyEnvelope(S.initialState(), env(5), 1);
  assert.equal(S.applyEnvelope(s, env(1, snap(), "s0", "2020-01-01T00:00:00Z"), 2), s);
  const n = S.applyEnvelope(s, env(1, snap(), "s2", "2026-11-21T15:00:00Z"), 2);
  assert.equal(n.sessionId, "s2"); assert.equal(n.events.length, 0);
});

test("diff produces readable events, newest first", () => {
  let s = S.applyEnvelope(S.initialState(), env(1), 1);
  s = S.applyEnvelope(s, env(2, snap({ permissions: { camera: "granted", microphone: "granted" }, viewer: { ...snap().viewer, connected: false } })), 2);
  assert.deepEqual(s.events.map((e) => e.text).sort(), ["Camera permission: Denied \u2192 Granted", "Viewer disconnected"]);
  assert.equal(S.diffSnapshots(snap(), snap()).length, 0);
});

test("formatters", () => {
  assert.equal(S.formatBytes(92e9), "92.0 GB");
  assert.equal(S.formatBytes(512), "512 B");
  assert.equal(S.formatBytes(null), "Unavailable");
  assert.equal(S.formatPercent(86), "86%");
  assert.equal(S.formatPercent(null), "Unavailable");
  assert.equal(S.formatLocal(null), "Unavailable");
  assert.match(S.formatLocal("2026-11-21T14:00:00Z"), /^\d\d:\d\d:\d\d$/);
});

test("close codes and backoff", () => {
  assert.match(S.closeMessage(4003), /revoked/);
  assert.equal(S.closeMessage(1006), null);
  assert.ok(S.isTerminalClose(4004) && !S.isTerminalClose(1006));
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map(S.backoffSeconds), [1, 2, 4, 8, 15, 15, 15]);
});

test("pair errors", () => {
  assert.match(S.pairErrorMessage("network"), /client isolation/);
  assert.match(S.pairErrorMessage(429, "PAIRING_LOCKED"), /new code on the phone/);
});

import qrcode from "../vendor/qrcode.js";
test("qr: version/size and determinism for a pairing string", () => {
  const mk = () => { const q = qrcode(0, "M"); q.addData("lvc-pair:1:AAAAAAAAAAAAAAAAAAAAAA", "Byte"); q.make(); return q; };
  const a = mk(), b = mk();
  const n = a.getModuleCount();
  assert.equal(n, 4 * 3 + 17); // 32 bytes at ECC M -> version 3 (29 modules)
  const dump = (q) => Array.from({ length: n }, (_, r) => Array.from({ length: n }, (_, c) => (q.isDark(r, c) ? 1 : 0)).join("")).join("\n");
  assert.equal(dump(a), dump(b));
  // finder pattern top-left: 7x7 ring
  assert.ok([0, 1, 2, 3, 4, 5, 6].every((i) => a.isDark(0, i) && a.isDark(6, i) && a.isDark(i, 0) && a.isDark(i, 6)));
  assert.ok(!a.isDark(1, 1) && a.isDark(2, 2));
});

test("power label and diff", () => {
  const b = (o) => ({ level_percent: 80, charging: null, plugged: null, ...o });
  assert.equal(S.powerLabel(b({ charging: true, plugged: true })), "Charging");
  assert.equal(S.powerLabel(b({ charging: false, plugged: true })), "Plugged in, not charging");
  assert.equal(S.powerLabel(b({ charging: false, plugged: false })), "On battery");
  assert.equal(S.powerLabel(b({})), "Unavailable");
  const d = S.diffSnapshots(snap({ battery: b({ plugged: false, charging: false }) }), snap({ battery: b({ plugged: true, charging: true }) }));
  assert.deepEqual(d, ["Power: on battery → charging"]);
});

test("stop reasons and errors", () => {
  assert.match(S.stopReasonMessage("camera_stopped"), /camera stopped on the phone/);
  assert.match(S.stopReasonMessage("ice_failed"), /client isolation/);
  assert.match(S.stopReasonMessage("replaced"), /another viewer/);
  assert.equal(S.stopReasonMessage("weird"), "Video stopped.");
  assert.match(S.webrtcErrorMessage("CAMERA_UNAVAILABLE"), /not available/);
});

test("camera hint is informational only", () => {
  assert.equal(S.cameraHint({ state: "not_started" }), null);
  assert.equal(S.cameraHint({ state: "previewing" }), null);
  assert.equal(S.cameraHint({ state: "paused" }), null); // unknown/removed states are tolerated
  assert.match(S.cameraHint({ state: "error", error: "PERMISSION_CAMERA" }), /permission/);
  assert.match(S.cameraHint({ state: "error", error: "WHATEVER" }), /error on the phone/);
});

test("codec preference puts H264 first, keeps fallbacks", () => {
  const c = [{ mimeType: "video/VP8" }, { mimeType: "video/H264", n: 1 }, { mimeType: "video/AV1" }, { mimeType: "video/h264", n: 2 }];
  assert.deepEqual(S.preferH264(c).map((x) => x.mimeType), ["video/H264", "video/h264", "video/VP8", "video/AV1"]);
});

const report = (bytes, ts, extra = {}) => [
  { id: "i", type: "inbound-rtp", kind: "video", codecId: "c", bytesReceived: bytes, timestamp: ts, frameWidth: 640, frameHeight: 360, framesPerSecond: 29.5, packetsLost: 0, framesDecoded: 10, ...extra },
  { id: "c", type: "codec", mimeType: "video/H264" },
  { id: "p", type: "candidate-pair", nominated: true, state: "succeeded", currentRoundTripTime: 0.004, localCandidateId: "l", remoteCandidateId: "r" },
  { id: "l", type: "local-candidate", candidateType: "host" }, { id: "r", type: "remote-candidate", candidateType: "prflx" },
];
test("stats: first sample has no bitrate, second computes delta", () => {
  const a = S.computeStats(report(1000, 1000), null);
  assert.equal(a.model.bitrateBps, null);
  assert.equal(a.model.codec, "H264");
  assert.equal(a.model.rttMs, 4);
  assert.equal(a.model.localCandidate, "host");
  assert.equal(a.model.packetsLost, 0); // real zero is kept
  const b = S.computeStats(report(126000, 2000), a.sample);
  assert.equal(b.model.bitrateBps, 1_000_000);
  assert.equal(S.formatBitrate(b.model.bitrateBps), "1.00 Mbps");
});
test("stats: missing data yields nulls and dashes", () => {
  const { model } = S.computeStats([], null);
  assert.equal(model.codec, null);
  assert.ok(S.metricRows(model).every(([, v]) => v === "—"));
  assert.ok(S.metricRows(null).every(([, v]) => v === "—"));
  assert.ok(S.phoneEncoderRows(null).every(([, v]) => v === "—"));
  const r = S.phoneEncoderRows({ capture: { width: 640, height: 360, fps_measured: 29.9 }, stream: { codec: "H264", encoder: "c2.x", width: 640, height: 360, fps: 29.7, bitrate_bps: 912000, quality_limitation: "none" } });
  assert.equal(r[2][1], "912 kbps · 29.7 fps");
});

const caps = { lenses: [
  { id: "0", kind: "wide", focal_mm: 6.9, zoom_range: [1, 10], stabilization: ["off", "on"], exposure_comp: [-12, 12], exposure_step: 1 / 6, torch: true,
    video: [{ width: 3840, height: 2160, fps: [30, 60], codecs: ["avc", "hevc"], hdr: true, verified: "passed", measured: { fps: 29.9 } }, { width: 1920, height: 1080, fps: [30], codecs: ["avc"], verified: "untested" }] },
  { id: "1", kind: "front", zoom_range: [1, 2], torch: false, stabilization: ["off"], video: [{ width: 1280, height: 720, fps: [30], codecs: ["avc"], verified: "failed" }] },
] };

test("lens labels and quality options", () => {
  assert.equal(S.lensLabel(caps.lenses[0]), "Wide · 6.9 mm");
  assert.equal(S.lensLabel(caps.lenses[1]), "Front");
  assert.equal(S.lensLabel({ id: "7", kind: "weird", video: [] }), "Camera 7");
  const o = S.qualityOptions(caps.lenses[0]);
  assert.equal(o.length, 5);
  assert.equal(o[0].key, "3840x2160@60:hevc"); // biggest, fastest, hevc first
  assert.equal(o[0].badge, "Tested ✓ 29.9 fps");
  assert.equal(o[4].badge, "Untested");
  assert.equal(S.qualityOptions(caps.lenses[1])[0].badge, "Failed");
  assert.deepEqual(S.qualityOptions(undefined), []);
});

test("hdr only when current quality supports it", () => {
  const l = caps.lenses[0];
  assert.ok(S.hdrSupported(l, { width: 3840, height: 2160, fps: 30, codec: "hevc" }));
  assert.ok(!S.hdrSupported(l, { width: 1920, height: 1080, fps: 30, codec: "avc" }));
});

test("settings diff, lens switch, validation", () => {
  const cur = { lens_id: "0", width: 3840, height: 2160, fps: 30, codec: "hevc", zoom: 5, torch: false };
  assert.deepEqual(S.diffSettings(cur, { zoom: 5, torch: true, hdr: undefined }), { torch: true });
  const p = S.settingsForLens(caps, "1", cur);
  assert.deepEqual(p, { lens_id: "1", width: 1280, height: 720, fps: 30, codec: "avc", zoom: 2 });
  assert.equal(S.validateSettings(caps, p, cur), null);
  assert.match(S.validateSettings(caps, { lens_id: "1" }, cur), /quality/);
  assert.match(S.validateSettings(caps, { hdr: true, width: 1920, height: 1080, fps: 30, codec: "avc" }, cur), /HDR/);
  assert.match(S.validateSettings(caps, { zoom: 11 }, cur), /Zoom/);
  assert.match(S.validateSettings(caps, { exposure_comp: 13 }, cur), /Exposure/);
  assert.match(S.validateSettings(caps, { exposure_comp: 1.5 }, cur), /Exposure/);
  assert.match(S.validateSettings(caps, { torch: true, lens_id: "1", width: 1280, height: 720, fps: 30, codec: "avc" }, cur), /torch/);
  assert.match(S.validateSettings(null, {}, cur), /not loaded/);
  assert.deepEqual(S.lockedKeys({ zoom: 2, codec: "avc", torch: true }), ["codec"]);
});

test("elapsed + profile + audio formatting", () => {
  assert.equal(S.formatElapsed(204000), "03:24");
  assert.equal(S.formatElapsed(3723000), "1:02:03");
  assert.equal(S.formatElapsed(null), "—");
  const rec = { state: "recording", elapsed_ms: 10000, started_utc: null };
  assert.equal(S.liveElapsed(rec, 1000, 3000), 12000);
  assert.equal(S.liveElapsed({ state: "recording", started_utc: "2026-11-21T14:00:00Z" }, null, Date.parse("2026-11-21T14:00:05Z")), 5000);
  assert.equal(S.liveElapsed({ state: "idle", elapsed_ms: null }, 1, 2), null);
  assert.equal(S.formatProfile({ width: 3840, height: 2160, fps: 30, codec: "hevc" }), "3840×2160 · 30 fps · HEVC");
  assert.equal(S.formatProfile(null), "—");
  assert.equal(S.formatAudio({ sample_rate: 48000, channels: 2 }), "48 kHz · stereo");
  assert.equal(S.exposureLabel(-6, 1 / 6), "−1.0 EV");
});

test("every slice 2 error code has a plain message", () => {
  for (const c of ["ACTION_NOT_ALLOWED_WHILE_RECORDING", "PROFILE_UNSUPPORTED", "LOW_STORAGE", "RECORDING_FINALIZE_FAILED", "PERMISSION_MIC", "ALREADY_RECORDING", "NOT_RECORDING"])
    assert.ok(!S.commandErrorMessage(c).startsWith("Command failed"), c);
  assert.equal(S.commandErrorMessage("WEIRD", "x"), "Command failed (WEIRD). x");
  assert.match(S.warningMessage("THERMAL"), /hot/);
  assert.equal(S.warningMessage(null), null);
  assert.equal(S.recordingStateLabel("idle"), "Not recording");
});
