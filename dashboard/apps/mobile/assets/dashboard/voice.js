import { commandErrorMessage } from "./state.js";
export const MAX_TRANSCRIPTS = 50;
export const initialLive = () => ({ transcripts: [], commands: {} });
/** Interpret a fresh final only. The phone supplies text from two decoders. */
export function planVoiceCommand(type, payload) {
    if (type !== "voice.transcript.final" || !payload || typeof payload !== "object")
        return null;
    const { utterance_id, text, end_reason, acoustic_text } = payload;
    if (end_reason !== "silence" || typeof text !== "string" ||
        typeof utterance_id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(utterance_id))
        return null;
    const request = text.toLowerCase().replace(/[^a-z0-9' ]/g, " ").replace(/\s+/g, " ").trim()
        .replace(/^(?:(?:okay|ok|please|can you|could you|would you) )+/, "")
        .replace(/(?: (?:please|right now|now|thank you|okay|i think))+$/, "");
    const request_id = `voice:${utterance_id}`;
    if ((request === "record" && acoustic_text === "record") ||
        (/^(start|begin) (the )?recording$/.test(request) && acoustic_text === "start recording")) {
        return { type: "recording.start", request_id };
    }
    if (/^(stop|end) (the )?recording$/.test(request) && acoustic_text === "stop recording") {
        return { type: "recording.stop", request_id };
    }
    return null;
}
/**
 * A live final the recording grammar did not take, for the PC-side arm command service
 * (`voice_bridge.py`, which owns MCP). Only completed utterances (ended on a pause) with text; the
 * service itself ignores anything about recording, so a recording request that failed the acoustic
 * check still does nothing.
 */
export function armRequest(type, payload, sessionId) {
    if (type !== "voice.transcript.final" || !payload || typeof payload !== "object" || !sessionId)
        return null;
    const { utterance_id, text, end_reason } = payload;
    if (end_reason !== "silence" || typeof text !== "string" || !text.trim() || text.length > 500 ||
        typeof utterance_id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(utterance_id))
        return null;
    return { session_id: sessionId, utterance_id, text: text.trim() };
}
/** Normalise the arm command service's JSON reply (or a transport failure) into a command result. */
export function armResult(reply) {
    if (!reply || typeof reply !== "object")
        return { command: "arm", ok: false, error: "ARM_BRIDGE_UNREACHABLE", message: null };
    const r = reply;
    return {
        command: typeof r.command === "string" ? r.command : "none",
        ok: r.ok === true,
        error: typeof r.error === "string" ? r.error : null,
        message: typeof r.message === "string" ? r.message : null,
    };
}
const ARM_ERR = {
    ARM_BRIDGE_UNREACHABLE: "Arm command service not reachable on this PC (voice_bridge.py).",
    ARM_UNREACHABLE: "The camera-arm MCP server is not reachable.",
};
/** Text for an arm command outcome; null when the service took no action. */
export function armText(c) {
    if (!c.command?.startsWith("arm"))
        return null;
    if (c.message)
        return c.message;
    if (c.error)
        return ARM_ERR[c.error] ?? `Arm command failed (${c.error}).`;
    return "Done";
}
const ts = (t) => Date.parse(t.started_utc ?? t.ended_utc ?? "") || 0;
const newestFirst = (list) => [...list].sort((a, b) => ts(b) - ts(a)).slice(0, MAX_TRANSCRIPTS);
/** Fold phone finals and dashboard command outcomes into the transcript list. */
export function applyVoiceEvent(live, type, p) {
    const id = typeof p?.utterance_id === "string" ? p.utterance_id : null;
    switch (type) {
        case "voice.transcript.final": {
            if (!id)
                return live;
            const t = { id, text: String(p.text ?? ""), started_utc: p.started_utc ?? null, ended_utc: p.ended_utc ?? null, end_reason: p.end_reason ?? null };
            return {
                ...live,
                transcripts: newestFirst([t, ...live.transcripts.filter((x) => x.id !== id)]),
            };
        }
        case "voice.command":
            return id ? { ...live, commands: { ...live.commands, [id]: { command: p.command ?? null, ok: p.ok !== false, error: p.error ?? null, message: typeof p.message === "string" ? p.message : null } } } : live;
        default: return live;
    }
}
/** Badge for a transcript's voice command, or null when none/no action. */
export function commandBadge(ev) {
    const command = ev?.command;
    if (!command || command === "none")
        return null;
    const arm = ev ? armText(ev) : null;
    if (arm)
        return ev?.ok ? `\u21BB ${arm}` : `\u2715 ${arm}`;
    if (ev && !ev.ok)
        return `\u2715 ${commandErrorMessage(ev.error)}`;
    return command === "recording.start" ? "\u25B6 Started recording" : command === "recording.stop" ? "\u25A0 Stopped recording" : null;
}
/** Fold a snapshot's voice block in: snapshot transcripts win by id. */
export function mergeSnapshotVoice(live, snap) {
    if (!snap)
        return live;
    const byId = new Map(live.transcripts.map((t) => [t.id, t]));
    for (const t of snap.transcripts ?? [])
        byId.set(t.id, t);
    return { ...live, transcripts: newestFirst([...byId.values()]) };
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
    VOICE_PROCESSING_FAILED: "Voice processing stopped. Restart the video stream to retry.",
    INVALID_WAKE_PHRASE: "The phone rejected the wake phrase.",
};
export function voiceStatus(v) {
    switch (v?.state) {
        case "listening": return { icon: "◉", text: "Listening", detail: null, sev: "ok" };
        case "wake_detected": return { icon: "★", text: "Wake detected", detail: null, sev: "ok" };
        case "recognizing": return { icon: "…", text: "Recognizing…", detail: null, sev: "ok" };
        case "cooling_down": return { icon: "…", text: "Getting ready to listen", detail: null, sev: "neutral" };
        case "muted": return { icon: "✕", text: "Muted", detail: "Wake detection and transcription are off.", sev: "neutral" };
        case "loading_model": return { icon: "↻", text: "Loading model…", detail: null, sev: "warn" };
        case "error": return { icon: "!", text: "Error", detail: (v.error && ERR[v.error]) || (v.error ? `Voice error (${v.error}).` : "Voice failed on the phone."), sev: "bad" };
        case "unavailable": return { icon: "–", text: "Unavailable", detail: v.wake_phrase ? "Starts with the video stream." : "Set a wake name in the dashboard.", sev: "neutral" };
        default: return { icon: "–", text: "Unavailable", detail: null, sev: "neutral" };
    }
}
export function wakeName(value) {
    return value.trim().replace(/\s+/g, " ").replace(/^hey(?: |$)/i, "");
}
export function validateWakeName(raw) {
    const name = wakeName(raw);
    if (name.length < 2 || name.length > 40)
        return { ok: false, error: "Enter a name with 2–40 characters." };
    if (!/^[A-Za-z' ]+$/.test(name) || !/[A-Za-z]/.test(name)) {
        return { ok: false, error: "Use English letters, spaces or apostrophes." };
    }
    return { ok: true, normalized: `hey ${name}` };
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
        return { ...p, command: { command: e.command ?? null, ok: e.ok !== false, error: e.error ?? null, message: typeof e.message === "string" ? e.message : null } };
    return p;
}
export function popupView(p) {
    if (p.phase === "listening") {
        const m = /^(.*?)(\S+)$/.exec(p.text.trimEnd());
        const stable = m ? m[1] : "", tail = m ? m[2] : "";
        return { title: "Listening…", subtitle: p.phrase ? `Wake phrase “${p.phrase}”` : null, stable, tail, mode: stable || tail ? "recognizing" : "wake", glyph: "", result: null, announce: "Listening" };
    }
    const none = p.text === "" || p.endReason === "no_speech";
    let result = null;
    if (none)
        result = { kind: "neutral", text: "Didn’t hear a command" };
    else if (p.command) {
        const c = p.command;
        const arm = armText(c);
        result = arm ? { kind: c.ok ? "neutral" : "bad", text: arm }
            : !c.ok ? { kind: "bad", text: commandErrorMessage(c.error) }
                : c.command === "recording.start" ? { kind: "rec", text: "▶ Started recording" }
                    : c.command === "recording.stop" ? { kind: "neutral", text: "■ Stopped recording" }
                        : { kind: "neutral", text: "No matching command" };
    }
    return { title: none ? "No command" : "Heard", subtitle: null, stable: none ? "" : p.text, tail: "", mode: "done", glyph: result?.kind === "bad" ? "!" : none || result?.text === "No matching command" ? "\u2013" : "\u2713", result, announce: [none ? "" : p.text, result?.text].filter(Boolean).join(". ") || "Didn’t hear a command" };
}
