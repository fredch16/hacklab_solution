// DEV ONLY (never shipped): fake phone for visual checks. HTTPS+WSS on localhost with a throwaway cert.
//   node dev/server.mjs <key.pem> <cert.pem> [port]
// Push events:  GET /dev/emit?type=voice.wake_detected&payload={"utterance_id":"u1","phrase":"hey robot"}
import https from "node:https"; import crypto from "node:crypto"; import { readFileSync } from "node:fs";
const [key, cert, port = "8443"] = process.argv.slice(2);
const dir = new URL("../../mobile/assets/dashboard/", import.meta.url);
const types = { html: "text/html", js: "text/javascript", css: "text/css" };
const socks = new Set(); let seq = 0;
const snapshot = (over = {}) => ({
  app: "active", session_started_utc: new Date().toISOString(), keep_screen_on: true,
  device: { model: "Pixel 10 Pro XL", manufacturer: "Google", os: "Android 17", sdk_int: 37, app_version: "0.1.0+1" },
  permissions: { camera: "granted", microphone: "granted" }, camera: { state: "not_started", error: null, lens: "back" },
  stream: { state: "idle", error: null }, recording: { state: "idle" },
  voice: { state: "listening", error: null, wake_phrase: "hey robot", language: "en", model: null, transcripts: [] },
  battery: { level_percent: 86, charging: false, plugged: false }, thermal: { status: "none" },
  storage: { free_bytes: 92e9, total_bytes: 256e9 }, recordings: [],
  network: { addresses: ["127.0.0.1"], port: Number(port), tls: "self_signed", cert_sha256: "AB:CD" },
  viewer: { paired: true, connected: true, paired_since_utc: new Date().toISOString(), expires_utc: new Date(Date.now() + 4e7).toISOString() }, ...over,
});
const frame = (s) => { const b = Buffer.from(s); const h = b.length < 126 ? Buffer.from([0x81, b.length]) : Buffer.from([0x81, 126, b.length >> 8, b.length & 255]); return Buffer.concat([h, b]); };
const send = (type, payload) => { const m = frame(JSON.stringify({ version: 1, type, session_id: "dev", sequence: ++seq, timestamp_utc: new Date().toISOString(), payload })); for (const s of socks) s.write(m); };
const srv = https.createServer({ key: readFileSync(key), cert: readFileSync(cert) }, (q, r) => {
  const u = new URL(q.url, "https://x");
  const j = (c, o) => { r.writeHead(c, { "content-type": "application/json" }); r.end(JSON.stringify(o)); };
  if (u.pathname === "/api/v1/session") return j(200, { paired: true });
  if (u.pathname === "/dev/emit") { const t = u.searchParams.get("type"); const p = JSON.parse(u.searchParams.get("payload") ?? "{}"); t === "session.snapshot" ? send(t, snapshot(p)) : send(t, p); return j(200, { ok: true }); }
  const f = u.pathname === "/" ? "index.html" : u.pathname.slice(1);
  if (!/^[a-zA-Z0-9_-]+\.(html|css|js)$/.test(f)) return j(404, { error: "NOT_FOUND" });
  let body; try { body = readFileSync(new URL(f, dir)); } catch { return j(404, { error: "NOT_FOUND" }); }
  r.writeHead(200, { "content-type": types[f.split(".").pop()] ?? "text/plain", "cache-control": "no-store" }); r.end(body);
});
srv.on("upgrade", (q, sock) => {
  sock.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${crypto.createHash("sha1").update(q.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64")}\r\n\r\n`);
  socks.add(sock); sock.on("close", () => socks.delete(sock)); sock.on("error", () => {}); sock.on("data", () => {});
  sock.write(frame(JSON.stringify({ version: 1, type: "session.snapshot", session_id: "dev", sequence: ++seq, timestamp_utc: new Date().toISOString(), payload: snapshot() })));
});
srv.listen(Number(port), "127.0.0.1", () => console.log(`dev-only mock phone on https://localhost:${port}`));
