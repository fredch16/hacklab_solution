"""Watch the arm move as a stick figure: side, top and front views from the geometry model (fk.py).

    python3 arm_view.py                       # shows the simulated arm of the server on :8891
    python3 arm_view.py --source http://localhost:8890    # the real arm's server
    then open http://localhost:8896

It reads the joints from a running capture_server.py and draws where fk.py says each joint
is. It never commands anything. The picture is the model's opinion, not a camera: joint
angles are approximate (see docs/MOTION.md), so use it to see the order and shape of a move,
not exact clearances.
"""

import argparse
import json
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fk
import motion

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arm View</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --line:#2c313c; --text:#e8eaef; --muted:#8a92a3; --arm:#4f8cff; --cam:#f5a524; --table:#3ecf8e; --ghost:#5b6474; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { padding:12px 16px; border-bottom:1px solid var(--line); display:flex; gap:8px 20px; flex-wrap:wrap; align-items:baseline; }
  h1 { font-size:16px; margin:0; }
  #status { color:var(--muted); }
  main { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; padding:16px; }
  figure { margin:0; background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  figcaption { padding:8px 12px; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
  canvas { display:block; width:100%; aspect-ratio:1/1; }
  p { margin:0 16px 16px; color:var(--muted); font-size:12px; max-width:70ch; }
</style></head><body>
<header><h1>Arm View</h1><span id="status">connecting…</span></header>
<main>
  <figure><figcaption>Side (forward →, up ↑)</figcaption><canvas id="side" width="600" height="600"></canvas></figure>
  <figure><figcaption>Top (looking down, forward ↑)</figcaption><canvas id="top" width="600" height="600"></canvas></figure>
  <figure><figcaption>Front (looking at the arm, up ↑)</figcaption><canvas id="front" width="600" height="600"></canvas></figure>
</main>
<p>Blue: the arm from base to jaw tip. Orange dot: estimated camera. Green line: the table. Grey dots: where the jaw tip sits in each saved view. This is the geometry model's picture of the joints, not a camera.</p>
<script>
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const R = 0.62;   // metres shown across each canvas
// each view: which model axes map to canvas right / up, and where the origin sits
const VIEWS = {
  side:  { h: (p) => p[1], v: (p) => p[2], ox: 0.25, oy: 0.88, table: true },
  top:   { h: (p) => p[0], v: (p) => p[1], ox: 0.5,  oy: 0.72, table: false },
  front: { h: (p) => p[0], v: (p) => p[2], ox: 0.5,  oy: 0.88, table: true },
};
function draw(id, data) {
  const c = document.getElementById(id), g = c.getContext("2d"), V = VIEWS[id], W = c.width, k = W / R;
  const X = (p) => W * V.ox + V.h(p) * k, Y = (p) => W * V.oy - V.v(p) * k;
  g.clearRect(0, 0, W, W);
  g.lineWidth = 1; g.strokeStyle = css("--line");
  for (let m = -0.6; m <= 0.6; m += 0.1) {           // 10 cm grid
    g.beginPath(); g.moveTo(W * V.ox + m * k, 0); g.lineTo(W * V.ox + m * k, W); g.stroke();
    g.beginPath(); g.moveTo(0, W * V.oy - m * k); g.lineTo(W, W * V.oy - m * k); g.stroke();
  }
  if (V.table) { g.strokeStyle = css("--table"); g.lineWidth = 3; g.beginPath(); g.moveTo(0, W * V.oy); g.lineTo(W, W * V.oy); g.stroke(); }
  g.fillStyle = css("--ghost"); g.font = "12px system-ui";
  for (const [name, p] of Object.entries(data.views || {})) {
    g.beginPath(); g.arc(X(p), Y(p), 4, 0, 7); g.fill(); g.fillText(name, X(p) + 7, Y(p) + 4);
  }
  if (!data.chain) return;
  g.strokeStyle = css("--arm"); g.lineWidth = 9; g.lineCap = "round"; g.lineJoin = "round";
  g.beginPath(); g.moveTo(X([0, 0, 0]), Y([0, 0, 0]));
  for (const p of data.chain) g.lineTo(X(p), Y(p));
  g.stroke();
  g.fillStyle = css("--text");
  for (const p of data.chain) { g.beginPath(); g.arc(X(p), Y(p), 4, 0, 7); g.fill(); }
  g.fillStyle = css("--cam"); g.beginPath(); g.arc(X(data.camera), Y(data.camera), 8, 0, 7); g.fill();
}
async function tick() {
  let data;
  try { data = await (await fetch("/pose")).json(); }
  catch (e) { document.getElementById("status").textContent = "arm_view.py is not answering"; return; }
  document.getElementById("status").textContent = data.status;
  for (const id of Object.keys(VIEWS)) draw(id, data);
}
tick(); setInterval(tick, 100);
</script></body></html>
"""


def read_pose(source: str) -> dict:
    """Joints from a capture_server.py, turned into points by the geometry model."""
    out = {"views": VIEW_TIPS, "chain": None, "camera": None}
    try:
        with urllib.request.urlopen(source + "/api/state", timeout=0.5) as r:
            state = json.load(r)
    except Exception as e:
        out["status"] = f"no answer from {source} ({e.__class__.__name__}); start capture_server.py --sim --port 8891"
        return out
    joints = {(8 if name == "gripper" else int(name[1:])): int(value)
              for name, value in re.findall(r"(j\d|gripper)=(\d+)", state.get("joints", ""))}
    if len(joints) != 8:
        out["status"] = "no joint data from the arm"
        return out
    points = fk.fk(joints)
    out["chain"] = [list(points[k]) for k in points if k != "camera"]
    out["camera"] = list(points["camera"])
    m = state.get("motion") or {}
    where = state.get("where") or {}
    place = {"view": f"at {where.get('view')}", "hub": "at the transit pose", "spoke": f"between {where.get('view')} and transit",
             "unknown": "at an unknown pose"}.get(where.get("where"), "")
    lowest = min(p[2] for p in out["chain"][1:] + [out["camera"]])
    arm = "simulated arm" if state.get("sim") else "real arm"
    doing = (f"moving to {m.get('view')}: {m.get('phase')} ({m.get('phase_index')}/{m.get('phase_count')})"
             if m.get("moving") else f"idle {place}".strip())
    out["status"] = f"{arm} · {doing} · lowest joint {lowest * 100:.0f} cm above the table (model)"
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/pose":
            body, kind = json.dumps(read_pose(self.server.source)).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, kind = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default="http://localhost:8891", help="a running capture_server.py")
    p.add_argument("--port", type=int, default=8896)
    args = p.parse_args()
    # Where the jaw tip sits in each saved view, as landmarks.
    VIEW_TIPS = {name: list(fk.fk(pose)["jaw_tip"]) for name, pose in motion.load_views().items()}
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.source = args.source.rstrip("/")
    print(f"showing the arm of {httpd.source} at http://localhost:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
