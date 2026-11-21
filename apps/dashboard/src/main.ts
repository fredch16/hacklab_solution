import { qrCanvas } from "./qr.js";
import { handleSignal, initVideo, startVideo, stopVideo, videoEl, view as vs } from "./video.js";
import {
  applyEnvelope, backoffSeconds, closeMessage, formatBytes, formatClock, formatLocal, formatPercent,
  initialState, isTerminalClose, pairErrorMessage, powerLabel, metricRows, phoneEncoderRows, cameraHint, type PhoneStats, type RecordingInfo, commandErrorMessage, formatAudio, formatBytes as fmtBytes, formatElapsed, formatProfile, liveElapsed, recordingStateLabel, warningMessage,
  UNAVAILABLE, type Envelope, type Snapshot, type State,
} from "./state.js";

type View = "boot" | "pairing" | "dashboard" | "terminal";
type Conn = "connected" | "reconnecting" | "disconnected";

let view: View = "boot";
let state: State = initialState();
let conn: Conn = "disconnected";
let terminalMsg = "";
let pairMsg = "";
let pairBusy = false;
let ws: WebSocket | null = null;
let attempt = 0;
let retryTimer: number | undefined;
let retryIn = 0;
let tick: number | undefined;
let dashTimer: number | undefined;

const root = document.getElementById("app") as HTMLElement;

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string | null, ...kids: (Node | string | null)[]): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids) if (k !== null) e.append(k);
  return e;
}
const icon = (c: string) => { const s = h("span", "ico", c); s.setAttribute("aria-hidden", "true"); return s; };

async function api(path: string, method = "GET", body?: unknown): Promise<{ status: number; json: any } | null> {
  try {
    const r = await fetch(path, {
      method, credentials: "same-origin", cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  } catch { return null; }
}

// ---------- flow ----------
async function start() {
  const r = await api("/api/v1/session");
  if (!r) { pairMsg = pairErrorMessage("network"); setView("pairing"); return; }
  if (r.json?.paired) { connect(); setView("dashboard"); } else setView("pairing");
}

function setView(v: View) {
  view = v;
  if (v !== "dashboard" && dash) { videoLost(); vs.phase = "idle"; vs.message = ""; clearInterval(dashTimer); dash = null; for (const t of pending.values()) clearTimeout(t); pending.clear(); }
  if (v === "pairing") { if (!pairPoll && pairMode === "qr") void requestQr(); } else stopPairing();
  render();
}

// ---------- QR pairing ----------
let pairMode: "qr" | "code" = "qr";
let qr: { text: string; expires: number } | null = null;
let qrMsg = "";
let qrDone = false; // denied / rate limited / unreachable: wait for the user instead of polling
let pairPoll: number | undefined;
let pollBusy = false;
let countdownTimer: number | undefined;

function stopPairing() { clearInterval(pairPoll); clearInterval(countdownTimer); pairPoll = undefined; qr = null; qrMsg = ""; qrDone = false; }

async function requestQr() {
  stopPairing(); qrMsg = ""; render();
  const r = await api("/api/v1/pair/request", "POST");
  if (view !== "pairing" || pairMode !== "qr") return;
  if (r && r.status === 200 && typeof r.json?.qr === "string") {
    qr = { text: r.json.qr, expires: Date.parse(r.json.expires_utc) };
    pairPoll = window.setInterval(() => void pollQr(), 1000);
    countdownTimer = window.setInterval(updateCountdown, 1000);
  } else {
    qrDone = true;
    qrMsg = !r ? pairErrorMessage("network") : r.json?.error === "RATE_LIMITED" ? "Too many QR requests. Wait a minute, then try again." : pairErrorMessage(r.status, r.json?.error);
  }
  render();
}

async function pollQr() {
  if (document.hidden || pollBusy || view !== "pairing") return;
  pollBusy = true;
  const r = await api("/api/v1/pair/request/status");
  pollBusy = false;
  if (view !== "pairing" || !qr) return;
  if (!r) { if (!qrMsg) { qrMsg = pairErrorMessage("network"); render(); } return; }
  if (qrMsg && r.status === 200) { qrMsg = ""; render(); }
  switch (r.json?.state) {
    case "approved": stopPairing(); state = initialState(); attempt = 0; connect(); setView("dashboard"); break;
    case "denied": stopPairing(); qrDone = true; qrMsg = "Pairing was denied on the phone."; render(); break;
    case "expired": void requestQr(); break;
  }
}

function updateCountdown() {
  const el = document.getElementById("countdown");
  if (el && qr) el.textContent = `Expires in ${Math.max(0, Math.round((qr.expires - Date.now()) / 1000))} s`;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) void pollQr(); });

async function submitPair(code: string) {
  if (!/^\d{6}$/.test(code)) { pairMsg = pairErrorMessage(400, "BAD_REQUEST"); render(); return; }
  pairBusy = true; pairMsg = ""; render();
  const r = await api("/api/v1/pair", "POST", { code });
  pairBusy = false;
  if (r && r.status === 200 && r.json?.paired) { state = initialState(); attempt = 0; connect(); setView("dashboard"); return; }
  pairMsg = r ? pairErrorMessage(r.status, r.json?.error) : pairErrorMessage("network");
  render();
}

async function unpair() {
  closeWs();
  await api("/api/v1/unpair", "POST", {});
  state = initialState(); pairMsg = ""; setView("pairing");
}

let phoneStats: PhoneStats | null = null;
let phoneStatsAt = 0;

// ---- commands ----
const pending = new Map<string, number>();
let recMsg = "";
const rid = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");

function sendCommand(type: string, payload: object): boolean {
  const id = rid();
  recMsg = "";
  if (!sendWs({ version: 1, type, request_id: id, payload })) { recMsg = "Not connected to the phone."; updateDash(); return false; }
  pending.set(id, window.setTimeout(() => { pending.delete(id); recMsg = "No response from the phone."; updateDash(); }, 10000));
  updateDash();
  return true;
}

function onCommandResult(env: { request_id?: string; payload?: any }) {
  const p = env.payload ?? {};
  const id = env.request_id ?? p.request_id;
  const t = id ? pending.get(id) : undefined;
  if (t === undefined) return;
  clearTimeout(t); pending.delete(id);
  if (!p.ok) recMsg = commandErrorMessage(p.error, p.message);
  updateDash();
}
const sendWs = (o: object) => { if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(o)); return true; } return false; };
initVideo(sendWs, () => updateDash());
function videoLost() {
  if (vs.phase === "connecting" || vs.phase === "live" || vs.phase === "stalled") stopVideo("Video stopped \u2014 connection to the phone was lost.", false);
  phoneStats = null;
}

function closeWs() {
  videoLost();
  clearTimeout(retryTimer); clearInterval(tick);
  if (ws) { ws.onclose = null; ws.onmessage = null; ws.close(); ws = null; }
}

function connect() {
  closeWs();
  conn = attempt === 0 && !state.snapshot ? "disconnected" : "reconnecting";
  const sock = new WebSocket(`wss://${location.host}/api/v1/events`);
  ws = sock;
  sock.onmessage = (m) => {
    try {
      const env = JSON.parse(String(m.data)) as Envelope & { request_id?: string };
      if (env.type === "camera.stats") { phoneStats = env.payload as unknown as PhoneStats; phoneStatsAt = Date.now(); updateDash(); return; }
      if (env.type === "command.result") { onCommandResult(env); return; }
      if (env.type !== "session.snapshot") { handleSignal(env.type, env.request_id, env.payload); return; }
      const next = applyEnvelope(state, env, Date.now());
      conn = "connected"; attempt = 0; state = next;
      if (view === "dashboard") render();
    } catch { /* malformed frame: ignore */ }
  };
  sock.onclose = (ev) => { if (ws === sock) { videoLost(); onClosed(ev.code); } };
  if (view === "dashboard") render();
}

async function onClosed(code: number) {
  ws = null;
  if (isTerminalClose(code)) {
    conn = "disconnected"; terminalMsg = closeMessage(code) ?? ""; setView("terminal"); return;
  }
  conn = "reconnecting";
  // An unauthenticated upgrade fails with a generic close; check whether we are still paired.
  const r = await api("/api/v1/session");
  if (r && !r.json?.paired) { terminalMsg = "This browser is no longer paired. Pair again from the phone."; setView("terminal"); return; }
  retryIn = backoffSeconds(attempt++);
  render();
  clearInterval(tick);
  tick = window.setInterval(() => { retryIn = Math.max(0, retryIn - 1); if (view === "dashboard") render(); }, 1000);
  retryTimer = window.setTimeout(connect, retryIn * 1000);
}

// ---------- rendering ----------
function render() {
  if (view === "dashboard") { if (!dash) dash = buildDash(); if (root.firstChild !== dash.page) root.replaceChildren(dash.page); updateDash(); return; }
  const focused = (document.activeElement as HTMLElement | null)?.dataset?.focus;
  const typed = (document.getElementById("code") as HTMLInputElement | null)?.value ?? "";
  root.replaceChildren(view === "pairing" ? pairingView(typed) : view === "terminal" ? terminalView() : h("p", "boot", "Loading dashboard…"));
  if (focused) (root.querySelector(`[data-focus="${focused}"]`) as HTMLElement | null)?.focus();
}

function brand(extra?: Node) {
  const hd = h("header", "top");
  hd.append(h("div", "brand", icon("◈"), " LOCAL VISION"));
  if (extra) hd.append(extra);
  return hd;
}

function pairingView(typed: string) {
  const tls = h("details", "tls", h("summary", null, "Why does my browser say \u201CNot secure\u201D?"),
    h("p", "muted", "Expected. The phone creates its own certificate, so the browser cannot verify it. The connection is still encrypted and never leaves your local network. To be sure you reached your phone, compare the certificate SHA-256 fingerprint in the browser's certificate viewer with the one shown on the phone's Link tab."));
  const body = pairMode === "qr" ? qrPanel() : codePanel(typed);
  return h("main", "pair-wrap", brand(), h("section", "card pair", h("h1", null, "Pair this browser"), body, tls));
}

function qrPanel() {
  const box = h("div", "qr-box");
  if (qr) { const cv = qrCanvas(qr.text); cv.className = "qr"; cv.setAttribute("role", "img"); cv.setAttribute("aria-label", "Pairing QR code"); box.append(cv); }
  else if (!qrDone) box.append(h("p", "muted", "Requesting a QR code\u2026"));
  const cd = h("p", "muted"); cd.id = "countdown";
  const err = h("p", "err", qrMsg); err.setAttribute("role", "alert");
  const retry = h("button", "btn primary", "Try again"); retry.dataset.focus = "retry"; retry.onclick = () => void requestQr();
  const alt = h("button", "link", "Can't scan? Use a code"); alt.type = "button"; alt.dataset.focus = "alt";
  alt.onclick = () => { stopPairing(); pairMode = "code"; pairMsg = ""; render(); };
  const out = h("div", "qr-panel", h("p", "lead", "On the phone: Link \u2192 Scan to pair, then tap Approve."), box, cd, err);
  if (qrDone) out.append(retry);
  out.append(alt);
  queueMicrotask(updateCountdown);
  return out;
}

function codePanel(typed: string) {
  const input = h("input"); input.id = "code"; input.type = "text"; input.inputMode = "numeric";
  input.autocomplete = "one-time-code"; input.maxLength = 6; input.pattern = "[0-9]{6}"; input.dataset.focus = "code";
  input.value = typed; input.setAttribute("aria-describedby", "pair-help pair-err");
  input.oninput = () => { input.value = input.value.replace(/\D/g, ""); };
  const label = h("label", "lbl", "6-digit pairing code"); label.htmlFor = "code";
  const btn = h("button", "btn primary", pairBusy ? "Pairing\u2026" : "Pair this browser"); btn.type = "submit"; btn.disabled = pairBusy;
  const err = h("p", "err", pairMsg); err.id = "pair-err"; err.setAttribute("role", "alert");
  const form = h("form", "pair-form", label, input, btn, err);
  form.onsubmit = (e) => { e.preventDefault(); void submitPair(input.value.trim()); };
  const help = h("p", "muted", "On the phone: Link \u2192 Use a code instead \u2192 Pair with code, then enter the 6-digit code here. The code is valid for 2 minutes. Pairing replaces any browser already paired.");
  help.id = "pair-help";
  const back = h("button", "link", "Scan a QR code instead"); back.type = "button"; back.dataset.focus = "back";
  back.onclick = () => { pairMode = "qr"; void requestQr(); };
  return h("div", null, help, form, back);
}

function terminalView() {
  const b = h("button", "btn primary", "Pair again"); b.dataset.focus = "again";
  b.onclick = () => { pairMsg = ""; pairMode = "qr"; state = initialState(); setView("pairing"); };
  const msg = h("p", "lead", terminalMsg); msg.setAttribute("role", "alert");
  return h("main", "pair-wrap", brand(), h("section", "card pair", h("h1", null, "Disconnected"), msg, b));
}

function badge(): HTMLElement {
  const m: Record<Conn, [string, string, string]> = { connected: ["●", "Connected", "ok"], reconnecting: ["↻", `Reconnecting…${retryIn > 0 ? ` (retry in ${retryIn}s)` : ""}`, "warn"], disconnected: ["○", "Disconnected", "bad"] };
  const [ic, text, sev] = m[conn];
  const b = h("span", `pill ${sev}`, icon(ic), text);
  const live = h("span", "live", b); live.setAttribute("role", "status"); live.setAttribute("aria-live", "polite");
  return live;
}

function kv(rows: [string, string | Node][]) {
  const dl = h("dl", "kv");
  for (const [k, v] of rows) dl.append(h("dt", null, k), h("dd", null, v));
  return dl;
}
const card = (title: string, ...kids: Node[]) => { const c = h("section", "card", h("h2", "mod", title), ...kids); return c; };
const stateLabel = (s: string | null | undefined) => (s == null ? UNAVAILABLE : s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));

function perm(name: string, v: string | null) {
  const m: Record<string, [string, string, string]> = {
    granted: ["✓", "Granted", "ok"], denied: ["✕", "Denied", "bad"], permanently_denied: ["✕", "Permanently denied", "bad"],
    restricted: ["!", "Restricted", "warn"], limited: ["!", "Limited", "warn"], not_requested: ["–", "Not requested", "neutral"],
  };
  const e = (v && m[v]) || ["?", UNAVAILABLE, "neutral"];
  return [name, h("span", `pill ${e[2]}`, icon(e[0]), e[1])] as [string, Node];
}

interface Dash { page: HTMLElement; hdr: HTMLElement; stale: HTMLElement; status: HTMLElement; overlay: HTMLElement; controls: HTMLElement; tags: HTMLElement; metrics: HTMLElement; phone: HTMLElement; recBox: HTMLElement; otherBox: HTMLElement; recs: HTMLElement; dlg: HTMLDialogElement; grid: HTMLElement }
let dash: Dash | null = null;

/** Replace children only if the markup changed, so buttons are not rebuilt under the pointer every second. */
function patch(el: HTMLElement, nodes: Node[]) {
  const t = document.createElement("div"); t.append(...nodes);
  if (t.innerHTML !== el.innerHTML) {
    const f = (document.activeElement as HTMLElement | null)?.dataset?.focus;
    el.replaceChildren(...t.childNodes);
    if (f) (el.querySelector(`[data-focus="${f}"]`) as HTMLElement | null)?.focus();
  }
}

function buildDash(): Dash {
  const hdr = h("header", "top"), stale = h("div"), status = h("p", "cam-status"), overlay = h("div", "vp-text");
  const vp = h("div", "viewport", videoEl, h("div", "overlay", overlay));
  const controls = h("div", "controls"), tags = h("div", "tags"), metrics = h("div"), phone = h("div");
  const cam = h("section", "card camera", h("h2", "mod", "Camera"), status, vp, controls, tags, metrics, phone);
  const recs = h("div", "contents");
  const recBox = h("div", "contents"), otherBox = h("div", "contents");
  const rail = h("aside", "rail", recBox, otherBox), grid = h("main", "grid", h("div", "main-col", cam, recs), rail);

  const title = h("h2", null, "Stop recording?"); title.id = "dlg-t";
  const keep = h("button", "btn primary", "Keep recording"); keep.type = "button"; keep.autofocus = true;
  const stop = h("button", "btn danger", "Stop recording"); stop.type = "button";
  const dlg = h("dialog", "dlg", title, h("p", "muted", "The recording is finalized and saved on the phone."), h("div", "controls", keep, stop));
  dlg.setAttribute("aria-labelledby", "dlg-t");
  keep.onclick = () => dlg.close();
  stop.onclick = () => { dlg.close(); sendCommand("recording.stop", {}); };
  clearInterval(dashTimer);
  dashTimer = window.setInterval(() => { if (state.snapshot?.recording.state === "recording") updateDash(); }, 1000);
  return { page: h("div", "page", hdr, stale, grid, dlg), hdr, stale, status, overlay, controls, tags, metrics, phone, recBox, otherBox, recs, dlg, grid };
}

const BUSY_REC = ["starting", "recording", "stopping", "finalizing"];

function recordingCard(s: Snapshot, offline: boolean): Node {
  const r = s.recording, st = r.state, on = st === "recording";
  const el = liveElapsed(r, state.receivedAt, Date.now());
  const pill = h("span", `pill tnum ${on ? "rec" : st === "failed" ? "bad" : "neutral"}`, icon(on ? "\u25CF" : st === "failed" ? "!" : "\u25CB"), on ? `REC ${formatElapsed(el)}` : recordingStateLabel(st));
  const rows: [string, string | Node][] = [["State", pill]];
  if (BUSY_REC.includes(st)) rows.push(["Elapsed", formatElapsed(el)], ["Size", fmtBytes(r.bytes)], ["Profile", formatProfile(r.profile)], ["Audio", `\u266A ${formatAudio(r.audio)}`]);
  const kids: Node[] = [kv(rows)];
  const warn = warningMessage(r.warning);
  if (warn) kids.push(banner("warn", "!", warn));
  if (r.error) kids.push(banner("bad", "\u2715", commandErrorMessage(r.error)));
  if (recMsg) kids.push(banner("bad", "\u2715", recMsg));
  const busy = pending.size > 0;
  const rb = h("button", "btn primary", "Record"); rb.dataset.focus = "record"; rb.disabled = offline || busy || BUSY_REC.includes(st);
  rb.onclick = () => void sendCommand("recording.start", {});
  const ctl = h("div", "controls", rb);
  if (on) {
    const sb = h("button", "btn danger", "Stop"); sb.dataset.focus = "stop"; sb.disabled = busy || offline;
    sb.onclick = () => { const d = dash?.dlg; if (d && typeof d.showModal === "function") d.showModal(); else if (confirm("Stop recording?")) sendCommand("recording.stop", {}); };
    ctl.append(sb);
  }
  kids.push(ctl);
  return card("Recording", ...kids);
}

function banner(kind: string, ic: string, text: string) { const b = h("p", `banner ${kind}`, icon(ic), " " + text); b.setAttribute("role", kind === "bad" ? "alert" : "status"); return b; }

function recordingsCard(s: Snapshot): Node {
  const list = s.recordings ?? [];
  const ul = h("ul", "recs");
  if (!list.length) ul.append(h("li", "muted", "No recordings yet."));
  for (const r of list) {
    const meta = [formatLocal(r.created_utc, new Date(), true), formatElapsed(r.duration_ms), fmtBytes(r.size_bytes), r.width && r.height ? `${r.width}\u00D7${r.height}` : null,
      r.fps_measured != null ? `${r.fps_measured.toFixed(1)} fps` : null, r.codec ? r.codec.toUpperCase() : null].filter(Boolean).join(" \u00B7 ");
    ul.append(h("li", null, h("strong", null, r.filename), h("span", "muted", meta)));
  }
  return card("Recordings", ul);
}

function updateDash() {
  const d = dash; if (!d || view !== "dashboard") return;
  const snap = state.snapshot, live = conn !== "connected";
  const dev = snap?.device;
  const model = dev?.model ? [dev.manufacturer, dev.model].filter(Boolean).join(" ") : "Waiting for phone\u2026";
  const un = h("button", "btn", "Unpair this browser"); un.dataset.focus = "unpair"; un.onclick = () => void unpair();
  patch(d.hdr, [h("div", "brand", icon("\u25C8"), " LOCAL VISION"), h("div", "device", model), badge(), h("span", "pill neutral", icon("\u2302"), "LAN only"), un]);
  patch(d.stale, live ? [h("div", "stale-bar", icon("\u23F8"), `Showing last known values \u2014 not live. Last updated ${formatClock(state.receivedAt)}.`)] : []);
  d.grid.classList.toggle("stale", live);

  const cs = snap?.camera.state ?? "not_started";
  patch(d.status, [document.createTextNode(`Camera \u00B7 ${stateLabel(cs)}`)]);
  const ph = vs.phase;
  const ov: Node[] = ph === "live" ? [] : ph === "connecting" ? [h("strong", null, vs.message || "Connecting\u2026")]
    : ph === "stalled" ? [h("strong", null, "No frames arriving"), h("span", "muted", vs.message)]
    : ph === "stopped" ? [h("strong", null, "Video stopped \u00B7 phone camera off"), h("span", "muted", vs.message)]
    : [h("strong", null, "Video not started"), h("span", "muted", "Click Start video to watch the phone camera.")];
  patch(d.overlay, ov);
  d.overlay.parentElement?.classList.toggle("hidden", ph === "live");

  const blocked = live ? "Not connected to the phone." : null;
  const hint = !live ? cameraHint(snap?.camera) : null;
  const active = ph === "connecting" || ph === "live" || ph === "stalled";
  const btn = h("button", active ? "btn" : "btn primary", active ? "Stop video" : ph === "stopped" ? "Start video again" : "Start video");
  btn.dataset.focus = "video"; btn.disabled = !active && blocked !== null;
  btn.onclick = () => (active ? stopVideo("Video stopped.") : void startVideo());
  patch(d.controls, [btn, ...(blocked && !active ? [h("span", "muted", blocked)] : hint && !active ? [h("span", "muted", hint)] : [])]);

  const st = vs.stats;
  const webrtc = ph === "live" && st?.codec ? `WebRTC \u00B7 ${st.codec}${st.width && st.height ? ` ${st.width}\u00D7${st.height}` : ""}` : `WebRTC \u00B7 ${ph === "connecting" ? "Connecting" : "Not started"}`;
  patch(d.tags, [
    ...(ph === "live" ? [h("span", "pill ok", icon("\u25CF"), "LIVE")] : []),
    h("span", "tag", webrtc),
    snap?.recording.state === "recording" ? h("span", "pill rec", icon("\u25CF"), `Local master \u00B7 ${formatProfile(snap.recording.profile)}`)
      : h("span", "tag", "Local master \u00B7 " + recordingStateLabel(snap?.recording.state ?? "idle")),
  ]);
  const strip = (r: [string, string][]) => h("dl", "kv strip", ...r.map(([k, v]) => h("div", "m", h("dt", null, k), h("dd", null, v))));
  patch(d.metrics, ph === "idle" ? [] : [strip(metricRows(ph === "stopped" ? null : st))]);
  const phoneOk = !live && Date.now() - phoneStatsAt < 5000 && (cs === "previewing" || cs === "transmitting");
  patch(d.phone, snap ? [h("p", "sub", "Phone encoder"), strip(phoneEncoderRows(phoneOk ? phoneStats : null))] : []);

  if (!snap) { patch(d.otherBox, [card("Waiting", h("p", "muted", "Waiting for the first snapshot from the phone\u2026"))]); return; }
  patch(d.recBox, [recordingCard(snap, live)]);
  patch(d.recs, [recordingsCard(snap)]);
  patch(d.otherBox, railCards(snap));
}

function railCards(s: Snapshot): Node[] {
  const v = s.viewer, n = s.network;
  const events = h("ul", "events");
  if (state.events.length === 0) events.append(h("li", "muted", "No changes yet."));
  for (const e of state.events) events.append(h("li", null, h("time", null, formatClock(e.at)), " ", e.text));
  const batt = `${formatPercent(s.battery.level_percent)} \u00B7 ${powerLabel(s.battery)}`;
  return [
    card("Session", kv([
      ["App state", stateLabel(s.app)], ["Started", formatLocal(s.session_started_utc, new Date(), true)],
      ["Screen kept on", s.keep_screen_on == null ? UNAVAILABLE : s.keep_screen_on ? "Yes" : "No"],
      perm("Camera permission", s.permissions.camera), perm("Microphone permission", s.permissions.microphone),
    ])),
    card("Voice", kv([["State", stateLabel(s.voice.state)]])),
    card("Device health", kv([
      ["Battery", batt], ["Thermal", stateLabel(s.thermal.status)],
      ["Storage free", `${formatBytes(s.storage.free_bytes)} of ${formatBytes(s.storage.total_bytes)}`],
      ["Device", [s.device.os, s.device.app_version && `app ${s.device.app_version}`].filter(Boolean).join(" · ") || UNAVAILABLE],
    ])),
    card("Connection", kv([
      ["Phone", n.addresses.length ? n.addresses.map((a) => `${a}${n.port ? ":" + n.port : ""}`).join(", ") : UNAVAILABLE],
      ["TLS", n.tls === "self_signed" ? "Self-signed" : stateLabel(n.tls)],
      ["Fingerprint", h("code", "fp", n.cert_sha256 ?? UNAVAILABLE)],
      ["Paired since", formatLocal(v.paired_since_utc, new Date(), true)], ["Expires", formatLocal(v.expires_utc, new Date(), true)],
    ])),
    card("Recent events", events),
  ];
}

void start();
