"""Dashboard-side command service: spoken requests from the Local Vision dashboard -> camera-arm MCP.

    .venv/bin/python voice_bridge.py                      # MCP at http://127.0.0.1:8200/mcp, listens on 127.0.0.1:8210
    .venv/bin/python voice_bridge.py --mcp-url http://127.0.0.1:8201/mcp --port 8211   # against a --sim stack

The paired browser dashboard forwards only eligible live finals that are not recording commands
(dashboard/docs/ARCHITECTURE.md, "Dashboard command routing and MCP handoff"):

    POST /api/v1/voice-command  {"session_id": "...", "utterance_id": "...", "text": "go to top"}
    -> {"ok": true, "command": "arm.go_to_view", "message": "Moving to top", "error": null}

This service owns interpretation, the MCP connection and the tool policy; the phone and the static
browser bundle never see MCP. It understands a small local grammar, no LLM:

  stop / halt / wait / freeze / abort / hold on  -> stop()          (checked first, always wins)
  go to / move to / show / look at ... <view>    -> go_to_view()    (only views checked on the real arm)
  where are you / status                         -> get_status()
  list views / what views / which views          -> list_views()

Owner decision (21 Nov): a spoken "go to <view>" moves at once, without a confirmation click.
Anything mentioning "record" is never acted on here: recording requests are the browser's own
grammar, and one that reaches us failed the phone's acoustic check. Each (session, utterance) runs
at most once; a repeat gets the first result back and nothing is replayed.

Robot motion stays behind mcp_server.py -> capture_server.py and their motion checks.
Only the standard library besides the MCP SDK (already a project dependency).
"""
import argparse
import asyncio
import ipaddress
import json
import logging
import re
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from mcp import Client

log = logging.getLogger("voice_bridge")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")          # utterance IDs (as the dashboard checks them)
SID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")      # phone session IDs are opaque
MAX_TEXT = 500

FILLER_HEAD = re.compile(r"^(?:(?:hey|okay|ok|please|can you|could you|would you|now|and|then|robot|camera|arm) )+")
FILLER_TAIL = re.compile(r"(?: (?:please|right now|now|thank you|thanks|okay|i think))+$")
STOP_RE = re.compile(r"\b(?:stop|halt|wait|freeze|abort|hold on|emergency)\b")
STATUS_RE = re.compile(r"^(?:where are you|where is the (?:camera|arm)|what(?:'s| is) (?:the |your )?(?:status|view|position)"
                       r"|status|what are you (?:doing|looking at)|which view are you (?:in|at|on))$")
LIST_RE = re.compile(r"^(?:list (?:the |all )?views|(?:what|which) views(?: (?:are there|do you have|can you (?:go to|show)))?"
                     r"|show (?:me )?(?:the |all )?views)$")
GOTO_RE = re.compile(r"^(?:go|move|turn|switch|look|point|show me|show|take me|change|pan|swing)"
                     r"(?: (?:over|back|the camera|the arm|camera|arm))?"
                     r"(?: (?:to|at|towards|toward|into|from))?(?: (?:the|a|your))? (.+?)(?: (?:view|position|shot|pose))?$")
WORD = re.compile(r"[a-z0-9']+")


def normalize(text: str) -> str:
    t = re.sub(r"[^a-z0-9' ]", " ", text.lower().replace("_", " "))
    t = re.sub(r"\s+", " ", t).strip()
    t = FILLER_HEAD.sub("", t)
    return FILLER_TAIL.sub("", t).strip()


def view_names(view: dict) -> list[str]:
    """Spoken forms of a view: its name ('side_low' -> 'side low') and aliases."""
    return [normalize(n) for n in [view["name"], *view.get("aliases", [])] if normalize(n)]


def match_view(phrase: str, views: list[dict]) -> dict | None:
    phrase = re.sub(r"^(?:the|a|your) ", "", phrase)
    phrase = re.sub(r" (?:view|position|shot|pose)$", "", phrase)
    for v in views:
        if phrase in view_names(v):
            return v
    return None


def plan(text: str, views: list[dict]) -> dict:
    """Interpret one transcript. Returns {"tool": ..., "args": {...}} or {"tool": None, "reason": ...}."""
    t = normalize(text)
    if not t:
        return {"tool": None, "reason": "empty"}
    if "record" in t:
        return {"tool": None, "reason": "recording"}
    if STOP_RE.search(t):
        return {"tool": "stop", "args": {}}
    if STATUS_RE.match(t):
        return {"tool": "get_status", "args": {}}
    if LIST_RE.match(t):
        return {"tool": "list_views", "args": {}}
    m = GOTO_RE.match(t)
    v = match_view(m.group(1), views) if m else match_view(t, views)
    if v is None and m:
        return {"tool": None, "reason": "unknown_view", "view": m.group(1)}
    if v is None:
        return {"tool": None, "reason": "no_match"}
    if not v.get("checked"):
        return {"tool": None, "reason": "unchecked_view", "view": v["name"]}
    return {"tool": "go_to_view", "args": {"view": v["name"]}}


COMMAND = {"stop": "arm.stop", "go_to_view": "arm.go_to_view", "get_status": "arm.status", "list_views": "arm.list_views"}


def _data(res) -> dict:
    if res.is_error:
        text = res.content[0].text if res.content else "tool error"
        try:
            err = json.loads(text)
            return {"_error": err.get("error") or "TOOL_ERROR", "_detail": err.get("detail") or text}
        except (ValueError, AttributeError):
            return {"_error": "TOOL_ERROR", "_detail": text}
    return res.structured_content or {}


def _status_line(st: dict) -> str:
    if st.get("moving"):
        pct = st.get("progress")
        return f"Moving to {st.get('target_view')}" + (f" ({round(pct * 100)}%)" if isinstance(pct, (int, float)) else "")
    where = f"At {st['current_view']}" if st.get("current_view") else "Between views"
    last = st.get("last_result") or {}
    if last and not last.get("ok") and last.get("error"):
        where += f"; last move failed ({last['error']})"
    if st.get("blockers"):
        where += "; can't move: " + ", ".join(map(str, st["blockers"]))
    return where


class Bridge:
    def __init__(self, target, max_seen: int = 500):
        """target: the MCP server's Streamable HTTP URL, or (tests) an MCPServer to call in-process."""
        self.target = target
        self.seen: OrderedDict[tuple[str, str], dict | None] = OrderedDict()
        self.max_seen = max_seen
        self.lock = threading.Lock()

    async def _run(self, text: str) -> dict:
        async with Client(self.target, read_timeout_seconds=10) as client:
            t = normalize(text)
            # stop needs no view list: skip that round trip so it reaches the arm sooner
            views = [] if STOP_RE.search(t) and "record" not in t else \
                _data(await client.call_tool("list_views", {})).get("views", [])
            p = plan(text, views)
            log.info("plan %r -> %s", text, p)
            if p["tool"] is None:
                return self._refusal(p, views)
            res = _data(await client.call_tool(p["tool"], p["args"]))
            command = COMMAND[p["tool"]]
            if "_error" in res:
                return {"ok": False, "command": command, "error": str(res["_error"]).upper(), "message": res["_detail"]}
            if p["tool"] == "stop":
                msg = "Stopped the arm" if res.get("was_moving") else "Arm holding (it wasn't moving)"
                return {"ok": bool(res.get("ok", True)), "command": command, "error": res.get("error"), "message": msg}
            if p["tool"] == "go_to_view":
                v = p["args"]["view"]
                msg = f"Moving to {v}" if res.get("started") else (res.get("detail") or f"Did not start the move to {v}")
                return {"ok": True, "command": command, "error": None, "message": msg}
            if p["tool"] == "get_status":
                return {"ok": True, "command": command, "error": None, "message": _status_line(res)}
            names = [v["name"] for v in res.get("views", []) if v.get("checked")]
            return {"ok": True, "command": command, "error": None,
                    "message": "Views: " + (", ".join(names) if names else "none checked yet")}

    @staticmethod
    def _refusal(p: dict, views: list[dict]) -> dict:
        reason = p["reason"]
        if reason == "unknown_view":
            names = ", ".join(v["name"] for v in views if v.get("checked"))
            return {"ok": False, "command": "arm.go_to_view", "error": "UNKNOWN_VIEW",
                    "message": f"No view called “{p['view']}”. Try: {names}"}
        if reason == "unchecked_view":
            return {"ok": False, "command": "arm.go_to_view", "error": "VIEW_NOT_CHECKED",
                    "message": f"{p['view']} isn't checked on the real arm yet; use the /control page"}
        return {"ok": True, "command": "none", "error": None, "message": None}

    def handle(self, session_id: str, utterance_id: str, text: str) -> dict:
        key = (session_id, utterance_id)
        with self.lock:
            if key in self.seen:
                prev = self.seen[key]
                return prev if prev is not None else {"ok": False, "command": "none", "error": "DUPLICATE",
                                                      "message": "Already handling this request"}
            self.seen[key] = None
            while len(self.seen) > self.max_seen:
                self.seen.popitem(last=False)
        try:
            out = asyncio.run(self._run(text))
        except Exception as e:  # MCP server down, timeout, protocol error
            log.warning("MCP call failed: %r", e)
            out = {"ok": False, "command": "none", "error": "ARM_UNREACHABLE",
                   "message": "The camera-arm MCP server is not reachable"}
        with self.lock:
            self.seen[key] = out
        return out


def origin_allowed(origin: str | None, extra: set[str]) -> bool:
    """Browsers always send Origin on this cross-origin POST. Allow the phone's LAN HTTPS origin
    (private or loopback address), localhost dev fixtures and --allow-origin entries. Requests without
    Origin come from local non-browser processes (curl, tests), which can reach the arm directly anyway."""
    if origin is None:
        return True
    if origin in extra:
        return True
    u = urlsplit(origin)
    if u.scheme != "https" or not u.hostname:
        return False
    if u.hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(u.hostname)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def make_handler(bridge: Bridge, extra_origins: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "voice_bridge/1"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and origin_allowed(origin, extra_origins):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "600")

        def _json(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not origin_allowed(self.headers.get("Origin"), extra_origins):
                return self._json(403, {"ok": False, "error": "ORIGIN_NOT_ALLOWED"})
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path == "/api/v1/health":
                return self._json(200, {"ok": True, "mcp_url": str(bridge.target)})
            self._json(404, {"ok": False, "error": "NOT_FOUND"})

        def do_POST(self):
            if not origin_allowed(self.headers.get("Origin"), extra_origins):
                return self._json(403, {"ok": False, "error": "ORIGIN_NOT_ALLOWED"})
            if self.path != "/api/v1/voice-command":
                return self._json(404, {"ok": False, "error": "NOT_FOUND"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"ok": False, "error": "JSON_REQUIRED"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 4096:
                    raise ValueError
                req = json.loads(self.rfile.read(n))
                sid, uid, text = req["session_id"], req["utterance_id"], req["text"]
                if not (isinstance(sid, str) and SID_RE.match(sid) and isinstance(uid, str) and ID_RE.match(uid)
                        and isinstance(text, str) and len(text) <= MAX_TEXT):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"ok": False, "error": "BAD_REQUEST"})
            out = bridge.handle(sid, uid, text)
            log.info("%s/%s %r -> %s", sid, uid, text, out)
            self._json(200, {"utterance_id": uid, **out})

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mcp-url", default="http://127.0.0.1:8200/mcp")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8210)
    p.add_argument("--allow-origin", action="append", default=[],
                   help="extra browser origin allowed to call (the phone's LAN https origin is allowed by default)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bridge = Bridge(args.mcp_url)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(bridge, set(args.allow_origin)))
    log.info("voice bridge on http://%s:%d -> %s", args.host, args.port, args.mcp_url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
