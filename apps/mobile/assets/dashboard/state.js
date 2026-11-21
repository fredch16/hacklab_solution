// Pure logic: no DOM, no network. Implements packages/protocol/README.md (v1).
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
