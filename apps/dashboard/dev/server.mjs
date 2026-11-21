// DEV ONLY: fake contract server for visual checks. Plain http/ws on localhost. Never shipped.
// The dashboard hardcodes wss://, so WS will fail here; use ?mock only in a real HTTPS setup. Serves files + fake REST.
import http from "node:http"; import { readFileSync } from "node:fs";
const dir = new URL("../../mobile/assets/dashboard/", import.meta.url);
const types = { html: "text/html", js: "text/javascript", css: "text/css" };
let paired = false;
http.createServer((q, r) => {
  const j = (c, o) => { r.writeHead(c, { "content-type": "application/json" }); r.end(JSON.stringify(o)); };
  if (q.url === "/api/v1/session") return j(200, { paired });
  if (q.url === "/api/v1/pair") { paired = true; return j(200, { paired }); }
  if (q.url === "/api/v1/unpair") { paired = false; return j(200, { paired }); }
  const f = q.url === "/" ? "index.html" : q.url.slice(1);
  try { r.writeHead(200, { "content-type": types[f.split(".").pop()] ?? "text/plain" }); r.end(readFileSync(new URL(f, dir))); } catch { j(404, { error: "NOT_FOUND" }); }
}).listen(8080, () => console.log("dev-only mock on http://localhost:8080"));
