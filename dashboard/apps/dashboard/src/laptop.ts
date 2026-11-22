import { armResult, type VoiceCommandResult } from "./voice.js";
import { directorPanel } from "./director.js";

export const laptop = document.documentElement.dataset.host === "laptop";
const tab = laptop ? crypto.randomUUID().replaceAll("-", "") : "";
export const robotTab = () => tab;
export const socketUrl = (path: string) => `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`;

type RobotState = { recording: boolean; busy: boolean; seconds: number; clip: string | null; error: string | null };

export async function laptopRecord(camera: "phone" | "robot", action: "start" | "stop"): Promise<VoiceCommandResult> {
  const command = camera === "robot" ? `robot.recording.${action}` : `recording.${action}`;
  if (!laptop) return { command, ok: false, error: "LAPTOP_REQUIRED", message: "Use the laptop dashboard to record the robot camera." };
  try {
    const response = await fetch("/laptop/recording", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({camera, action, tab}),
      signal: AbortSignal.timeout(10000),
    });
    return {...armResult(await response.json()), command};
  } catch {
    return { command, ok: false, error: "RECORDING_FAILED", message: "The laptop recorder is unreachable." };
  }
}

const element = <T extends keyof HTMLElementTagNameMap>(tag: T, text?: string) => {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  return node;
};

export function robotCard(): HTMLElement {
  const card = element("section");
  card.className = "card camera robot-camera";
  const title = element("h2", "Robot camera");
  title.className = "mod";
  const image = element("img");
  image.alt = "Live robot camera";
  image.hidden = true;
  const frame = element("div");
  frame.className = "robot-frame";
  const feed = element("p", "Waiting for the robot camera…");
  feed.className = "muted";
  frame.append(feed, image);
  const status = element("p");
  status.setAttribute("role", "status");
  const note = element("p");
  note.setAttribute("role", "status");
  const buttons = element("div");
  buttons.className = "controls";
  const record = element("button", "Record robot camera");
  record.className = "btn primary";
  record.disabled = true;
  const stopArm = element("button", "Stop arm");
  stopArm.className = "btn danger";
  const arm = element("a", "Arm controls");
  arm.href = "http://127.0.0.1:8890/control";
  arm.target = "_blank";
  arm.rel = "noopener noreferrer";
  arm.className = "link";
  const settings = element("a", "Claude API key settings");
  settings.href = "/laptop/claude-settings";
  settings.target = "_blank";
  settings.rel = "noopener noreferrer";
  settings.className = "link";
  const clip = element("a", "Download last robot clip");
  clip.hidden = true;
  buttons.append(record, stopArm, clip, arm, settings);
  card.append(title, frame, buttons, status, note);

  let inView = false, live = false, lastFrame = 0, href = "", commandPending = false, polling = false;
  let ws: WebSocket | null = null;
  let robot: RobotState | null = null;
  const viewing = () => inView && !document.hidden && card.isConnected;

  function disconnect() {
    const old = ws;
    ws = null;
    old?.close();
    if (href) URL.revokeObjectURL(href);
    href = "";
    live = false;
    image.hidden = true;
    image.removeAttribute("src");
    feed.hidden = false;
    feed.textContent = viewing() ? "Robot camera unavailable" : "Robot camera paused while out of view";
    record.disabled = true;
  }

  function connect() {
    if (!viewing() || ws) return;
    const sock = new WebSocket(socketUrl(`/laptop/robot/events/${tab}`));
    ws = sock;
    sock.onmessage = ({data}) => {
      if (ws !== sock || !viewing() || !(data instanceof Blob)) return;
      if (href) URL.revokeObjectURL(href);
      href = URL.createObjectURL(data);
      image.src = href;
    };
    sock.onclose = () => { if (ws === sock) disconnect(); };
    sock.onerror = () => { if (ws === sock) disconnect(); };
  }
  image.onload = () => {
    if (!viewing() || !ws || ws.readyState !== WebSocket.OPEN) return;
    if (!live) ws.send("viewing");
    lastFrame = performance.now();
    live = true;
    feed.hidden = true;
    image.hidden = false;
    record.disabled = commandPending || (!!robot?.busy && !robot.recording);
  };

  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const response = await fetch(`/laptop/robot/state/${tab}`, {cache: "no-store", signal: AbortSignal.timeout(2000)});
      if (!response.ok) return;
      robot = await response.json() as RobotState;
      record.textContent = robot.recording ? "Stop robot recording" : "Record robot camera";
      record.disabled = commandPending || !live || (robot.busy && !robot.recording);
      status.textContent = robot.recording ? `Recording · ${robot.seconds} s` : robot.error ?? (robot.busy ? "Recording in another tab" : "");
      if (robot.clip) {
        clip.href = `/laptop/clips/${encodeURIComponent(robot.clip)}`;
        clip.hidden = false;
      }
    } catch { status.textContent = "Laptop recorder unavailable"; }
    finally { polling = false; }
  }
  record.onclick = async () => {
    commandPending = true;
    record.disabled = true;
    const result = await laptopRecord("robot", robot?.recording ? "stop" : "start");
    note.textContent = result.message ?? "";
    commandPending = false;
    await poll();
  };
  stopArm.onclick = async () => {
    try {
      const response = await fetch("/laptop/arm/stop", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
        signal: AbortSignal.timeout(3000),
      });
      const result = await response.json();
      note.textContent = response.ok && result.ok ? "Arm holding" : "Stop could not be confirmed";
    } catch { note.textContent = "Stop could not be confirmed"; }
  };

  new IntersectionObserver(([entry]) => {
    inView = entry?.isIntersecting === true && entry.intersectionRatio >= 0.25;
    if (viewing()) connect();
    else disconnect();
  }, {threshold: [0, 0.25]}).observe(frame);
  document.addEventListener("visibilitychange", () => { if (viewing()) connect(); else disconnect(); });
  window.addEventListener("pagehide", disconnect);
  window.setInterval(() => {
    if (viewing() && live && performance.now() - lastFrame < 1700 && ws?.readyState === WebSocket.OPEN) ws.send("viewing");
    else if (live) disconnect();
    if (viewing() && !ws) connect();
    void poll();
  }, 1000);
  // Camera director (capture_server.py --director). Its cinematic take uses this card's own robot recorder.
  directorPanel(card, frame, image, () => laptopRecord("robot", "start").then(poll), () => laptopRecord("robot", "stop").then(poll),
    () => !!robot?.recording);
  return card;
}
