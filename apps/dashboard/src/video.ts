// Browser WebRTC receiver. Signaling rides the events socket (provided via init).
import { computeStats, ICE_MESSAGE, preferH264, stopReasonMessage, webrtcErrorMessage, type StatsSample, type VideoStats } from "./state.js";

export type Phase = "idle" | "connecting" | "live" | "stalled" | "stopped";
export const view: { phase: Phase; message: string; stats: VideoStats | null } = { phase: "idle", message: "", stats: null };

export const videoEl = document.createElement("video");
videoEl.autoplay = true; videoEl.muted = true; videoEl.playsInline = true; videoEl.controls = false;
videoEl.setAttribute("aria-label", "Live camera video");

let send: (o: object) => boolean = () => false;
let changed: () => void = () => {};
export function initVideo(sendFn: (o: object) => boolean, onChange: () => void) { send = sendFn; changed = onChange; }

let pc: RTCPeerConnection | null = null;
let reqId = "";
let gen = 0;
let statsTimer: number | undefined, answerTimer: number | undefined, discTimer: number | undefined;
let sample: StatsSample | null = null;
let lastFrames = -1, lastAdvance = 0;

const rid = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
function set(phase: Phase, message = "") { view.phase = phase; view.message = message; changed(); }

function teardown() {
  gen++;
  clearInterval(statsTimer); clearTimeout(answerTimer); clearTimeout(discTimer);
  if (pc) { pc.ontrack = null; pc.oniceconnectionstatechange = null; pc.close(); pc = null; }
  videoEl.srcObject = null;
  sample = null; lastFrames = -1; view.stats = null;
}

/** Stop and show a message. notify: tell the phone (best effort). */
export function stopVideo(message: string, notify = true) {
  const had = pc !== null;
  if (had && notify) send({ version: 1, type: "webrtc.stop", request_id: rid(), payload: {} });
  teardown();
  set("stopped", message);
}

export async function startVideo() {
  if (pc) return;
  teardown();
  const my = gen;
  set("connecting", "Starting phone camera\u2026");
  try {
    const p = new RTCPeerConnection({ iceServers: [] });
    pc = p;
    const t = p.addTransceiver("video", { direction: "recvonly" });
    const caps = RTCRtpReceiver.getCapabilities("video");
    if (caps && t.setCodecPreferences) t.setCodecPreferences(preferH264(caps.codecs));
    p.ontrack = (e) => { videoEl.srcObject = e.streams[0] ?? new MediaStream([e.track]); void videoEl.play().catch(() => {}); };
    p.oniceconnectionstatechange = () => {
      const s = p.iceConnectionState;
      clearTimeout(discTimer);
      if (s === "failed") stopVideo(`Video stopped. ${ICE_MESSAGE}`);
      else if (s === "disconnected") discTimer = window.setTimeout(() => stopVideo(`Video stopped. ${ICE_MESSAGE}`), 5000);
    };
    await p.setLocalDescription(await p.createOffer());
    await new Promise<void>((res) => {
      if (p.iceGatheringState === "complete") return res();
      const done = () => { if (p.iceGatheringState === "complete") { p.removeEventListener("icegatheringstatechange", done); res(); } };
      p.addEventListener("icegatheringstatechange", done);
      setTimeout(res, 3000);
    });
    if (my !== gen || !p.localDescription) return;
    reqId = rid();
    if (!send({ version: 1, type: "webrtc.offer", request_id: reqId, payload: { sdp: p.localDescription.sdp } })) { stopVideo("Video stopped — not connected to the phone.", false); return; }
    answerTimer = window.setTimeout(() => stopVideo("The phone did not answer. Try again."), 12000);
    statsTimer = window.setInterval(() => void tick(), 1000);
  } catch {
    if (my === gen) stopVideo("Could not set up video in this browser.", false);
  }
}

async function tick() {
  const p = pc; if (!p) return;
  const r = await p.getStats();
  if (p !== pc) return;
  const { model, sample: s } = computeStats([...r.values()], sample);
  sample = s; view.stats = model;
  const now = Date.now();
  if (model.framesDecoded != null && model.framesDecoded > lastFrames) {
    if (lastFrames >= 0 || model.framesDecoded > 0) { lastAdvance = now; if (view.phase !== "live") { view.phase = "live"; view.message = ""; } }
    lastFrames = model.framesDecoded;
  } else if (view.phase === "live" && now - lastAdvance > 3000) { view.phase = "stalled"; view.message = "No frames arriving."; }
  changed();
}

/** Handle phone->browser signaling. Returns true if consumed. */
export function handleSignal(type: string, requestId: string | undefined, payload: any): boolean {
  if (!type.startsWith("webrtc.")) return false;
  const p = pc; if (!p) return true;
  if (type === "webrtc.answer") {
    if (requestId && requestId !== reqId) return true;
    clearTimeout(answerTimer);
    view.message = "Connecting\u2026"; changed();
    p.setRemoteDescription({ type: "answer", sdp: String(payload?.sdp ?? "") }).catch(() => stopVideo(webrtcErrorMessage("NEGOTIATION_FAILED")));
  } else if (type === "webrtc.error") {
    if (!requestId || requestId === reqId) stopVideo(webrtcErrorMessage(payload?.code, payload?.message), false);
  } else if (type === "webrtc.stopped") stopVideo(stopReasonMessage(payload?.reason), false);
  return true;
}
