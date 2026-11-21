import { commandErrorMessage } from "./state.js";
// Pure voice logic: transcript reducer, labels, wake-phrase validation. No DOM.

export interface VoiceTranscript { id: string; text: string; started_utc: string | null; ended_utc: string | null; end_reason: string | null; command?: string | null }
export interface LiveVoice {
  transcripts: VoiceTranscript[]; // newest first
  commands: Record<string, { command: string | null; ok: boolean; error: string | null }>; // voice.command results by utterance id
}
export const MAX_TRANSCRIPTS = 50;
export const initialLive = (): LiveVoice => ({ transcripts: [], commands: {} });

const ts = (t: VoiceTranscript) => Date.parse(t.started_utc ?? t.ended_utc ?? "") || 0;
const newestFirst = (list: VoiceTranscript[]) => [...list].sort((a, b) => ts(b) - ts(a)).slice(0, MAX_TRANSCRIPTS);

/** Fold finals and command results from phone->browser voice.* events into the transcript list. (Wake/partials drive the popup only.) */
export function applyVoiceEvent(live: LiveVoice, type: string, p: any): LiveVoice {
  const id = typeof p?.utterance_id === "string" ? p.utterance_id : null;
  switch (type) {
    case "voice.transcript.final": {
      if (!id) return live;
      const t: VoiceTranscript = { id, text: String(p.text ?? ""), started_utc: p.started_utc ?? null, ended_utc: p.ended_utc ?? null, end_reason: p.end_reason ?? null };
      return {
        ...live,
        transcripts: newestFirst([t, ...live.transcripts.filter((x) => x.id !== id)]),
      };
    }
    case "voice.command":
      return id ? { ...live, commands: { ...live.commands, [id]: { command: p.command ?? null, ok: p.ok !== false, error: p.error ?? null } } } : live;
    default: return live;
  }
}

/** Badge for a transcript's voice command, or null when none/no action. */
export function commandBadge(t: VoiceTranscript, ev?: LiveVoice["commands"][string]): string | null {
  const command = ev?.command ?? t.command ?? null;
  if (!command || command === "none") return null;
  if (ev && !ev.ok) return `\u2715 ${commandErrorMessage(ev.error)}`;
  return command === "recording.start" ? "\u25B6 Started recording" : command === "recording.stop" ? "\u25A0 Stopped recording" : null;
}

/** Fold a snapshot's voice block in: snapshot transcripts win by id. */
export function mergeSnapshotVoice(live: LiveVoice, snap: { transcripts?: VoiceTranscript[] | null } | null | undefined): LiveVoice {
  if (!snap) return live;
  const byId = new Map(live.transcripts.map((t) => [t.id, t]));
  for (const t of snap.transcripts ?? []) byId.set(t.id, t);
  return { ...live, transcripts: newestFirst([...byId.values()]) };
}

export function endReasonLabel(r: string | null | undefined): string {
  switch (r) {
    case "silence": return "ended on pause";
    case "max_duration": return "reached 15 s limit";
    case "no_speech": return "no speech heard";
    case "cancelled": return "cancelled";
    default: return "ended";
  }
}

export interface VoiceStatus { icon: string; text: string; detail: string | null; sev: "ok" | "warn" | "bad" | "neutral" }
const ERR: Record<string, string> = {
  MODEL_MISSING: "The voice model is missing on the phone.",
  PERMISSION_MIC: "Microphone permission is not granted on the phone.",
  VOICE_INTERRUPTED: "Voice was interrupted — the microphone may be in use by another app.",
  VOICE_PROCESSING_FAILED: "Voice processing stopped. Restart the video stream to retry.",
  INVALID_WAKE_PHRASE: "The phone rejected the wake phrase.",
};
export function voiceStatus(v: { state?: string | null; error?: string | null; wake_phrase?: string | null } | null | undefined): VoiceStatus {
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

export type WakeNameCheck =
  | { ok: true; normalized: string }
  | { ok: false; error: string };

export function wakeName(value: string): string {
  return value.trim().replace(/\s+/g, " ").replace(/^hey(?: |$)/i, "");
}

export function validateWakeName(raw: string): WakeNameCheck {
  const name = wakeName(raw);
  if (name.length < 2 || name.length > 40) return { ok: false, error: "Enter a name with 2–40 characters." };
  if (!/^[A-Za-z' ]+$/.test(name) || !/[A-Za-z]/.test(name)) {
    return { ok: false, error: "Use English letters, spaces or apostrophes." };
  }
  return { ok: true, normalized: `hey ${name}` };
}

// ---------- Voice popup state machine ----------
export interface Popup {
  utteranceId: string; phrase: string | null; phase: "listening" | "done"; text: string;
  endReason: string | null; command: { command: string | null; ok: boolean; error: string | null } | null;
}
/** wake -> partial* -> final -> command -> (dismiss = null). A new wake replaces whatever is showing. */
export function popupReduce(p: Popup | null, type: string, e: any): Popup | null {
  const id = typeof e?.utterance_id === "string" ? e.utterance_id : null;
  if (!id) return p;
  if (type === "voice.wake_detected") return { utteranceId: id, phrase: e.phrase ?? null, phase: "listening", text: "", endReason: null, command: null };
  if (!p || p.utteranceId !== id) return p;
  if (type === "voice.transcript.partial" && p.phase === "listening" && typeof e.text === "string") return { ...p, text: e.text };
  if (type === "voice.transcript.final") return { ...p, phase: "done", text: String(e.text ?? "").trim(), endReason: e.end_reason ?? null };
  if (type === "voice.command" && p.phase === "done") return { ...p, command: { command: e.command ?? null, ok: e.ok !== false, error: e.error ?? null } };
  return p;
}

export interface PopupView {
  title: string; subtitle: string | null; stable: string; tail: string; mode: "wake" | "recognizing" | "done"; glyph: string;
  result: { kind: "rec" | "neutral" | "bad"; text: string } | null; announce: string;
}
export function popupView(p: Popup): PopupView {
  if (p.phase === "listening") {
    const m = /^(.*?)(\S+)$/.exec(p.text.trimEnd());
    const stable = m ? (m[1] as string) : "", tail = m ? (m[2] as string) : "";
    return { title: "Listening…", subtitle: p.phrase ? `Wake phrase “${p.phrase}”` : null, stable, tail, mode: stable || tail ? "recognizing" : "wake", glyph: "", result: null, announce: "Listening" };
  }
  const none = p.text === "" || p.endReason === "no_speech";
  let result: PopupView["result"] = null;
  if (none) result = { kind: "neutral", text: "Didn’t hear a command" };
  else if (p.command) {
    const c = p.command;
    result = !c.ok ? { kind: "bad", text: commandErrorMessage(c.error) }
      : c.command === "recording.start" ? { kind: "rec", text: "▶ Starting recording" }
      : c.command === "recording.stop" ? { kind: "neutral", text: "■ Stopping recording" }
      : { kind: "neutral", text: "No matching command" };
  }
  return { title: none ? "No command" : "Heard", subtitle: null, stable: none ? "" : p.text, tail: "", mode: "done", glyph: result?.kind === "bad" ? "!" : none || result?.text === "No matching command" ? "\u2013" : "\u2713", result, announce: [none ? "" : p.text, result?.text].filter(Boolean).join(". ") || "Didn’t hear a command" };
}
