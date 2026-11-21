import test from "node:test";
import assert from "node:assert/strict";
import * as V from "../dist/voice.js";

const final = (id, text, t = "2026-11-21T14:00:05Z", end_reason = "silence") => ({ utterance_id: id, text, started_utc: t, ended_utc: t, end_reason });

test("final adds a transcript; wake and partials do not touch the list", () => {
  let l = V.applyVoiceEvent(V.initialLive(), "voice.wake_detected", { utterance_id: "u1", phrase: "hey robot" });
  l = V.applyVoiceEvent(l, "voice.transcript.partial", { utterance_id: "u1", text: "turn to" });
  assert.equal(l.transcripts.length, 0);
  l = V.applyVoiceEvent(l, "voice.transcript.final", final("u1", "turn towards the window"));
  assert.deepEqual(l.transcripts.map((t) => t.text), ["turn towards the window"]);
});

test("duplicate final replaces; newest first", () => {
  let l = V.applyVoiceEvent(V.initialLive(), "voice.transcript.final", final("u1", "one", "2026-11-21T14:00:01Z"));
  l = V.applyVoiceEvent(l, "voice.transcript.final", final("u2", "two", "2026-11-21T14:00:09Z"));
  l = V.applyVoiceEvent(l, "voice.transcript.final", final("u1", "one fixed", "2026-11-21T14:00:01Z"));
  assert.deepEqual(l.transcripts.map((t) => t.text), ["two", "one fixed"]);
  assert.equal(V.applyVoiceEvent(l, "voice.unknown", {}), l);
});

test("snapshot merge: reconnect restores transcripts, snapshot wins by id", () => {
  const l = V.applyVoiceEvent(V.initialLive(), "voice.transcript.final", final("u1", "old", "2026-11-21T14:00:01Z"));
  const m = V.mergeSnapshotVoice(l, { transcripts: [{ id: "u2", text: "b", started_utc: "2026-11-21T14:00:09Z", ended_utc: null, end_reason: "silence" }, { id: "u1", text: "a", started_utc: "2026-11-21T14:00:01Z", ended_utc: null, end_reason: "silence" }] });
  assert.deepEqual(m.transcripts.map((t) => t.text), ["b", "a"]);
  assert.equal(V.mergeSnapshotVoice(l, null), l);
});

test("end reasons and statuses", () => {
  assert.equal(V.endReasonLabel("silence"), "ended on pause");
  assert.equal(V.endReasonLabel("max_duration"), "reached 15 s limit");
  assert.equal(V.endReasonLabel("no_speech"), "no speech heard");
  assert.equal(V.voiceStatus({ state: "listening" }).text, "Listening");
  assert.match(V.voiceStatus({ state: "unavailable", wake_phrase: null }).detail, /Set a wake name/);
  assert.match(V.voiceStatus({ state: "error", error: "MODEL_MISSING" }).detail, /model is missing/);
  assert.equal(V.voiceStatus({ state: "error", error: "PERMISSION_MIC" }).sev, "bad");
  assert.equal(V.voiceStatus(null).text, "Unavailable");
});

test("the editable name always follows Hey", () => {
  assert.deepEqual(["Robot", "ELi", "Elrobot"].map((name) => V.validateWakeName(name).normalized),
    ["hey Robot", "hey ELi", "hey Elrobot"]);
  assert.equal(V.validateWakeName("  Hey   Robot  ").normalized, "hey Robot");
  assert.equal(V.wakeName("hey Eli"), "Eli");
  for (const name of ["", "hey", "a", "r2d2", "''", "x".repeat(41)])
    assert.equal(V.validateWakeName(name).ok, false, name);
});

test("voice command badges reflect dashboard outcomes", () => {
  let l = V.applyVoiceEvent(V.initialLive(), "voice.transcript.final", final("u1", "start recording"));
  l = V.applyVoiceEvent(l, "voice.command", { utterance_id: "u1", command: "recording.start", ok: true, error: null });
  assert.equal(V.commandBadge(l.commands.u1), "▶ Started recording");
  l = V.applyVoiceEvent(l, "voice.command", { utterance_id: "u1", command: "recording.start", ok: false, error: "LOW_STORAGE" });
  assert.match(V.commandBadge(l.commands.u1), /^✕ .*storage/i);
  assert.equal(V.commandBadge({ command: "recording.stop", ok: true, error: null }), "■ Stopped recording");
  assert.equal(V.commandBadge(), null);
  assert.equal(V.commandBadge({ command: "none", ok: true, error: null }), null);
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
  assert.deepEqual([V.popupView(p).title, V.popupView(p).subtitle, V.popupView(p).mode], ["Listening…", "Wake phrase “hey robot”", "wake"]);
  p = V.popupReduce(p, "voice.transcript.partial", { utterance_id: "u1", text: "start the rec" });
  assert.deepEqual([V.popupView(p).stable, V.popupView(p).tail, V.popupView(p).mode], ["start the ", "rec", "recognizing"]);
  assert.equal(V.popupReduce(p, "voice.transcript.partial", { utterance_id: "other", text: "x" }), p);
  p = V.popupReduce(p, "voice.transcript.final", { utterance_id: "u1", text: "start recording", end_reason: "silence" });
  assert.equal(V.popupView(p).mode, "done"); assert.equal(V.popupView(p).glyph, "\u2713");
  assert.equal(V.popupView(p).stable, "start recording");
  assert.equal(V.popupView(p).result, null); // command result not here yet
  assert.equal(V.popupReduce(p, "voice.transcript.partial", { utterance_id: "u1", text: "late" }).text, "start recording");
  const ok = V.popupReduce(p, "voice.command", { utterance_id: "u1", command: "recording.start", ok: true });
  assert.deepEqual(V.popupView(ok).result, { kind: "rec", text: "▶ Started recording" });
  assert.equal(V.popupView(V.popupReduce(p, "voice.command", { utterance_id: "u1", command: "recording.stop", ok: true })).result.text, "■ Stopped recording");
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

test("a live recording transcript must agree with the acoustic transcription", () => {
  const type = "voice.transcript.final";
  const plan = (text, acoustic_text) => V.planVoiceCommand(type, { ...final("utt-1", text), acoustic_text });
  assert.deepEqual(plan("Please begin the recording now.", "start recording"),
    { type: "recording.start", request_id: "voice:utt-1" });
  assert.deepEqual(plan("end recording", "stop recording"),
    { type: "recording.stop", request_id: "voice:utt-1" });
  assert.deepEqual(plan("ok, record!", "record"),
    { type: "recording.start", request_id: "voice:utt-1" });
  for (const [text, acoustic] of [
    ["start recording", undefined], ["stop recording", null],
    ["start recording", "stop recording"], ["start recording", "record"],
    ["stout recording", "start recording"], ["don't stop recording", "stop recording"],
    ["start recording and stop recording", "start recording"], ["record the meeting", "record"],
  ]) assert.equal(plan(text, acoustic), null, text);
  const p = { ...final("utt-1", "start recording"), acoustic_text: "start recording" };
  assert.equal(V.planVoiceCommand("voice.transcript.partial", p), null);
  assert.equal(V.planVoiceCommand("session.snapshot", p), null);
  assert.equal(V.planVoiceCommand(type, { ...p, end_reason: "max_duration" }), null);
});

test("arm request: only completed live finals with text and a session", () => {
  const f = { utterance_id: "u9", text: " go to top ", end_reason: "silence" };
  assert.deepEqual(V.armRequest("voice.transcript.final", f, "sess-1"), { session_id: "sess-1", utterance_id: "u9", text: "go to top" });
  assert.equal(V.armRequest("voice.transcript.partial", f, "sess-1"), null);
  assert.equal(V.armRequest("voice.transcript.final", f, null), null);
  assert.equal(V.armRequest("voice.transcript.final", { ...f, end_reason: "max_duration" }, "s"), null);
  assert.equal(V.armRequest("voice.transcript.final", { ...f, text: "  " }, "s"), null);
  assert.equal(V.armRequest("voice.transcript.final", { ...f, utterance_id: "../x" }, "s"), null);
  // recording finals are taken by planVoiceCommand first; this test only pins the shape
  assert.equal(V.planVoiceCommand("voice.transcript.final", f), null);
});

test("arm results: badge, popup and transport failure", () => {
  const ok = V.armResult({ ok: true, command: "arm.go_to_view", error: null, message: "Moving to top" });
  assert.deepEqual(ok, { command: "arm.go_to_view", ok: true, error: null, message: "Moving to top" });
  assert.equal(V.commandBadge(ok), "↻ Moving to top");
  const refused = V.armResult({ ok: false, command: "arm.go_to_view", error: "VIEW_NOT_CHECKED", message: "left isn't checked" });
  assert.equal(V.commandBadge(refused), "✕ left isn't checked");
  const down = V.armResult(null);
  assert.match(V.commandBadge(down), /^✕ Arm command service not reachable/);
  assert.equal(V.commandBadge(V.armResult({ ok: true, command: "none", error: null, message: null })), null);
  let l = V.applyVoiceEvent(V.initialLive(), "voice.command", { utterance_id: "u9", ...ok });
  assert.equal(l.commands.u9.message, "Moving to top");
  let p = V.popupReduce(null, "voice.wake_detected", { utterance_id: "u9" });
  p = V.popupReduce(p, "voice.transcript.final", { utterance_id: "u9", text: "go to top", end_reason: "silence" });
  assert.deepEqual(V.popupView(V.popupReduce(p, "voice.command", { utterance_id: "u9", ...ok })).result, { kind: "neutral", text: "Moving to top" });
  assert.equal(V.popupView(V.popupReduce(p, "voice.command", { utterance_id: "u9", ...down })).result.kind, "bad");
  assert.equal(V.popupView(V.popupReduce(p, "voice.command", { utterance_id: "u9", command: "none", ok: true })).result.text, "No matching command");
});

test("camera recording targets are explicit and phone is the default", () => {
  const final = (text, acoustic_text) => ({utterance_id: "target", text, acoustic_text, end_reason: "silence"});
  const plan = (text, hint) => V.planVoiceCommand("voice.transcript.final", final(text, hint));
  assert.equal(plan("start robot recording", "start robot recording")?.type, "robot.recording.start");
  assert.equal(plan("robot camera recording", "start robot recording")?.type, "robot.recording.start");
  assert.equal(plan("stop robot camera recording", "stop robot recording")?.type, "robot.recording.stop");
  assert.equal(plan("phone camera recording", "start recording")?.type, "recording.start");
  assert.equal(plan("start camera recording", "start recording")?.type, "recording.start");
  assert.equal(plan("stop phone recording", "stop recording")?.type, "recording.stop");
  assert.equal(plan("start robot recording", "start recording"), null);
  assert.equal(plan("don't start robot recording", "start robot recording"), null);
});
