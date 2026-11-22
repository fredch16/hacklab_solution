// Camera director panel for the robot camera card (laptop dashboard). The capture server's
// camera_director owns tracking, scoring and every arm move; this file only draws, selects boxes
// in source-image coordinates and starts or stops jobs.

export type Fit = "contain" | "cover" | "fill";
export type Display = { width: number; height: number; natW: number; natH: number; fit: Fit; rotate?: 0 | 90 | 180 | 270; mirror?: boolean };
export type Box = [number, number, number, number];   // x1, y1, x2, y2, normalized 0..1 of the source frame

// Where the (rotated) image's content lands inside the element box, in element pixels.
function placement(d: Display) {
  const turned = d.rotate === 90 || d.rotate === 270;
  const w = turned ? d.natH : d.natW, h = turned ? d.natW : d.natH;
  if (d.fit === "fill") return { ox: 0, oy: 0, sx: d.width / w, sy: d.height / h, w, h };
  const s = d.fit === "contain" ? Math.min(d.width / w, d.height / h) : Math.max(d.width / w, d.height / h);
  return { ox: (d.width - w * s) / 2, oy: (d.height - h * s) / 2, sx: s, sy: s, w, h };
}

// Element pixel (relative to the element's top left) -> normalized source point, or null outside the picture.
export function toSource(d: Display, px: number, py: number): [number, number] | null {
  if (!(d.natW > 0 && d.natH > 0 && d.width > 0 && d.height > 0)) return null;
  const p = placement(d);
  let u = (px - p.ox) / (p.w * p.sx), v = (py - p.oy) / (p.h * p.sy);
  if (u < 0 || u > 1 || v < 0 || v > 1) return null;
  if (d.mirror) u = 1 - u;
  switch (d.rotate ?? 0) {
    case 90: [u, v] = [v, 1 - u]; break;
    case 180: [u, v] = [1 - u, 1 - v]; break;
    case 270: [u, v] = [1 - v, u]; break;
  }
  return [u, v];
}

// Normalized source point -> element pixel (may lie outside the element under "cover").
export function toView(d: Display, u: number, v: number): [number, number] {
  switch (d.rotate ?? 0) {
    case 90: [u, v] = [1 - v, u]; break;
    case 180: [u, v] = [1 - u, 1 - v]; break;
    case 270: [u, v] = [v, 1 - u]; break;
  }
  if (d.mirror) u = 1 - u;
  const p = placement(d);
  return [p.ox + u * p.w * p.sx, p.oy + v * p.h * p.sy];
}

// A drag from (ax, ay) to (bx, by) in element pixels -> source box, clipped to the picture; null when the
// drag starts outside the picture or the box is smaller than minPx source pixels on a side.
export function dragToBox(d: Display, ax: number, ay: number, bx: number, by: number, minPx = 12): Box | null {
  const a = toSource(d, ax, ay);
  if (!a) return null;
  const p = placement(d);
  const cx = Math.min(Math.max(bx, p.ox), p.ox + p.w * p.sx), cy = Math.min(Math.max(by, p.oy), p.oy + p.h * p.sy);
  const b = toSource(d, Math.min(Math.max(cx, 0), d.width), Math.min(Math.max(cy, 0), d.height));
  if (!b) return null;
  const box: Box = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
  if ((box[2] - box[0]) * d.natW < minPx || (box[3] - box[1]) * d.natH < minPx) return null;
  return box;
}

// Source box -> element rectangle [left, top, width, height] for drawing.
export function boxToView(d: Display, b: Box): [number, number, number, number] {
  const corners = [toView(d, b[0], b[1]), toView(d, b[2], b[3]), toView(d, b[0], b[3]), toView(d, b[2], b[1])];
  const xs = corners.map((c) => c[0]), ys = corners.map((c) => c[1]);
  const l = Math.min(...xs), t = Math.min(...ys);
  return [l, t, Math.max(...xs) - l, Math.max(...ys) - t];
}

// ------------------------------------------------------------------ panel

type Obs = { box: Box; confidence: number; method: string; evidence: string; age_s: number | null; stable: boolean };
type View = { valid: boolean; score: number | null; reason?: string; thumb?: string; measured_joints?: Record<string, number>;
  toward?: string | null; s?: number; label?: string; recheck_score?: number; components?: Record<string, number> };
type Predicted = { score: number; toward: string | null; s: number; route: string; components: Record<string, number> };
type Preset = { preset: string; status: string; reason: string | null; baseline: View | null; best: (View & { predicted_score?: number | null }) | null;
  candidates: View[]; predicted: Predicted | null };
type Localization = { ok: boolean; point_m: number[]; size_m: number; method: string; uncertainty_m: number; warning?: string;
  check?: { view_disagreement_m?: number; triangulated_height_m?: number } };
type Job = { id: string; kind: string; mode: string; status: string; phase: string | null; step: string; reason: string | null; moving_to: string | null;
  progress: Record<string, number | string>; result: Record<string, unknown> | null };
export type DirectorState = {
  gate: { mode: string; moves: boolean; reasons: string[] };
  camera: { queue: string | null; simulated: boolean; frame_id: number | null; age_s: number | null; live: boolean; size: [number, number] | null; gripper_camera: boolean };
  target: null | { id: string; label: string; kind: string; how: string; status: string; observation: Obs | null };
  sticker_reference: boolean;
  localization: Localization | null;
  arm: { joints: Record<string, number> | null; where: { where?: string; view?: string; to?: string; s?: number }; moving: boolean };
  job: Job | null; catalogue: Preset[]; events: { t: number; text: string }[];
};

const el = <T extends keyof HTMLElementTagNameMap>(tag: T, cls = "", text = "") => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text) n.textContent = text;
  return n;
};
const fmt = (v: number | null | undefined, d = 2) => (v == null ? "–" : v.toFixed(d));

async function call(path: string, body?: unknown): Promise<{ status: number; data: any }> {
  try {
    const r = await fetch(`/laptop/director/${path}`, body === undefined ? { cache: "no-store", signal: AbortSignal.timeout(4000) } : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(25000) });
    return { status: r.status, data: await r.json().catch(() => ({})) };
  } catch { return { status: 0, data: { error: "The arm server is unreachable." } }; }
}

// Plain-language line for the current state: what the director is doing or why it holds.
export function describe(s: DirectorState): string {
  const j = s.job;
  if (j && j.status === "RUNNING") {
    const p = j.progress;
    const pos = p.preset_count ? ` · position ${p.preset_index} of ${p.preset_count}` : "";
    const cand = p.candidate_count ? ` · candidate ${p.candidate_index} of ${p.candidate_count}` : "";
    const shot = p.shot_count ? ` · shot ${p.shot_index} of ${p.shot_count}` : "";
    const vis = j.phase === "watch" && p.visible_share != null ? ` · visible ${Math.round(Number(p.visible_share) * 100)}%` : "";
    return `${j.kind}: ${j.step}${pos}${cand}${shot}${vis}${j.moving_to ? ` · moving to ${j.moving_to}` : ""}`;
  }
  if (j && j.status !== "DONE") return `${j.kind} ${j.status.replace("_", " ")}${j.reason ? `: ${j.reason}` : ""}`;
  if (!s.camera.live) return "HOLD: the robot camera has no fresh frames";
  if (!s.target) return s.sticker_reference ? "No target. Auto-find the sticker or select an object." : "No target. Select an object (tick Save as Hack Lab sticker the first time).";
  if (s.target.status === "TARGET_LOST") return `TARGET LOST: ${s.target.label} is not in the picture`;
  return j ? `${j.kind} finished` : `Tracking ${s.target.label}`;
}

type RecordingResult = { ok: boolean; error?: string | null; message?: string | null };

export function directorPanel(card: HTMLElement, frame: HTMLElement, live: HTMLImageElement, startRecording: () => Promise<RecordingResult>,
  stopRecording: () => Promise<RecordingResult>, isRecording: () => boolean): void {
  frame.classList.add("director-frame");
  const still = el("img", "director-still");
  still.alt = "Frozen robot camera frame for drawing a box";
  still.hidden = true;
  const canvas = el("canvas", "director-overlay");
  canvas.setAttribute("aria-hidden", "true");
  frame.append(still, canvas);

  const panel = el("div", "director");
  const head = el("div", "director-head");
  const badge = el("span", "badge");
  const tbadge = el("span", "badge");
  const camLabel = el("span", "muted small");
  head.append(el("h3", "", "Camera director"), badge, tbadge, camLabel);
  const line = el("p", "director-line");
  line.setAttribute("role", "status");
  const sub = el("p", "muted small");
  const buttons = el("div", "controls");
  const b = (text: string, cls = "btn") => { const x = el("button", cls, text); buttons.append(x); return x; };
  const select = b("Select object", "btn primary"), autofind = b("Auto-find sticker"), keep = b("Keep in frame"),
    watch = b("Keep in view"), optimize = b("Optimize all views"), show = b("Director Show", "btn primary"),
    gimbal = b("Gimbal take"), scene = b("Scene"), back = b("Return"), snap = b("Snap to view"), selfcal = b("Self-calibrate"), cinema = b("Cinematic"), tour = b("Director Tour", "btn primary"), stop = b("STOP", "btn stop");
  const recLabel = el("label", "small");
  const rec = el("input");
  rec.type = "checkbox";
  recLabel.append(rec, " Record the shots");
  const claudeLabel = el("label", "small");
  const useClaude = el("input");
  useClaude.type = "checkbox";
  claudeLabel.append(useClaude, " Plan with Claude (planner only; the local director moves the arm)");
  buttons.append(recLabel, claudeLabel);
  const briefing = el("form", "controls");
  const briefLabel = el("label", "small", "Filming brief");
  const brief = el("input", "director-name");
  brief.size = 37;
  brief.maxLength = 500;
  brief.required = true;
  brief.placeholder = "Ease left and leave room on the right";
  briefLabel.append(brief);
  const film = el("button", "btn primary", "Film with Claude");
  film.type = "submit";
  briefing.append(briefLabel, film);
  const confirm = el("div", "controls director-confirm");
  confirm.hidden = true;
  const name = el("input", "director-name");
  name.placeholder = "What is it? (optional)";
  name.maxLength = 40;
  const stickerLabel = el("label", "small");
  const asSticker = el("input");
  asSticker.type = "checkbox";
  stickerLabel.append(asSticker, " Save as Hack Lab sticker");
  const ok = el("button", "btn primary", "Confirm box"), redo = el("button", "btn", "Reselect"), cancel = el("button", "btn", "Cancel");
  const hint = el("span", "muted small", "Drag a box around the object on the frozen frame.");
  confirm.append(hint, name, stickerLabel, ok, redo, cancel);
  const note = el("p", "small");
  note.setAttribute("role", "status");
  const locLine = el("p", "small muted");
  const gallery = el("div", "director-gallery");
  const story = el("div", "director-story");
  panel.append(head, line, sub, briefing, buttons, confirm, note, locLine, gallery, story);
  card.append(panel);

  let st: DirectorState | null = null, off = false;
  let picking: { frameId: number; box: Box | null } | null = null, drag: [number, number] | null = null;
  let recordingFor: string | null = null;   // job id whose shot phase this tab is recording
  let polling = false;

  const shown = () => (picking ? still : live);
  // The shown image's geometry, plus where its element box sits inside the frame (the canvas covers the frame).
  let shift: [number, number] = [0, 0];
  const display = (): Display | null => {
    const img = shown();
    if (img.hidden || !img.naturalWidth) return null;
    const r = img.getBoundingClientRect(), f = frame.getBoundingClientRect();
    shift = [r.left - f.left, r.top - f.top];
    return { width: r.width, height: r.height, natW: img.naturalWidth, natH: img.naturalHeight, fit: "contain" };
  };
  const offset = (_d: Display): [number, number] => shift;

  function draw() {
    const f = frame.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(f.width * dpr) || canvas.height !== Math.round(f.height * dpr)) {
      canvas.width = Math.round(f.width * dpr); canvas.height = Math.round(f.height * dpr);
    }
    const g = canvas.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, f.width, f.height);
    const d = display();
    if (!d) return;
    const [dx, dy] = offset(d);
    const rect = (box: Box, color: string, label: string, dash = false) => {
      const [l, t, w, h] = boxToView(d, box);
      g.setLineDash(dash ? [6, 4] : []);
      g.lineWidth = 2.5; g.strokeStyle = color; g.strokeRect(dx + l, dy + t, w, h);
      g.setLineDash([]);
      if (label) {
        g.font = "600 13px system-ui, sans-serif";
        const tw = g.measureText(label).width + 10;
        g.fillStyle = "rgba(8,13,20,.8)"; g.fillRect(dx + l, dy + t - 20, tw, 19);
        g.fillStyle = color; g.fillText(label, dx + l + 5, dy + t - 6);
      }
    };
    // desired framing guide: the centre the score aims for
    const [cx, cy] = toView(d, 0.5, 0.5);
    g.strokeStyle = "rgba(234,242,249,.35)"; g.lineWidth = 1;
    g.beginPath(); g.moveTo(dx + cx - 12, dy + cy); g.lineTo(dx + cx + 12, dy + cy); g.moveTo(dx + cx, dy + cy - 12); g.lineTo(dx + cx, dy + cy + 12); g.stroke();
    if (picking) {
      if (picking.box) rect(picking.box, "#79B9F2", "selection", true);
      return;
    }
    const o = st?.target?.observation;
    if (st?.target && o && st.target.status === "TRACKING") {
      rect(o.box, o.stable ? "#73DCB0" : "#F4BD6A", `${st.target.label} · ${Math.round(o.confidence * 100)}% · ${fmt(o.age_s, 1)} s`);
    }
  }

  function local(ev: PointerEvent): [number, number] | null {
    const d = display();
    if (!d) return null;
    const f = frame.getBoundingClientRect(), [dx, dy] = offset(d);
    return [ev.clientX - f.left - dx, ev.clientY - f.top - dy];
  }
  canvas.addEventListener("pointerdown", (ev) => {
    if (!picking) return;
    ev.preventDefault();          // otherwise the browser starts its own drag/selection and cancels the pointer
    drag = local(ev);
    canvas.setPointerCapture(ev.pointerId);
  });
  frame.addEventListener("dragstart", (ev) => ev.preventDefault());
  canvas.addEventListener("pointercancel", () => { drag = null; });
  canvas.addEventListener("pointermove", (ev) => {
    const d = display(), p = local(ev);
    if (!picking || !drag || !d || !p) return;
    picking.box = dragToBox(d, drag[0], drag[1], p[0], p[1], 4);
    draw();
  });
  canvas.addEventListener("pointerup", (ev) => {
    const d = display(), p = local(ev);
    if (picking && drag && d && p) picking.box = dragToBox(d, drag[0], drag[1], p[0], p[1]);
    drag = null;
    ok.disabled = !picking?.box;
    hint.textContent = picking?.box ? "Confirm the box, or drag again." : "Too small or outside the picture: drag again.";
    draw();
  });

  function endPicking() {
    picking = null; still.hidden = true; confirm.hidden = true; canvas.classList.remove("picking");
    draw();
  }
  async function startPicking() {
    const fid = st?.camera.frame_id;
    if (fid == null) { note.textContent = "No robot camera frame to draw on yet."; return; }
    picking = { frameId: fid, box: null };
    still.onload = () => draw();
    still.src = `/laptop/director/frame.jpg?id=${fid}`;
    still.hidden = false; confirm.hidden = false; ok.disabled = true; asSticker.checked = !st?.sticker_reference;
    hint.textContent = "Drag a box around the object on the frozen frame.";
    canvas.classList.add("picking");
  }
  select.onclick = () => void startPicking();
  redo.onclick = () => { if (picking) { picking.box = null; ok.disabled = true; draw(); } };
  cancel.onclick = endPicking;
  ok.onclick = async () => {
    if (!picking?.box) return;
    ok.disabled = true;
    const r = await call("select", { frame_id: picking.frameId, box: picking.box, label: name.value, as_sticker: asSticker.checked });
    note.textContent = r.status === 200 ? `Tracking ${r.data.target.label}.` : (r.data.error ?? "Selection failed.");
    if (r.status === 200) endPicking(); else ok.disabled = false;
    await poll();
  };
  autofind.onclick = async () => {
    autofind.disabled = true;
    const r = await call("autofind", {});
    note.textContent = r.status === 200 ? `Found the sticker (${r.data.observation.evidence}).` : (r.data.detail ?? r.data.error ?? "Not found.");
    if (r.data.error === "no_reference") void startPicking();
    autofind.disabled = false;
    await poll();
  };
  const job = (kind: string, extra: Record<string, unknown> = {}) => async () => {
    const r = await call("job", { kind, ...extra });
    note.textContent = r.status === 200 ? "" : (r.data.error ?? "Could not start.");
    await poll();
  };
  keep.onclick = job("center");
  briefing.onsubmit = event => {
    event.preventDefault();
    if (brief.value.trim() && !film.disabled) void job("film", { brief: brief.value.trim() })();
  };
  watch.onclick = job("watch");
  optimize.onclick = job("survey");
  cinema.onclick = () => job("cinematic", useClaude.checked ? { planner: "claude" } : {})();
  scene.onclick = () => job("scene", useClaude.checked ? { planner: "claude" } : {})();
  back.onclick = job("return");
  show.onclick = job("show");
  gimbal.onclick = job("gimbal");
  snap.onclick = job("snap");
  selfcal.onclick = job("selfcal");
  tour.onclick = job("tour");
  stop.onclick = async () => {
    try {
      const r = await fetch("/laptop/arm/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", signal: AbortSignal.timeout(7000) });
      const res = await r.json();
      note.textContent = r.ok && res.ok ? "STOPPED: arm holding." : "STOP sent, but the hold could not be confirmed. Check the arm.";
    } catch { note.textContent = "STOP could not be confirmed. Check the arm."; }
    await poll();
  };

  function viewCell(title: string, v: View | null | undefined) {
    const c = el("figure", "shot");
    if (v?.thumb) { const i = el("img"); i.src = `/laptop/director/thumb/${v.thumb}.jpg?${Date.now() >> 12}`; i.alt = `${title} view`; c.append(i); }
    c.append(el("figcaption", "small", `${title}: ${v ? (v.valid ? fmt(v.score) : "not seen") : "–"}`));
    return c;
  }
  function renderGallery(s: DirectorState) {
    const l = s.localization;
    locLine.textContent = l?.ok ? `Target localized at (${l.point_m.map((v) => (v * 100).toFixed(1)).join(", ")}) cm, ${(l.size_m * 100).toFixed(1)} cm wide ± ${(l.uncertainty_m * 100).toFixed(1)} cm · ${l.method}${l.check?.view_disagreement_m != null ? ` · views agree within ${(l.check.view_disagreement_m * 100).toFixed(1)} cm` : ""}${l.warning ? ` · ${l.warning}` : ""}` : "";
    gallery.replaceChildren(...s.catalogue.map((p) => {
      const c = el("article", `preset-card ${p.status.toLowerCase()}`);
      const h = el("div", "director-head");
      h.append(el("strong", "", p.preset), el("span", `badge ${p.status.toLowerCase()}`, p.status.replace("_", " ")));
      c.append(h);
      if (p.reason) c.append(el("p", "small muted", p.reason));
      const pos = (v: { toward?: string | null; s?: number } | null) => (v?.toward ? `${Math.round((v.s ?? 0) * 100)}% toward ${v.toward}` : "the preset itself");
      if (p.predicted) c.append(el("p", "small", `Predicted best: ${pos(p.predicted)} · ${fmt(p.predicted.score)}`));
      if (p.best || p.baseline) {
        const row = el("div", "shots");
        row.append(viewCell(p.best ? "verified" : "seen", p.best ?? p.baseline));
        p.candidates.forEach((x) => row.append(viewCell(x.label ?? "extra", x)));
        c.append(row);
      }
      if (p.best) {
        c.append(el("p", "small", `Verified: ${pos(p.best)} · score ${fmt(p.best.score)}${p.best.predicted_score != null ? ` (model ${fmt(p.best.predicted_score)})` : ""}`));
        if (p.best.measured_joints) c.append(el("p", "small muted mono", `measured ${Object.entries(p.best.measured_joints).map(([m, v]) => `${m}:${v}`).join(" ")}`));
        const recall = el("button", "btn", "Recall best");
        recall.onclick = job("recall", { preset: p.preset });
        recall.disabled = !s.gate.moves || s.job?.status === "RUNNING";
        c.append(recall);
      }
      return c;
    }));
    const board = (s.job?.result as any)?.storyboard as { preset: string; score: number; thumb: string }[] | undefined;
    const moves = (s.job?.result as any)?.moves as { name: string; done: boolean; complete?: boolean; in_frame?: number | null; why?: string | null }[] | undefined;
    story.replaceChildren();
    const showShots = (s.job?.result as any)?.shots as { shot: string; steady?: boolean; in_frame?: number | null; skipped?: string }[] | undefined;
    if (s.job?.kind === "show" && showShots?.length) {
      story.append(el("h4", "", "Director Show"));
      for (const x of showShots) story.append(el("p", "small", `${x.shot}${x.skipped ? ` · skipped: ${x.skipped}` : x.in_frame != null ? ` · object in frame ${Math.round(x.in_frame * 100)}%` : x.steady === false ? " · object left the picture" : ""}`));
    }
    if (s.job?.kind === "scene" || s.job?.kind === "film") {
      const reason = s.job.result?.plan_reason;
      if (typeof reason === "string" && reason) story.append(el("p", "small", reason));
      for (const m of moves ?? []) story.append(el("p", "small", `${m.name}: ${m.done ? `${m.complete ? "full move" : "shortened"} · object in frame ${m.in_frame == null ? "–" : Math.round(m.in_frame * 100) + "%"}` : "not possible"}${m.why ? ` · ${m.why}` : ""}`));
    }
    if (board?.length) {
      story.append(el("h4", "", s.job?.status === "BLOCKED" ? "Storyboard (still montage — no moving take: motion not approved)" : "Storyboard"));
      const row = el("div", "shots");
      board.forEach((x, i) => row.append(viewCell(`${i + 1}. ${x.preset}`, { valid: true, score: x.score, thumb: x.thumb })));
      story.append(row);
    }
  }

  async function refresh() {
    const r = await call("state");
    if (r.status === 404) {
      off = true;
      panel.classList.add("off");
      line.textContent = "Camera director is off. Start the arm server with --director to use it.";
      return;
    }
    if (r.status !== 200) { line.textContent = "HOLD: camera director unreachable."; return; }
    off = false;
    panel.classList.remove("off");
    const s = st = r.data as DirectorState;
    badge.textContent = s.gate.mode;
    badge.className = `badge ${s.gate.mode === "PHYSICAL" ? "live" : s.gate.mode === "SIMULATED" ? "sim" : "dry"}`;
    badge.title = s.gate.reasons.join("\n");
    const t = s.target, o = t?.observation;
    tbadge.textContent = !t ? "NO TARGET" : t.status === "TRACKING" ? `TRACKING ${Math.round((o?.confidence ?? 0) * 100)}%` : t.status.replace("_", " ");
    tbadge.className = `badge ${t?.status === "TRACKING" ? "live" : t ? "warn" : ""}`;
    camLabel.textContent = s.camera.simulated ? "camera: SIMULATED" :
      `camera: ${s.camera.queue ?? "none"}${s.camera.gripper_camera ? " (gripper camera)" : " (not the documented gripper camera)"} · frame age ${fmt(s.camera.age_s, 1)} s`;
    line.textContent = describe(s);
    const w = s.arm.where;
    sub.textContent = `${w?.where === "view" ? `at ${w.view}` : w?.where === "between" ? `between ${w.view} and ${w.to} (${Math.round((w.s ?? 0) * 100)}%)` : `pose: ${w?.where ?? "unknown"}`}` +
      (s.arm.joints ? ` · measured ${Object.entries(s.arm.joints).map(([m, v]) => `${m}:${v}`).join(" ")}` : "") +
      (s.gate.moves ? "" : ` · ${s.gate.reasons[0] ?? ""}`);
    const running = s.job?.status === "RUNNING";
    for (const x of [autofind, select, keep, watch, optimize, cinema, scene, back, tour, show, gimbal, snap, selfcal, film]) x.disabled = running || !!picking;
    film.disabled ||= !s.target || !s.gate.moves;
    autofind.hidden = !s.sticker_reference;
    renderGallery(s);
    // Record only deliberate shots: the cinematic phase and keep-in-view holds, never estimating or verifying.
    const shooting = rec.checked && running && s.gate.moves && (s.job?.phase === "cinematic" || s.job?.phase === "watch");
    if (recordingFor && (!shooting || s.job?.id !== recordingFor)) {
      const result = await stopRecording();
      if (result.ok || result.error === "NOT_RECORDING") recordingFor = null;
      else note.textContent = result.message ?? "Could not pause the recorder.";
    } else if (shooting && !recordingFor && !isRecording()) {
      const result = await startRecording();
      if (result.ok) recordingFor = s.job!.id;
      else note.textContent = result.message ?? "Could not start the recorder.";
    }
    draw();
  }
  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try { await refresh(); }
    finally { polling = false; }
  }
  new ResizeObserver(() => draw()).observe(frame);
  live.addEventListener("load", () => { if (!picking) draw(); });
  window.setInterval(() => { if (!off || Date.now() % 10000 < 600) void poll(); }, 600);
  void poll();
}
