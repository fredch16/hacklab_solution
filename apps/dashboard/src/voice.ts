// Pure voice logic: transcript reducer, labels, wake-phrase validation. No DOM.

export interface VoiceTranscript { id: string; text: string; started_utc: string | null; ended_utc: string | null; end_reason: string | null }
export interface LiveVoice {
  wake: { utteranceId: string; phrase: string | null } | null;
  partial: { utteranceId: string; text: string } | null;
  transcripts: VoiceTranscript[]; // newest first
}
export const MAX_TRANSCRIPTS = 50;
export const initialLive = (): LiveVoice => ({ wake: null, partial: null, transcripts: [] });

const ts = (t: VoiceTranscript) => Date.parse(t.started_utc ?? t.ended_utc ?? "") || 0;
const newestFirst = (list: VoiceTranscript[]) => [...list].sort((a, b) => ts(b) - ts(a)).slice(0, MAX_TRANSCRIPTS);

/** Apply a phone->browser voice.* event. Unknown types are ignored. */
export function applyVoiceEvent(live: LiveVoice, type: string, p: any): LiveVoice {
  const id = typeof p?.utterance_id === "string" ? p.utterance_id : null;
  switch (type) {
    case "voice.wake_detected":
      return id ? { ...live, wake: { utteranceId: id, phrase: p.phrase ?? null }, partial: null } : live;
    case "voice.transcript.partial":
      // a late partial for an utterance that already has its final must not resurrect the partial line
      if (!id || typeof p.text !== "string" || live.transcripts.some((t) => t.id === id)) return live;
      return { ...live, partial: { utteranceId: id, text: p.text } };
    case "voice.transcript.final": {
      if (!id) return live;
      const t: VoiceTranscript = { id, text: String(p.text ?? ""), started_utc: p.started_utc ?? null, ended_utc: p.ended_utc ?? null, end_reason: p.end_reason ?? null };
      return {
        wake: live.wake?.utteranceId === id ? null : live.wake,
        partial: live.partial?.utteranceId === id ? null : live.partial,
        transcripts: newestFirst([t, ...live.transcripts.filter((x) => x.id !== id)]),
      };
    }
    default: return live;
  }
}

/** Fold a snapshot's voice block in: snapshot transcripts win by id; transient wake/partial clear when voice is idle. */
export function mergeSnapshotVoice(live: LiveVoice, snap: { state?: string | null; transcripts?: VoiceTranscript[] | null } | null | undefined): LiveVoice {
  if (!snap) return live;
  const byId = new Map(live.transcripts.map((t) => [t.id, t]));
  for (const t of snap.transcripts ?? []) byId.set(t.id, t);
  const transcripts = newestFirst([...byId.values()]);
  const active = snap.state === "wake_detected" || snap.state === "recognizing";
  const done = (id: string | undefined) => !!id && byId.has(id);
  return {
    wake: active && live.wake && !done(live.wake.utteranceId) ? live.wake : null,
    partial: active && live.partial && !done(live.partial.utteranceId) ? live.partial : null,
    transcripts,
  };
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
  INVALID_WAKE_PHRASE: "The phone rejected the wake phrase.",
};
export function voiceStatus(v: { state?: string | null; error?: string | null; wake_phrase?: string | null } | null | undefined): VoiceStatus {
  switch (v?.state) {
    case "listening": return { icon: "◉", text: "Listening", detail: null, sev: "ok" };
    case "wake_detected": return { icon: "★", text: "Wake detected", detail: null, sev: "ok" };
    case "recognizing": return { icon: "…", text: "Recognizing…", detail: null, sev: "ok" };
    case "muted": return { icon: "✕", text: "Muted", detail: "Wake detection and transcription are off.", sev: "neutral" };
    case "loading_model": return { icon: "↻", text: "Loading model…", detail: null, sev: "warn" };
    case "error": return { icon: "!", text: "Error", detail: (v.error && ERR[v.error]) || (v.error ? `Voice error (${v.error}).` : "Voice failed on the phone."), sev: "bad" };
    case "unavailable": return { icon: "–", text: "Unavailable", detail: v.wake_phrase ? "Voice is not available on the phone." : "Set a wake phrase in the dashboard.", sev: "neutral" };
    default: return { icon: "–", text: "Unavailable", detail: null, sev: "neutral" };
  }
}

const COMMON = new Set(["hey", "hi", "hello", "ok", "okay", "yes", "no", "stop", "go", "start", "help"]);
export interface PhraseCheck { ok: boolean; normalized: string; error: string | null; warning: string | null }
/** Mirrors the contract: 2-40 chars, letters/spaces/apostrophes. Phone remains the authority. */
export function validateWakePhrase(raw: string): PhraseCheck {
  const normalized = raw.trim().replace(/\s+/g, " ");
  const bad = (error: string): PhraseCheck => ({ ok: false, normalized, error, warning: null });
  if (normalized.length < 2) return bad("Enter at least 2 characters.");
  if (normalized.length > 40) return bad("Use at most 40 characters.");
  if (!/^[A-Za-z' ]+$/.test(normalized)) return bad("Use letters, spaces and apostrophes only (English).");
  const words = normalized.split(" ").length, letters = normalized.replace(/[^A-Za-z]/g, "").length;
  let warning: string | null = null;
  if (words < 2 && letters < 6) warning = "Short single words trigger by accident. Use two words or at least 6 letters.";
  else if (COMMON.has(normalized.toLowerCase())) warning = "This is a very common word and may trigger by accident.";
  return { ok: true, normalized, error: null, warning };
}
