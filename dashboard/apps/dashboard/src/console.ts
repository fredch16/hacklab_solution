// "Peter" studio: the laptop dashboard's home screen. The live 3-D twin and the robot camera side by side, what
// the director is doing right now and which modes are on in the top bar, and the agent (type, or say the wake
// word on the phone) in the dock. Everything else (director tools, phone, manual arm control, settings) lives
// in the menu. Built around the existing robot card and phone dashboard, which keep their own logic.
import { mountTwin } from "./twin3d.js";

const JOBS: Record<string, string> = {
  show: "Cinematic show", gimbal: "Gimbal take", film: "Claude's shot", tour: "Director tour", watch: "Keep in view",
  follow: "Follow object", selfcal: "Self-calibration", snap: "Snap to view", cinematic: "Cinematic shots",
  survey: "Finding the best view", recall: "Recall view", goto: "Going to a view", scene: "Scene", center: "Framing",
  calibrate: "Calibration tour", return: "Returning",
};
const EXAMPLES = ["follow the object", "go to the left view", "do a gimbal take", "cinematic show", "keep it in view",
  "film a shot: slow reveal from the right"];

type Orb = "idle" | "listening" | "thinking" | "working" | "done" | "error";

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, ...kids: (Node | string)[]) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  e.append(...kids);
  return e;
}

export function mountConsole(robotCard: HTMLElement, phoneNodes: HTMLElement[]) {
  document.body.classList.add("studio");
  const feed = robotCard.querySelector<HTMLElement>("[data-role=feed]")!;
  const actions = robotCard.querySelector<HTMLElement>("[data-role=actions]")!;
  const recStatus = robotCard.querySelector<HTMLElement>("[data-role=rec-status]")!;

  // ---- top bar: brand, live modes, the current task, menu ----
  const logo = el("img", "s-logo"); logo.src = "./peter_icon.png"; logo.alt = "";
  const chips = el("div", "s-chips"); chips.setAttribute("aria-label", "Active modes");
  const taskName = el("strong"), taskStep = el("span", "s-task-step");
  const task = el("div", "s-task", el("span", "s-task-dot"), el("div", "s-task-text", taskName, taskStep));
  task.setAttribute("role", "status"); task.setAttribute("aria-live", "polite");
  const stop = el("button", "s-stop", "STOP"); stop.type = "button"; stop.title = "Stop the arm now";
  const full = el("button", "s-full", "⛶"); full.type = "button"; full.title = "Full screen (F)"; full.setAttribute("aria-label", "Full screen");
  const toggleFull = () => document.fullscreenElement ? void document.exitFullscreen() : void document.documentElement.requestFullscreen().catch(() => {});
  full.onclick = toggleFull;
  document.addEventListener("fullscreenchange", () => full.classList.toggle("on", !!document.fullscreenElement));
  document.addEventListener("keydown", (e) => {
    if ((e.key === "f" || e.key === "F") && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement)) toggleFull();
  });
  const menuBtn = el("button", "s-menu-btn", el("span", "s-burger"), "Menu"); menuBtn.type = "button";
  menuBtn.setAttribute("aria-expanded", "false");
  const top = el("header", "s-top", el("div", "s-brand", logo, el("div", "", el("b", "", "Peter"), el("small", "", "camera robot studio"))),
    chips, task, stop, full, menuBtn);

  // ---- hero: twin | camera ----
  const twinHost = el("div", "s-twin-canvas");
  const readout = el("div", "s-readout");
  const twin = el("section", "s-panel s-twin", el("div", "s-panel-head", el("h2", "", "Digital twin"), el("span", "s-live", "live")),
    twinHost, readout);
  const recBadge = el("div", "s-recbadge", el("span", "s-recdot"), "REC");
  const cam = el("section", "s-panel s-cam", el("div", "s-panel-head", el("h2", "", "Robot camera"), recBadge), feed, actions, recStatus);
  const hero = el("main", "s-hero", twin, cam);

  // ---- dock: the agent ----
  const orb = el("div", "s-orb", el("span", "s-orb-core"), el("span", "s-orb-ring"), el("span", "s-orb-ring r2"));
  orb.setAttribute("aria-hidden", "true");
  const say = el("p", "s-say", "Hi, I'm Peter. Tell me what to film.");
  const input = el("input", "s-input"); input.placeholder = `Ask Peter… e.g. “${EXAMPLES[0]}”`; input.maxLength = 500;
  input.setAttribute("aria-label", "Command for Peter");
  const send = el("button", "s-send", "Send"); send.type = "submit";
  const form = el("form", "s-ask", input, send);
  const hint = el("p", "s-hint", "or say the wake word to the phone");
  const sugg = el("div", "s-sugg", ...EXAMPLES.slice(1, 5).map((x) => {
    const b = el("button", "s-chip-btn", x); b.type = "button"; b.onclick = () => { input.value = x; form.requestSubmit(); }; return b;
  }));
  const dock = el("footer", "s-dock", orb, el("div", "s-dock-main", say, form, el("div", "s-dock-foot", hint, sugg)));

  // ---- menu ----
  const tabs: [string, string][] = [["director", "Director tools"], ["phone", "Phone & voice"], ["arm", "Arm"], ["settings", "Settings"]];
  const panes = new Map<string, HTMLElement>();
  const tabBar = el("nav", "s-tabs");
  for (const [id, label] of tabs) {
    const b = el("button", "s-tab", label); b.type = "button"; b.dataset.tab = id;
    b.onclick = () => openTab(id);
    tabBar.append(b);
    panes.set(id, el("div", "s-pane"));
  }
  panes.get("director")!.append(robotCard);
  panes.get("phone")!.append(...phoneNodes);
  const link = (text: string, href: string, note: string) => {
    const a = el("a", "s-link", el("b", "", text), el("small", "", note)); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
  };
  panes.get("arm")!.append(
    link("Manual arm control", "http://127.0.0.1:8890/control", "Saved views, speeds, recover, hold, limp"),
    link("Capture views", "http://127.0.0.1:8890/", "Teach and check the arm's views"),
    link("Station", "http://127.0.0.1:8889/", "NormaCore Station: buses, calibration, cameras"));
  panes.get("settings")!.append(
    link("Claude API key", "/laptop/claude-settings", "The key the planner and voice bridge use"),
    el("p", "s-note", "The wake name is set in Phone & voice. Speech stays on the phone; nothing goes to a cloud speech service."));
  const closeBtn = el("button", "s-close", "×"); closeBtn.type = "button"; closeBtn.setAttribute("aria-label", "Close menu");
  const drawer = el("aside", "s-drawer", el("div", "s-drawer-head", el("b", "", "Menu"), closeBtn), tabBar, ...panes.values());
  drawer.setAttribute("aria-label", "Menu");
  const scrim = el("div", "s-scrim");
  function setOpen(open: boolean) {
    document.body.classList.toggle("s-open", open);
    menuBtn.setAttribute("aria-expanded", String(open));
    if (open) closeBtn.focus();
  }
  function openTab(id: string) {
    for (const [k, p] of panes) p.hidden = k !== id;
    for (const b of tabBar.querySelectorAll<HTMLElement>(".s-tab")) b.classList.toggle("on", b.dataset.tab === id);
    try { localStorage.setItem("peter.tab", id); } catch { /* private window */ }
  }
  let saved = "director"; try { saved = localStorage.getItem("peter.tab") || saved; } catch { /* default */ }
  openTab(panes.has(saved) ? saved : "director");
  menuBtn.onclick = () => setOpen(!document.body.classList.contains("s-open"));
  closeBtn.onclick = scrim.onclick = () => setOpen(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });

  document.body.prepend(el("div", "s-app", top, hero, dock), scrim, drawer);

  // ---- live state: orb, chips, current task ----
  let orbState: Orb = "idle", orbUntil = 0;
  const setOrb = (s: Orb, line?: string, holdMs = 0) => {
    orbState = s; orb.dataset.state = s; dock.dataset.state = s;
    if (line !== undefined) say.textContent = line;
    orbUntil = holdMs ? Date.now() + holdMs : 0;
  };
  setOrb("idle");
  taskName.textContent = "Ready"; taskStep.textContent = "waiting for a command"; task.dataset.status = "idle";
  const modes = { gate: "", tracking: null as number | null, rec: false, phone: "", voice: "", job: null as any };
  function renderChips() {
    const c: [string, string, string][] = [];
    if (modes.gate) c.push([modes.gate === "PHYSICAL" ? "on" : "warn", "◆", modes.gate === "PHYSICAL" ? "Moves on" : modes.gate]);
    if (modes.tracking !== null) c.push(["on", "◎", `Tracking ${Math.round(modes.tracking * 100)}%`]);
    else c.push(["off", "◎", "Not tracking"]);
    if (modes.rec) c.push(["rec", "●", "Recording"]);
    if (modes.voice) c.push([modes.voice === "listening" ? "on" : "off", "◖", modes.voice === "listening" ? "Wake word on" : `Voice ${modes.voice}`]);
    c.push([modes.phone === "connected" ? "on" : "off", "▣", modes.phone === "connected" ? "Phone" : "Phone off"]);
    const html = c.map(([k, i, t]) => `<span class="s-mode ${k}"><i>${i}</i>${t}</span>`).join("");
    if (chips.innerHTML !== html) chips.innerHTML = html;
  }
  function renderTask(j: { kind: string; status: string; step: string } | null) {
    const running = j?.status === "RUNNING";
    task.dataset.status = j ? j.status.toLowerCase() : "idle";
    taskName.textContent = j ? (JOBS[j.kind] ?? j.kind) : "Ready";
    taskStep.textContent = !j ? "waiting for a command" : running ? (j.step || "working…") : j.status === "HOLD" ? `on hold · ${j.step}` : j.status.toLowerCase();
    if (running && orbState !== "listening" && orbState !== "thinking") setOrb("working", `${taskName.textContent}: ${taskStep.textContent}`);
    else if (!running && orbState === "working" && Date.now() > orbUntil) setOrb("idle");
  }
  void mountTwin(twinHost, (t) => {
    const o = t?.object;
    readout.innerHTML = !t?.ok ? "<span>arm data unavailable</span>"
      : o ? `<span>${t.target ?? "object"}</span><b>${o.point_m.map((v) => (v * 100).toFixed(0)).join(" · ")}</b><span>cm · ±${(o.sigma_m * 100).toFixed(0)}</span>`
      : `<span>${t.target ? `${t.target}: locating…` : "no object marked"}</span>`;
    renderTask(t?.job ?? null);
  });
  async function pollState() {
    try {
      const r = await fetch("/laptop/director/state", { cache: "no-store", signal: AbortSignal.timeout(2000) });
      const d = await r.json();
      modes.gate = d?.gate?.mode ?? "";
      const obs = d?.target?.observation;
      modes.tracking = d?.target?.status === "TRACKING" && obs ? Math.min(1, obs.confidence ?? 1) : null;
      renderTask(d?.job ? { kind: d.job.kind, status: d.job.status, step: d.job.step ?? "" } : null);
    } catch { modes.gate = ""; }
    modes.rec = /Recording/.test(recStatus.textContent ?? "");
    recBadge.classList.toggle("on", modes.rec);
    renderChips();
  }
  window.setInterval(() => { if (!document.hidden) void pollState(); }, 1000);
  void pollState();

  document.addEventListener("peter-phone", (e) => {
    const d = (e as CustomEvent).detail ?? {};
    modes.phone = d.conn ?? ""; modes.voice = d.voice ?? "";
    renderChips();
  });
  document.addEventListener("peter-voice", (e) => {
    const v = (e as CustomEvent).detail as { mode: string; title: string; stable?: string; tail?: string; result?: { kind: string; text: string } } | null;
    if (!v) return;
    const heard = [v.stable, v.tail].filter(Boolean).join(" ");
    if (v.result) setOrb(v.result.kind === "ok" ? "done" : "error", v.result.text, 4000);
    else if (v.mode === "wake" || v.mode === "listening") setOrb("listening", heard || "Listening…");
    else setOrb("thinking", heard || v.title);
  });

  // ---- typed commands: the same path as spoken ones (voice bridge -> Claude/MCP -> director) ----
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = ""; send.disabled = true;
    setOrb("thinking", `“${text}”`);
    try {
      const id = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
      const r = await fetch("/laptop/arm/voice", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: AbortSignal.timeout(30000),
        body: JSON.stringify({ session_id: "studio", utterance_id: `typed-${id}`, text }),
      });
      const d = await r.json().catch(() => ({}));
      const ok = r.ok && d.ok !== false && d.command !== "none";
      setOrb(ok ? "done" : "error", d.message || (d.command === "none" ? "I didn't catch a camera command in that." : d.error || "Something went wrong."), 5000);
    } catch {
      setOrb("error", "The voice bridge is not answering.", 5000);
    } finally { send.disabled = false; input.focus(); }
  };
  stop.onclick = async () => {
    stop.classList.add("hit");
    try { await fetch("/laptop/arm/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); setOrb("done", "Stopped. The arm is holding.", 4000); }
    catch { setOrb("error", "Stop could not be confirmed. Use Manual arm control.", 6000); }
    setTimeout(() => stop.classList.remove("hit"), 600);
  };
  window.setInterval(() => { if (orbUntil && Date.now() > orbUntil) { orbUntil = 0; if (orbState === "done" || orbState === "error") setOrb("idle"); } }, 500);
}
