import test from "node:test";
import assert from "node:assert/strict";
import * as V from "../dist/voice.js";

const final = (id, text, t = "2026-11-21T14:00:05Z", end_reason = "silence") => ({ utterance_id: id, text, started_utc: t, ended_utc: t, end_reason });

test("wake -> partial -> final replaces partial and wake", () => {
  let l = V.applyVoiceEvent(V.initialLive(), "voice.wake_detected", { utterance_id: "u1", phrase: "hey robot" });
  assert.equal(l.wake.phrase, "hey robot");
  l = V.applyVoiceEvent(l, "voice.transcript.partial", { utterance_id: "u1", text: "turn to" });
  l = V.applyVoiceEvent(l, "voice.transcript.partial", { utterance_id: "u1", text: "turn towards the" });
  assert.equal(l.partial.text, "turn towards the");
  l = V.applyVoiceEvent(l, "voice.transcript.final", final("u1", "turn towards the window"));
  assert.equal(l.partial, null); assert.equal(l.wake, null);
  assert.deepEqual(l.transcripts.map((t) => t.text), ["turn towards the window"]);
});

test("late partial after final is ignored; duplicate final replaces; newest first", () => {
  let l = V.applyVoiceEvent(V.initialLive(), "voice.transcript.final", final("u1", "one", "2026-11-21T14:00:01Z"));
  l = V.applyVoiceEvent(l, "voice.transcript.partial", { utterance_id: "u1", text: "late" });
  assert.equal(l.partial, null);
  l = V.applyVoiceEvent(l, "voice.transcript.final", final("u2", "two", "2026-11-21T14:00:09Z"));
  l = V.applyVoiceEvent(l, "voice.transcript.final", final("u1", "one fixed", "2026-11-21T14:00:01Z"));
  assert.deepEqual(l.transcripts.map((t) => t.text), ["two", "one fixed"]);
  assert.equal(V.applyVoiceEvent(l, "voice.unknown", {}), l);
});

test("snapshot merge: reconnect restores transcripts; idle voice clears transients", () => {
  let l = V.applyVoiceEvent(V.initialLive(), "voice.wake_detected", { utterance_id: "u3", phrase: "x" });
  const m = V.mergeSnapshotVoice(l, { state: "recognizing", transcripts: [{ id: "u2", text: "b", started_utc: "2026-11-21T14:00:09Z", ended_utc: null, end_reason: "silence" }, { id: "u1", text: "a", started_utc: "2026-11-21T14:00:01Z", ended_utc: null, end_reason: "silence" }] });
  assert.deepEqual(m.transcripts.map((t) => t.id), ["u2", "u1"]);
  assert.ok(m.wake);
  assert.equal(V.mergeSnapshotVoice(m, { state: "listening", transcripts: [] }).wake, null);
  assert.equal(V.mergeSnapshotVoice(l, null), l);
  // snapshot already containing the utterance's final drops its partial
  const p = V.applyVoiceEvent(l, "voice.transcript.partial", { utterance_id: "u3", text: "hi" });
  assert.equal(V.mergeSnapshotVoice(p, { state: "recognizing", transcripts: [{ id: "u3", text: "hi there", started_utc: null, ended_utc: null, end_reason: "silence" }] }).partial, null);
});

test("end reasons and statuses", () => {
  assert.equal(V.endReasonLabel("silence"), "ended on pause");
  assert.equal(V.endReasonLabel("max_duration"), "reached 15 s limit");
  assert.equal(V.endReasonLabel("no_speech"), "no speech heard");
  assert.equal(V.voiceStatus({ state: "listening" }).text, "Listening");
  assert.match(V.voiceStatus({ state: "unavailable", wake_phrase: null }).detail, /Set a wake phrase/);
  assert.match(V.voiceStatus({ state: "error", error: "MODEL_MISSING" }).detail, /model is missing/);
  assert.equal(V.voiceStatus({ state: "error", error: "PERMISSION_MIC" }).sev, "bad");
  assert.equal(V.voiceStatus(null).text, "Unavailable");
});

test("wake phrase validation", () => {
  assert.equal(V.validateWakePhrase("  hey   robot ").normalized, "hey robot");
  assert.equal(V.validateWakePhrase("hey robot").warning, null);
  assert.equal(V.validateWakePhrase("computer").warning, null); // 8 letters but common? see below
  assert.ok(V.validateWakePhrase("hey").warning);
  assert.ok(V.validateWakePhrase("okay").warning);
  assert.ok(V.validateWakePhrase("robot").warning);
  assert.equal(V.validateWakePhrase("robot").ok, true);
  assert.equal(V.validateWakePhrase("a").ok, false);
  assert.equal(V.validateWakePhrase("x".repeat(41)).ok, false);
  assert.equal(V.validateWakePhrase("hey r2d2").ok, false);
  assert.equal(V.validateWakePhrase("don't stop").ok, true);
});

test("voice.command badges, from event and from snapshot", () => {
  let l = V.applyVoiceEvent(V.initialLive(), "voice.transcript.final", final("u1", "start recording"));
  l = V.applyVoiceEvent(l, "voice.command", { utterance_id: "u1", command: "recording.start", ok: true, error: null });
  assert.equal(V.commandBadge(l.transcripts[0], l.commands.u1), "▶ Started recording");
  l = V.applyVoiceEvent(l, "voice.command", { utterance_id: "u1", command: "recording.start", ok: false, error: "LOW_STORAGE" });
  assert.match(V.commandBadge(l.transcripts[0], l.commands.u1), /^✕ .*storage/i);
  assert.equal(V.commandBadge({ id: "x", text: "", command: "recording.stop" }), "■ Stopped recording");
  assert.equal(V.commandBadge({ id: "x", text: "", command: null }), null);
  assert.equal(V.commandBadge({ id: "x", text: "" }, { command: "none", ok: true, error: null }), null);
  const m = V.mergeSnapshotVoice(l, { state: "listening", transcripts: [] });
  assert.ok(m.commands.u1);
});
test("unavailable reason when waiting for the stream", () => {
  const s = V.voiceStatus({ state: "unavailable", wake_phrase: "hey robot" });
  assert.equal(s.detail, "Starts with the video stream."); assert.equal(s.sev, "neutral");
});

test("popup: wake -> partial -> final -> command, replacement, no_speech", () => {
  let p = V.popupReduce(null, "voice.wake_detected", { utterance_id: "u1", phrase: "hey robot" });
  assert.equal(p.phase, "listening");
  assert.deepEqual([V.popupView(p).title, V.popupView(p).subtitle, V.popupView(p).animating], ["Listening…", "Wake phrase “hey robot”", true]);
  p = V.popupReduce(p, "voice.transcript.partial", { utterance_id: "u1", text: "start the rec" });
  assert.deepEqual([V.popupView(p).stable, V.popupView(p).tail], ["start the ", "rec"]);
  assert.equal(V.popupReduce(p, "voice.transcript.partial", { utterance_id: "other", text: "x" }), p);
  p = V.popupReduce(p, "voice.transcript.final", { utterance_id: "u1", text: "start recording", end_reason: "silence" });
  assert.equal(V.popupView(p).animating, false);
  assert.equal(V.popupView(p).stable, "start recording");
  assert.equal(V.popupView(p).result, null); // command result not here yet
  assert.equal(V.popupReduce(p, "voice.transcript.partial", { utterance_id: "u1", text: "late" }).text, "start recording");
  const ok = V.popupReduce(p, "voice.command", { utterance_id: "u1", command: "recording.start", ok: true });
  assert.deepEqual(V.popupView(ok).result, { kind: "rec", text: "▶ Starting recording" });
  assert.equal(V.popupView(V.popupReduce(p, "voice.command", { utterance_id: "u1", command: "recording.stop", ok: true })).result.text, "■ Stopping recording");
  assert.equal(V.popupView(V.popupReduce(p, "voice.command", { utterance_id: "u1", command: "none", ok: true })).result.text, "No matching command");
  const bad = V.popupView(V.popupReduce(p, "voice.command", { utterance_id: "u1", command: "recording.start", ok: false, error: "LOW_STORAGE" }));
  assert.equal(bad.result.kind, "bad"); assert.match(bad.result.text, /storage/i);
  // a new wake replaces immediately
  const n = V.popupReduce(ok, "voice.wake_detected", { utterance_id: "u2", phrase: "hey robot" });
  assert.equal(n.utteranceId, "u2"); assert.equal(n.command, null); assert.equal(n.text, "");
  // no speech
  const ns = V.popupReduce(n, "voice.transcript.final", { utterance_id: "u2", text: "", end_reason: "no_speech" });
  assert.equal(V.popupView(ns).result.text, "Didn’t hear a command");
  assert.equal(V.popupReduce(null, "voice.transcript.final", { utterance_id: "u9", text: "x" }), null);
});
