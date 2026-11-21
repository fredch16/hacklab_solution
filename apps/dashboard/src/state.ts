// Pure logic: no DOM, no network. Implements packages/protocol/README.md (v1).

export interface Snapshot {
  app: string;
  session_started_utc: string | null;
  keep_screen_on: boolean | null;
  device: { model: string | null; manufacturer: string | null; os: string | null; sdk_int: number | null; app_version: string | null };
  permissions: { camera: string | null; microphone: string | null };
  camera: { state: string; error?: string | null; lens?: string | null };
  stream?: { state: string; error: string | null };
  recording: { state: string };
  voice: { state: string };
  battery: { level_percent: number | null; charging: boolean | null; plugged?: boolean | null };
  thermal: { status: string | null };
  storage: { free_bytes: number | null; total_bytes: number | null };
  network: { addresses: string[]; port: number | null; tls: string | null; cert_sha256: string | null };
  viewer: { paired: boolean; connected: boolean; paired_since_utc: string | null; expires_utc: string | null };
}

export interface Envelope {
  version: number;
  type: string;
  session_id: string;
  sequence: number;
  timestamp_utc: string;
  payload: Snapshot;
}

export interface State {
  sessionId: string | null;
  sequence: number;
  timestampUtc: string | null;
  snapshot: Snapshot | null;
  receivedAt: number | null; // ms epoch, local receipt time
  events: { at: number; text: string }[]; // newest first
}

export const MAX_EVENTS = 30;
export const initialState = (): State => ({ sessionId: null, sequence: -1, timestampUtc: null, snapshot: null, receivedAt: null, events: [] });

/** Returns a new state, or the same object if the envelope is ignored. */
export function applyEnvelope(s: State, env: Envelope, now: number): State {
  if (env.version !== 1 || env.type !== "session.snapshot" || !env.payload) return s;
  if (s.sessionId !== null && env.session_id !== s.sessionId) {
    // Every envelope is a snapshot; a different session_id is accepted only if it is newer.
    if (s.timestampUtc !== null && Date.parse(env.timestamp_utc) < Date.parse(s.timestampUtc)) return s;
  } else if (s.sessionId !== null && env.sequence <= s.sequence) {
    return s;
  }
  const fresh = s.sessionId !== env.session_id;
  const added = s.snapshot && !fresh ? diffSnapshots(s.snapshot, env.payload) : [];
  const events = [...added.map((text) => ({ at: now, text })).reverse(), ...(fresh ? [] : s.events)].slice(0, MAX_EVENTS);
  return { sessionId: env.session_id, sequence: env.sequence, timestampUtc: env.timestamp_utc, snapshot: env.payload, receivedAt: now, events };
}

const label = (v: string | null | undefined) => (v == null ? "Unavailable" : v.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));

/** Human-readable changes between consecutive snapshots (state fields only). */
export function diffSnapshots(a: Snapshot, b: Snapshot): string[] {
  const out: string[] = [];
  const chg = (name: string, x: string | null | undefined, y: string | null | undefined) => {
    if (x !== y) out.push(`${name}: ${label(x)} \u2192 ${label(y)}`);
  };
  chg("App state", a.app, b.app);
  chg("Camera permission", a.permissions.camera, b.permissions.camera);
  chg("Microphone permission", a.permissions.microphone, b.permissions.microphone);
  chg("Camera", a.camera.state, b.camera.state);
  chg("Recording", a.recording.state, b.recording.state);
  chg("Voice", a.voice.state, b.voice.state);
  const pa = powerLabel(a.battery).toLowerCase(), pb = powerLabel(b.battery).toLowerCase();
  if (pa !== pb) out.push(`Power: ${pa} \u2192 ${pb}`);
  chg("Thermal", a.thermal.status, b.thermal.status);
  if (a.viewer.connected !== b.viewer.connected) out.push(b.viewer.connected ? "Viewer connected" : "Viewer disconnected");
  if (a.keep_screen_on !== b.keep_screen_on) out.push(`Keep screen on: ${b.keep_screen_on == null ? "Unavailable" : b.keep_screen_on ? "on" : "off"}`);
  if (a.network.addresses.join() !== b.network.addresses.join()) out.push(`Phone addresses: ${b.network.addresses.join(", ") || "none"}`);
  if (a.network.cert_sha256 !== b.network.cert_sha256) out.push("Certificate changed");
  return out;
}

export const UNAVAILABLE = "Unavailable";

export function powerLabel(b: Snapshot["battery"]): string {
  if (b.charging === true) return "Charging";
  if (b.plugged === true) return b.charging === false ? "Plugged in, not charging" : "Plugged in";
  if (b.plugged === false) return "On battery";
  return b.charging === false ? "Not charging" : UNAVAILABLE;
}

export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return UNAVAILABLE;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

export function formatPercent(n: number | null | undefined): string {
  return n == null || !Number.isFinite(n) ? UNAVAILABLE : `${Math.round(n)}%`;
}

const pad = (n: number) => String(n).padStart(2, "0");
/** Local time of day, or date + time when not today. */
export function formatLocal(iso: string | null | undefined, now: Date = new Date(), withDate = false): string {
  if (!iso) return UNAVAILABLE;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return UNAVAILABLE;
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const sameDay = d.toDateString() === now.toDateString();
  return withDate && !sameDay ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${t}` : t;
}

export const formatClock = (ms: number | null) => (ms == null ? UNAVAILABLE : formatLocal(new Date(ms).toISOString()));

export function closeMessage(code: number): string | null {
  switch (code) {
    case 4001: return "Another browser was paired. Pair again from the phone.";
    case 4002: return "This browser's access expired. Pair again from the phone.";
    case 4003: return "Access revoked on the phone.";
    case 4004: return "The session ended on the phone. Start a session on the phone and pair again.";
    default: return null;
  }
}
export const isTerminalClose = (code: number) => code >= 4001 && code <= 4004;

const BACKOFF = [1, 2, 4, 8, 15];
/** Reconnect delay in seconds for a 0-based attempt: 1,2,4,8,15,15... */
export const backoffSeconds = (attempt: number) => BACKOFF[Math.min(Math.max(attempt, 0), BACKOFF.length - 1)] as number;

export function pairErrorMessage(status: number | "network", code?: string): string {
  if (status === "network") return "Phone unreachable \u2014 check same Wi-Fi, session started on phone, client isolation.";
  switch (code) {
    case "PAIRING_CODE_INVALID": return "That code is wrong or expired. Check the phone and try again.";
    case "PAIRING_LOCKED": return "Too many attempts. Generate a new code on the phone.";
    case "ORIGIN_REJECTED": return "Request rejected: open this page using the phone's address exactly as shown on its Link tab.";
    case "BAD_REQUEST": return "Enter the 6-digit code.";
    default: return `Pairing failed (HTTP ${status}).`;
  }
}

// ---------- Slice 1: WebRTC video ----------

export const ICE_MESSAGE = "Phone and PC can't reach each other directly \u2014 same Wi-Fi? client isolation? VPN?";

export function stopReasonMessage(reason: string | null | undefined): string {
  switch (reason) {
    case "browser_stop": return "Video stopped.";
    case "camera_stopped": return "Video stopped \u2014 the camera stopped on the phone.";
    case "session_ended": return "Video stopped \u2014 the session ended on the phone.";
    case "replaced": return "Video stopped \u2014 another viewer took over.";
    case "ice_failed": return `Video stopped. ${ICE_MESSAGE}`;
    case "error": return "Video stopped \u2014 the phone reported an error.";
    default: return "Video stopped.";
  }
}

export function webrtcErrorMessage(code: string | null | undefined, message?: string | null): string {
  const base = code === "CAMERA_UNAVAILABLE" ? "The phone camera is not available."
    : code === "NEGOTIATION_FAILED" ? "Could not set up video with the phone. Try again."
    : code === "BAD_REQUEST" ? "The phone rejected the video request (too many attempts?). Wait a second and try again."
    : "Video failed.";
  return message ? `${base} (${message})` : base;
}

/** Why Start video is unavailable for a phone camera state, or null when it is available. */
export function startBlockedReason(camera: Snapshot["camera"] | undefined): string | null {
  if (!camera) return "Waiting for the phone.";
  if (camera.state === "previewing" || camera.state === "transmitting") return null;
  if (camera.state === "error") {
    const e: Record<string, string> = {
      PERMISSION_CAMERA: "Camera permission is denied on the phone.", DEVICE_IN_USE: "The camera is in use by another app.",
      CAPABILITY_UNSUPPORTED: "The phone does not support the required camera mode.", CAMERA_FAILED: "The camera failed on the phone.",
    };
    return (camera.error && e[camera.error]) || "The camera has an error on the phone.";
  }
  if (camera.state === "paused") return "Camera paused \u2014 the phone is on the scanner screen.";
  if (camera.state === "initializing") return "Camera is starting on the phone.";
  if (camera.state === "interrupted") return "Camera interrupted on the phone.";
  return "Camera not running on the phone.";
}

/** H.264 first (stable), everything else kept after as fallback. */
export function preferH264<T extends { mimeType: string }>(codecs: T[]): T[] {
  const h = (c: T) => c.mimeType.toLowerCase() === "video/h264";
  return [...codecs.filter(h), ...codecs.filter((c) => !h(c))];
}

export interface StatsSample { bytes: number; ts: number }
export interface VideoStats {
  codec: string | null; width: number | null; height: number | null; fps: number | null; bitrateBps: number | null;
  rttMs: number | null; localCandidate: string | null; remoteCandidate: string | null; packetsLost: number | null; framesDecoded: number | null;
}
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Reduce RTCStatsReport entries to a display model. First sample (no prev) has no bitrate. */
export function computeStats(entries: Record<string, any>[], prev: StatsSample | null): { model: VideoStats; sample: StatsSample | null } {
  const byId = new Map(entries.map((e) => [e.id, e]));
  const inbound = entries.find((e) => e.type === "inbound-rtp" && (e.kind ?? e.mediaType) === "video");
  const pair = entries.find((e) => e.type === "candidate-pair" && e.nominated && e.state === "succeeded") ?? entries.find((e) => e.type === "candidate-pair" && e.state === "succeeded");
  const mime = inbound?.codecId ? byId.get(inbound.codecId)?.mimeType : null;
  const bytes = num(inbound?.bytesReceived), ts = num(inbound?.timestamp);
  let bitrateBps: number | null = null;
  if (prev && bytes != null && ts != null && ts > prev.ts && bytes >= prev.bytes) bitrateBps = ((bytes - prev.bytes) * 8) / ((ts - prev.ts) / 1000);
  const rtt = num(pair?.currentRoundTripTime);
  return {
    model: {
      codec: typeof mime === "string" ? mime.replace(/^video\//i, "") : null,
      width: num(inbound?.frameWidth), height: num(inbound?.frameHeight), fps: num(inbound?.framesPerSecond), bitrateBps,
      rttMs: rtt == null ? null : rtt * 1000,
      localCandidate: (pair && byId.get(pair.localCandidateId)?.candidateType) ?? null,
      remoteCandidate: (pair && byId.get(pair.remoteCandidateId)?.candidateType) ?? null,
      packetsLost: num(inbound?.packetsLost), framesDecoded: num(inbound?.framesDecoded),
    },
    sample: bytes != null && ts != null ? { bytes, ts } : prev,
  };
}

const dash = (v: string | null) => v ?? "\u2014";
export function formatBitrate(bps: number | null): string {
  return bps == null ? "\u2014" : bps >= 1e6 ? `${(bps / 1e6).toFixed(2)} Mbps` : `${Math.round(bps / 1e3)} kbps`;
}
export const formatSize = (w: number | null, h: number | null) => (w && h ? `${w}\u00D7${h}` : "\u2014");

/** Browser-side metric rows; missing values show an em dash, never 0. */
export function metricRows(m: VideoStats | null): [string, string][] {
  return [
    ["Codec", dash(m?.codec ?? null)], ["Size", formatSize(m?.width ?? null, m?.height ?? null)],
    ["FPS", m?.fps == null ? "\u2014" : m.fps.toFixed(1)], ["Bitrate", formatBitrate(m?.bitrateBps ?? null)],
    ["RTT", m?.rttMs == null ? "\u2014" : `${Math.round(m.rttMs)} ms`],
    ["Path", m?.localCandidate || m?.remoteCandidate ? `${m?.localCandidate ?? "?"} \u2192 ${m?.remoteCandidate ?? "?"}` : "\u2014"],
    ["Lost", m?.packetsLost == null ? "\u2014" : String(m.packetsLost)],
  ];
}

export interface PhoneStats {
  capture?: { width: number | null; height: number | null; fps_measured: number | null } | null;
  stream?: { codec: string | null; encoder: string | null; width: number | null; height: number | null; fps: number | null; bitrate_bps: number | null; quality_limitation: string | null } | null;
}
export function phoneEncoderRows(p: PhoneStats | null): [string, string][] {
  const c = p?.capture, st = p?.stream;
  return [
    ["Capture", c?.fps_measured == null ? "\u2014" : `${c.fps_measured.toFixed(1)} fps${c.width && c.height ? ` \u00B7 ${c.width}\u00D7${c.height}` : ""}`],
    ["Encoder", st ? [st.codec, st.encoder].filter(Boolean).join(" \u00B7 ") || "\u2014" : "\u2014"],
    ["Sent", st ? `${formatBitrate(st.bitrate_bps)}${st.fps == null ? "" : ` \u00B7 ${st.fps.toFixed(1)} fps`}` : "\u2014"],
    ["Limited by", st?.quality_limitation ? st.quality_limitation : "\u2014"],
  ];
}
