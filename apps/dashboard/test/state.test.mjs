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
