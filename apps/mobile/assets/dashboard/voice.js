import { commandErrorMessage } from "./state.js";
export const MAX_TRANSCRIPTS = 50;
export const initialLive = () => ({ wake: null, partial: null, transcripts: [], commands: {} });
const ts = (t) => Date.parse(t.started_utc ?? t.ended_utc ?? "") || 0;
const newestFirst = (list) => [...list].sort((a, b) => ts(b) - ts(a)).slice(0, MAX_TRANSCRIPTS);
/** Apply a phone->browser voice.* event. Unknown types are ignored. */
export function applyVoiceEvent(live, type, p) {
    const id = typeof p?.utterance_id === "string" ? p.utterance_id : null;
    switch (type) {
        case "voice.wake_detected":
            return id ? { ...live, wake: { utteranceId: id, phrase: p.phrase ?? null }, partial: null } : live;
        case "voice.transcript.partial":
            // a late partial for an utterance that already has its final must not resurrect the partial line
            if (!id || typeof p.text !== "string" || live.transcripts.some((t) => t.id === id))
                return live;
            return { ...live, partial: { utteranceId: id, text: p.text } };
        case "voice.transcript.final": {
            if (!id)
                return live;
            const t = { id, text: String(p.text ?? ""), started_utc: p.started_utc ?? null, ended_utc: p.ended_utc ?? null, end_reason: p.end_reason ?? null };
            return {
                wake: live.wake?.utteranceId === id ? null : live.wake,
                partial: live.partial?.utteranceId === id ? null : live.partial,
                transcripts: newestFirst([t, ...live.transcripts.filter((x) => x.id !== id)]),
                commands: live.commands,
            };
        }
        case "voice.command":
            return id ? { ...live, commands: { ...live.commands, [id]: { command: p.command ?? null, ok: p.ok !== false, error: p.error ?? null } } } : live;
        default: return live;
    }
}
/** Badge for a transcript's voice command, or null when none/no action. */
export function commandBadge(t, ev) {
    const command = ev?.command ?? t.command ?? null;
    if (!command || command === "none")
        return null;
    if (ev && !ev.ok)
        return `\u2715 ${commandErrorMessage(ev.error)}`;
    return command === "recording.start" ? "\u25B6 Started recording" : command === "recording.stop" ? "\u25A0 Stopped recording" : null;
}
/** Fold a snapshot's voice block in: snapshot transcripts win by id; transient wake/partial clear when voice is idle. */
export function mergeSnapshotVoice(live, snap) {
    if (!snap)
        return live;
    const byId = new Map(live.transcripts.map((t) => [t.id, t]));
    for (const t of snap.transcripts ?? [])
        byId.set(t.id, t);
    const transcripts = newestFirst([...byId.values()]);
    const active = snap.state === "wake_detected" || snap.state === "recognizing";
    const done = (id) => !!id && byId.has(id);
    return {
        wake: active && live.wake && !done(live.wake.utteranceId) ? live.wake : null,
        partial: active && live.partial && !done(live.partial.utteranceId) ? live.partial : null,
        transcripts,
        commands: live.commands,
    };
}
export function endReasonLabel(r) {
    switch (r) {
        case "silence": return "ended on pause";
        case "max_duration": return "reached 15 s limit";
        case "no_speech": return "no speech heard";
        case "cancelled": return "cancelled";
        default: return "ended";
    }
}
const ERR = {
    MODEL_MISSING: "The voice model is missing on the phone.",
    PERMISSION_MIC: "Microphone permission is not granted on the phone.",
    VOICE_INTERRUPTED: "Voice was interrupted — the microphone may be in use by another app.",
    INVALID_WAKE_PHRASE: "The phone rejected the wake phrase.",
};
export function voiceStatus(v) {
    switch (v?.state) {
        case "listening": return { icon: "◉", text: "Listening", detail: null, sev: "ok" };
        case "wake_detected": return { icon: "★", text: "Wake detected", detail: null, sev: "ok" };
        case "recognizing": return { icon: "…", text: "Recognizing…", detail: null, sev: "ok" };
        case "muted": return { icon: "✕", text: "Muted", detail: "Wake detection and transcription are off.", sev: "neutral" };
        case "loading_model": return { icon: "↻", text: "Loading model…", detail: null, sev: "warn" };
        case "error": return { icon: "!", text: "Error", detail: (v.error && ERR[v.error]) || (v.error ? `Voice error (${v.error}).` : "Voice failed on the phone."), sev: "bad" };
        case "unavailable": return { icon: "–", text: "Unavailable", detail: v.wake_phrase ? "Starts with the video stream." : "Set a wake phrase in the dashboard.", sev: "neutral" };
        default: return { icon: "–", text: "Unavailable", detail: null, sev: "neutral" };
    }
}
const COMMON = new Set(["hey", "hi", "hello", "ok", "okay", "yes", "no", "stop", "go", "start", "help"]);
/** Mirrors the contract: 2-40 chars, letters/spaces/apostrophes. Phone remains the authority. */
export function validateWakePhrase(raw) {
    const normalized = raw.trim().replace(/\s+/g, " ");
    const bad = (error) => ({ ok: false, normalized, error, warning: null });
    if (normalized.length < 2)
        return bad("Enter at least 2 characters.");
    if (normalized.length > 40)
        return bad("Use at most 40 characters.");
    if (!/^[A-Za-z' ]+$/.test(normalized))
        return bad("Use letters, spaces and apostrophes only (English).");
    const words = normalized.split(" ").length, letters = normalized.replace(/[^A-Za-z]/g, "").length;
    let warning = null;
    if (words < 2 && letters < 6)
        warning = "Short single words trigger by accident. Use two words or at least 6 letters.";
    else if (COMMON.has(normalized.toLowerCase()))
        warning = "This is a very common word and may trigger by accident.";
    return { ok: true, normalized, error: null, warning };
}
/** wake -> partial* -> final -> command -> (dismiss = null). A new wake replaces whatever is showing. */
export function popupReduce(p, type, e) {
    const id = typeof e?.utterance_id === "string" ? e.utterance_id : null;
    if (!id)
        return p;
    if (type === "voice.wake_detected")
        return { utteranceId: id, phrase: e.phrase ?? null, phase: "listening", text: "", endReason: null, command: null };
    if (!p || p.utteranceId !== id)
        return p;
    if (type === "voice.transcript.partial" && p.phase === "listening" && typeof e.text === "string")
        return { ...p, text: e.text };
    if (type === "voice.transcript.final")
        return { ...p, phase: "done", text: String(e.text ?? "").trim(), endReason: e.end_reason ?? null };
    if (type === "voice.command" && p.phase === "done")
        return { ...p, command: { command: e.command ?? null, ok: e.ok !== false, error: e.error ?? null } };
    return p;
}
export function popupView(p) {
    if (p.phase === "listening") {
        const m = /^(.*?)(\S+)$/.exec(p.text.trimEnd());
        const stable = m ? m[1] : "", tail = m ? m[2] : "";
        return { title: "Listening…", subtitle: p.phrase ? `Wake phrase “${p.phrase}”` : null, stable, tail, animating: true, result: null, announce: "Listening" };
    }
    const none = p.text === "" || p.endReason === "no_speech";
    let result = null;
    if (none)
        result = { kind: "neutral", text: "Didn’t hear a command" };
    else if (p.command) {
        const c = p.command;
        result = !c.ok ? { kind: "bad", text: commandErrorMessage(c.error) }
            : c.command === "recording.start" ? { kind: "rec", text: "▶ Starting recording" }
                : c.command === "recording.stop" ? { kind: "neutral", text: "■ Stopping recording" }
                    : { kind: "neutral", text: "No matching command" };
    }
    return { title: none ? "No command" : "Heard", subtitle: null, stable: none ? "" : p.text, tail: "", animating: false, result, announce: [none ? "" : p.text, result?.text].filter(Boolean).join(". ") || "Didn’t hear a command" };
}
