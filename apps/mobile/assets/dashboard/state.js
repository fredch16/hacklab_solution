export const MAX_EVENTS = 30;
export const initialState = () => ({ sessionId: null, sequence: -1, timestampUtc: null, snapshot: null, receivedAt: null, events: [] });
/** Returns a new state, or the same object if the envelope is ignored. */
export function applyEnvelope(s, env, now) {
    if (env.version !== 1 || env.type !== "session.snapshot" || !env.payload)
        return s;
    if (s.sessionId !== null && env.session_id !== s.sessionId) {
        // Every envelope is a snapshot; a different session_id is accepted only if it is newer.
        if (s.timestampUtc !== null && Date.parse(env.timestamp_utc) < Date.parse(s.timestampUtc))
            return s;
    }
    else if (s.sessionId !== null && env.sequence <= s.sequence) {
        return s;
    }
    const fresh = s.sessionId !== env.session_id;
    const added = s.snapshot && !fresh ? diffSnapshots(s.snapshot, env.payload) : [];
    const events = [...added.map((text) => ({ at: now, text })).reverse(), ...(fresh ? [] : s.events)].slice(0, MAX_EVENTS);
    return { sessionId: env.session_id, sequence: env.sequence, timestampUtc: env.timestamp_utc, snapshot: env.payload, receivedAt: now, events };
}
const label = (v) => (v == null ? "Unavailable" : v.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));
/** Human-readable changes between consecutive snapshots (state fields only). */
export function diffSnapshots(a, b) {
    const out = [];
    const chg = (name, x, y) => {
        if (x !== y)
            out.push(`${name}: ${label(x)} \u2192 ${label(y)}`);
    };
    chg("App state", a.app, b.app);
    chg("Camera permission", a.permissions.camera, b.permissions.camera);
    chg("Microphone permission", a.permissions.microphone, b.permissions.microphone);
    chg("Camera", a.camera.state, b.camera.state);
    chg("Recording", a.recording.state, b.recording.state);
    chg("Voice", a.voice.state, b.voice.state);
    const pa = powerLabel(a.battery).toLowerCase(), pb = powerLabel(b.battery).toLowerCase();
    if (pa !== pb)
        out.push(`Power: ${pa} \u2192 ${pb}`);
    chg("Thermal", a.thermal.status, b.thermal.status);
    if (a.viewer.connected !== b.viewer.connected)
        out.push(b.viewer.connected ? "Viewer connected" : "Viewer disconnected");
    if (a.keep_screen_on !== b.keep_screen_on)
        out.push(`Keep screen on: ${b.keep_screen_on == null ? "Unavailable" : b.keep_screen_on ? "on" : "off"}`);
    if (a.network.addresses.join() !== b.network.addresses.join())
        out.push(`Phone addresses: ${b.network.addresses.join(", ") || "none"}`);
    if (a.network.cert_sha256 !== b.network.cert_sha256)
        out.push("Certificate changed");
    return out;
}
export const UNAVAILABLE = "Unavailable";
export function powerLabel(b) {
    if (b.charging === true)
        return "Charging";
    if (b.plugged === true)
        return b.charging === false ? "Plugged in, not charging" : "Plugged in";
    if (b.plugged === false)
        return "On battery";
    return b.charging === false ? "Not charging" : UNAVAILABLE;
}
export function formatBytes(n) {
    if (n == null || !Number.isFinite(n) || n < 0)
        return UNAVAILABLE;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1000 && i < u.length - 1) {
        n /= 1000;
        i++;
    }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}
export function formatPercent(n) {
    return n == null || !Number.isFinite(n) ? UNAVAILABLE : `${Math.round(n)}%`;
}
const pad = (n) => String(n).padStart(2, "0");
/** Local time of day, or date + time when not today. */
export function formatLocal(iso, now = new Date(), withDate = false) {
    if (!iso)
        return UNAVAILABLE;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime()))
        return UNAVAILABLE;
    const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    const sameDay = d.toDateString() === now.toDateString();
    return withDate && !sameDay ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${t}` : t;
}
export const formatClock = (ms) => (ms == null ? UNAVAILABLE : formatLocal(new Date(ms).toISOString()));
export function closeMessage(code) {
    switch (code) {
        case 4001: return "Another browser was paired. Pair again from the phone.";
        case 4002: return "This browser's access expired. Pair again from the phone.";
        case 4003: return "Access revoked on the phone.";
        case 4004: return "The session ended on the phone. Start a session on the phone and pair again.";
        default: return null;
    }
}
export const isTerminalClose = (code) => code >= 4001 && code <= 4004;
const BACKOFF = [1, 2, 4, 8, 15];
/** Reconnect delay in seconds for a 0-based attempt: 1,2,4,8,15,15... */
export const backoffSeconds = (attempt) => BACKOFF[Math.min(Math.max(attempt, 0), BACKOFF.length - 1)];
export function pairErrorMessage(status, code) {
    if (status === "network")
        return "Phone unreachable \u2014 check same Wi-Fi, session started on phone, client isolation.";
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
export function stopReasonMessage(reason) {
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
export function webrtcErrorMessage(code, message) {
    const base = code === "CAMERA_UNAVAILABLE" ? "The phone camera is not available."
        : code === "NEGOTIATION_FAILED" ? "Could not set up video with the phone. Try again."
            : code === "BAD_REQUEST" ? "The phone rejected the video request (too many attempts?). Wait a second and try again."
                : "Video failed.";
    return message ? `${base} (${message})` : base;
}
/** Informational note about the phone camera (never blocks Start video: the phone starts its camera on offer). */
export function cameraHint(camera) {
    if (!camera || camera.state !== "error")
        return null;
    const e = {
        PERMISSION_CAMERA: "Camera permission is not granted on the phone.", DEVICE_IN_USE: "The camera is in use by another app.",
        CAPABILITY_UNSUPPORTED: "The phone does not support the required camera mode.", CAMERA_FAILED: "The camera failed on the phone.",
    };
    return (camera.error && e[camera.error]) || "The camera has an error on the phone.";
}
/** H.264 first (stable), everything else kept after as fallback. */
export function preferH264(codecs) {
    const h = (c) => c.mimeType.toLowerCase() === "video/h264";
    return [...codecs.filter(h), ...codecs.filter((c) => !h(c))];
}
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
/** Reduce RTCStatsReport entries to a display model. First sample (no prev) has no bitrate. */
export function computeStats(entries, prev) {
    const byId = new Map(entries.map((e) => [e.id, e]));
    const inbound = entries.find((e) => e.type === "inbound-rtp" && (e.kind ?? e.mediaType) === "video");
    const pair = entries.find((e) => e.type === "candidate-pair" && e.nominated && e.state === "succeeded") ?? entries.find((e) => e.type === "candidate-pair" && e.state === "succeeded");
    const mime = inbound?.codecId ? byId.get(inbound.codecId)?.mimeType : null;
    const bytes = num(inbound?.bytesReceived), ts = num(inbound?.timestamp);
    let bitrateBps = null;
    if (prev && bytes != null && ts != null && ts > prev.ts && bytes >= prev.bytes)
        bitrateBps = ((bytes - prev.bytes) * 8) / ((ts - prev.ts) / 1000);
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
const dash = (v) => v ?? "\u2014";
export function formatBitrate(bps) {
    return bps == null ? "\u2014" : bps >= 1e6 ? `${(bps / 1e6).toFixed(2)} Mbps` : `${Math.round(bps / 1e3)} kbps`;
}
export const formatSize = (w, h) => (w && h ? `${w}\u00D7${h}` : "\u2014");
/** Browser-side metric rows; missing values show an em dash, never 0. */
export function metricRows(m) {
    return [
        ["Codec", dash(m?.codec ?? null)], ["Size", formatSize(m?.width ?? null, m?.height ?? null)],
        ["FPS", m?.fps == null ? "\u2014" : m.fps.toFixed(1)], ["Bitrate", formatBitrate(m?.bitrateBps ?? null)],
        ["RTT", m?.rttMs == null ? "\u2014" : `${Math.round(m.rttMs)} ms`],
        ["Path", m?.localCandidate || m?.remoteCandidate ? `${m?.localCandidate ?? "?"} \u2192 ${m?.remoteCandidate ?? "?"}` : "\u2014"],
        ["Lost", m?.packetsLost == null ? "\u2014" : String(m.packetsLost)],
    ];
}
export function phoneEncoderRows(p) {
    const c = p?.capture, st = p?.stream;
    return [
        ["Capture", c?.fps_measured == null ? "\u2014" : `${c.fps_measured.toFixed(1)} fps${c.width && c.height ? ` \u00B7 ${c.width}\u00D7${c.height}` : ""}`],
        ["Encoder", st ? [st.codec, st.encoder].filter(Boolean).join(" \u00B7 ") || "\u2014" : "\u2014"],
        ["Sent", st ? `${formatBitrate(st.bitrate_bps)}${st.fps == null ? "" : ` \u00B7 ${st.fps.toFixed(1)} fps`}` : "\u2014"],
        ["Limited by", st?.quality_limitation ? st.quality_limitation : "\u2014"],
    ];
}
// ---------- Slice 2: recording ----------
export function formatElapsed(ms) {
    if (ms == null || !Number.isFinite(ms) || ms < 0)
        return "\u2014";
    const t = Math.floor(ms / 1000), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}
/** Elapsed anchored to local receipt time (immune to phone/PC clock skew). */
export function liveElapsed(rec, receivedAt, now) {
    if (rec.state !== "recording")
        return rec.elapsed_ms ?? null;
    if (rec.elapsed_ms != null)
        return rec.elapsed_ms + (receivedAt == null ? 0 : Math.max(0, now - receivedAt));
    const t = rec.started_utc ? Date.parse(rec.started_utc) : NaN;
    return Number.isNaN(t) ? null : Math.max(0, now - t);
}
export function formatProfile(p) {
    if (!p)
        return "\u2014";
    const parts = [p.width && p.height ? `${p.width}\u00D7${p.height}` : null, p.fps != null ? `${p.fps} fps` : null, p.codec ? p.codec.toUpperCase() : null, p.hdr ? "HDR" : null].filter(Boolean);
    return parts.length ? parts.join(" \u00B7 ") : "\u2014";
}
export function formatAudio(a) {
    if (!a || a.sample_rate == null)
        return "\u2014";
    const ch = a.channels === 2 ? "stereo" : a.channels === 1 ? "mono" : a.channels != null ? `${a.channels} ch` : null;
    return [`${a.sample_rate / 1000} kHz`, ch].filter(Boolean).join(" \u00B7 ");
}
export function recordingStateLabel(state) {
    const m = { idle: "Not recording", starting: "Starting\u2026", recording: "Recording", stopping: "Stopping\u2026", finalizing: "Finalizing file\u2026", available: "Saved", failed: "Failed" };
    return (state && m[state]) || label(state);
}
export function commandErrorMessage(code, message) {
    const m = {
        PHONE_DISCONNECTED: "Not connected to the phone.",
        COMMAND_TIMEOUT: "No response from the phone.",
        ACTION_NOT_ALLOWED_WHILE_RECORDING: "That setting can't be changed while recording. Stop the recording first.",
        PROFILE_UNSUPPORTED: "The phone can't record with that quality setting. Choose another quality.",
        LOW_STORAGE: "Not enough free storage on the phone to start recording (at least 2 GB needed).",
        RECORDING_FINALIZE_FAILED: "The recording could not be finalized on the phone. The file may be incomplete.",
        PERMISSION_MIC: "Microphone permission is not granted on the phone. Recordings need audio.",
        PERMISSION_CAMERA: "Camera permission is not granted on the phone.",
        ALREADY_RECORDING: "The phone is already recording.",
        NOT_RECORDING: "The phone is not recording.",
        BAD_REQUEST: "The phone rejected the request.",
        CAMERA_UNAVAILABLE: "The phone camera is not available.",
        DEVICE_IN_USE: "The camera is in use by another app.",
        CAPABILITY_UNSUPPORTED: "The phone does not support that camera mode.",
        CAMERA_FAILED: "The camera failed on the phone.",
        INVALID_WAKE_PHRASE: "The phone rejected that wake name. Use 2–40 characters with English letters, spaces or apostrophes.",
        MODEL_MISSING: "The voice model is missing on the phone.",
        VOICE_INTERRUPTED: "Voice was interrupted on the phone.",
    };
    const base = (code && m[code]) || `Command failed${code ? ` (${code})` : ""}.`;
    return message && !(code && m[code]) ? `${base} ${message}` : base;
}
export function warningMessage(code) {
    if (code === "LOW_STORAGE")
        return "Phone storage is running low. Recording stops automatically if it gets too low.";
    if (code === "THERMAL")
        return "The phone is hot. Recording continues; let it cool if you can.";
    return code ? `Warning: ${code}` : null;
}
