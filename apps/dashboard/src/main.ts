import { qrCanvas } from "./qr.js";
import {
  applyEnvelope, backoffSeconds, closeMessage, formatBytes, formatClock, formatLocal, formatPercent,
  initialState, isTerminalClose, pairErrorMessage, powerLabel, UNAVAILABLE, type Envelope, type Snapshot, type State,
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

function closeWs() {
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
      const next = applyEnvelope(state, JSON.parse(String(m.data)) as Envelope, Date.now());
      conn = "connected"; attempt = 0; state = next;
      if (view === "dashboard") render();
    } catch { /* malformed frame: ignore */ }
  };
  sock.onclose = (ev) => { if (ws === sock) onClosed(ev.code); };
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
  const focused = (document.activeElement as HTMLElement | null)?.dataset?.focus;
  const typed = (document.getElementById("code") as HTMLInputElement | null)?.value ?? "";
  root.replaceChildren(view === "pairing" ? pairingView(typed) : view === "terminal" ? terminalView() : view === "dashboard" ? dashboardView() : h("p", "boot", "Loading dashboard…"));
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

function dashboardView() {
  const snap = state.snapshot;
  const stale = conn !== "connected";
  const dev = snap?.device;
  const model = dev?.model ? [dev.manufacturer, dev.model].filter(Boolean).join(" ") : "Waiting for phone…";

  const hd = h("header", "top");
  hd.append(h("div", "brand", icon("◈"), " LOCAL VISION"), h("div", "device", model), badge(), h("span", "pill neutral", icon("⌂"), "LAN only"));
  const un = h("button", "btn", "Unpair this browser"); un.dataset.focus = "unpair"; un.onclick = () => void unpair();
  hd.append(un);

  const main = h("main", "grid" + (stale ? " stale" : ""));
  if (stale) {
    main.before(h("div", "stale-bar", icon("⏸"), `Showing last known values — not live. Last updated ${formatClock(state.receivedAt)}.`));
  }

  const cam = h("section", "card camera");
  const vp = h("div", "viewport", h("div", "vp-text", h("strong", null, `Camera · ${stateLabel(snap?.camera.state ?? "not_started")}`), h("span", "muted", "No video yet. Camera streaming arrives in a later release.")));
  vp.setAttribute("role", "img"); vp.setAttribute("aria-label", "Camera viewport: no video, camera not started");
  cam.append(h("h2", "mod", "Camera"), vp, h("div", "tags",
    h("span", "tag", "WebRTC · " + stateLabel(snap?.camera.state ?? "not_started")),
    h("span", "tag", "Local master · " + stateLabel(snap?.recording.state ?? "not_started"))));

  const rail = h("aside", "rail");
  if (snap) rail.append(...railCards(snap)); else rail.append(card("Waiting", h("p", "muted", "Waiting for the first snapshot from the phone…")));
  main.append(h("div", "main-col", cam), rail);

  const wrap = h("div", "page", hd, main);
  if (stale) wrap.prepend(h("div", "sr-only", "Values are stale"));
  return wrap;
}

function railCards(s: Snapshot): Node[] {
  const v = s.viewer, n = s.network;
  const recActive = s.recording.state === "recording";
  const rec = h("span", `pill ${recActive ? "rec" : "neutral"}`, icon(recActive ? "●" : "○"), stateLabel(s.recording.state));
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
    card("Recording", kv([["State", rec]])),
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
