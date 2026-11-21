"""Loopback dashboard. Phone signaling is pinned HTTPS/WSS; WebRTC media stays peer-to-peer."""
import argparse
import asyncio
import ipaddress
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, Fingerprint, MultipartReader, WSMsgType, web

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "dashboard/apps/dashboard/dist"
LOG = logging.getLogger("laptop_dashboard")
TAB_ID = re.compile(r"[a-f0-9]{32}")
CLIP = re.compile(r"robot-\d{8}T\d{6}-[a-f0-9]{8}\.mkv")
PHONE_ROUTES = {
    "/api/v1/session": "GET", "/api/v1/pair": "POST", "/api/v1/pair/request": "POST",
    "/api/v1/pair/request/status": "GET", "/api/v1/unpair": "POST", "/api/v1/snapshot": "GET",
}
PHONE_COOKIES = {"lvc_session", "lvc_pending"}


@dataclass(frozen=True)
class PhoneTarget:
    origin: str
    fingerprint: Fingerprint

    @classmethod
    def parse(cls, address: str, fingerprint: str):
        url = urlsplit(address.strip())
        if (url.scheme != "https" or url.username is not None or url.password is not None
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("Enter the phone's HTTPS address from its Link screen.")
        try:
            ip = ipaddress.IPv4Address(url.hostname)
            port = url.port or 443
        except (ValueError, TypeError):
            raise ValueError("Enter the phone's local IPv4 address and port.") from None
        if not ip.is_private or ip.is_unspecified or ip.is_link_local:
            raise ValueError("Use the phone's private LAN address.")
        hex_pin = re.sub(r"[:\s]", "", fingerprint)
        if not re.fullmatch(r"[a-fA-F0-9]{64}", hex_pin):
            raise ValueError("Copy the complete SHA-256 fingerprint from the phone's Link screen.")
        return cls(f"https://{ip}:{port}", Fingerprint(bytes.fromhex(hex_pin)))


@dataclass
class RecordingLease:
    last_view: float = float("-inf")
    last_frame: float = float("-inf")

    def viewed(self, now):
        self.last_view = now

    def frame(self, now):
        self.last_frame = now

    def live(self, now):
        return now - self.last_view <= 3 and now - self.last_frame <= 2


class Dashboard:
    def __init__(self, clips: Path, arm: str, bridge: str, port: int):
        self.clips, self.arm, self.bridge = clips, arm, bridge
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.phone = None
        self.phone_cookies = {}
        self.phone_ws = None
        self.phone_tab = None
        self.phone_session = None
        self.phone_connecting = False
        self.phone_pending = {}
        self.robot_tabs = {}
        self.robot_owner = None
        self.robot_process = None
        self.robot_name = None
        self.robot_started = None
        self.robot_last = max((p.name for p in clips.glob("*.mkv") if CLIP.fullmatch(p.name)), default=None)
        self.robot_error = None
        self.robot_lock = asyncio.Lock()

    async def lifecycle(self, app):
        async with ClientSession(cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=12)) as client:
            self.http = client
            yield
            await self.end_robot()

    @web.middleware
    async def protect(self, request, handler):
        origin = f"http://{request.host}"
        supplied = request.headers.get("Origin")
        if origin not in self.origins or (supplied is not None and supplied != origin):
            raise web.HTTPForbidden()
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden()
        if request.method == "POST" and request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        return await handler(request)

    async def assets(self, request):
        name = request.match_info.get("name") or "index.html"
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.(?:html|css|js)", name) or not (ASSETS / name).is_file():
            raise web.HTTPNotFound()
        if name == "index.html":
            body = (ASSETS / name).read_text().replace('<html lang="en">', '<html lang="en" data-host="laptop">')
            return web.Response(text=body, content_type="text/html", headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; "
                                           "connect-src 'self' http://127.0.0.1:8210; "
                                           "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
            })
        return web.FileResponse(ASSETS / name, headers={"Cache-Control": "no-store"})

    async def phone_target(self, request):
        if request.method == "GET":
            return web.json_response({"address": self.phone.origin if self.phone else ""})
        try:
            config = await request.json()
            target = PhoneTarget.parse(config["address"], config["fingerprint"])
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            return web.json_response({"error": str(error) if isinstance(error, ValueError) else "Invalid phone setup."}, status=400)
        if self.phone_ws is not None or self.phone_connecting:
            return web.json_response({"error": "Unpair the connected phone first."}, status=409)
        try:
            async with self.http.get(target.origin + "/api/v1/session", ssl=target.fingerprint,
                                     allow_redirects=False, timeout=ClientTimeout(total=4)) as response:
                if response.status != 200:
                    raise ValueError("The phone did not accept the connection.")
        except (ClientError, TimeoutError, ValueError):
            return web.json_response({"error": "Couldn’t reach the phone with that fingerprint. Check its Link screen and Wi-Fi."}, status=502)
        if self.phone_ws is not None or self.phone_connecting:
            return web.json_response({"error": "Unpair the connected phone first."}, status=409)
        self.phone, self.phone_cookies = target, {}
        return web.json_response({"ok": True, "address": target.origin})

    async def phone_http(self, request):
        if PHONE_ROUTES.get(request.path) != request.method:
            raise web.HTTPNotFound()
        target = self.phone
        if target is None:
            return web.json_response({"paired": False} if request.path.endswith("/session") else {"error": "PHONE_NOT_SET"},
                                     status=200 if request.path.endswith("/session") else 409)
        try:
            async with self.http.request(request.method, target.origin + request.path, ssl=target.fingerprint,
                                         cookies=self.phone_cookies, headers={"Origin": target.origin,
                                         "Content-Type": "application/json"}, data=await request.read(),
                                         allow_redirects=False) as response:
                result = await response.read()
                if self.phone is target:
                    for name, cookie in response.cookies.items():
                        if name in PHONE_COOKIES:
                            if cookie.value:
                                self.phone_cookies[name] = cookie.value
                            else:
                                self.phone_cookies.pop(name, None)
                    if request.path == "/api/v1/unpair" and response.status == 200:
                        self.phone_cookies.clear()
                    elif request.path == "/api/v1/session" and response.status == 200:
                        if isinstance((status := json.loads(result)), dict) and status.get("paired") is False:
                            self.phone_cookies.pop("lvc_session", None)
                return web.Response(status=response.status, body=result, content_type="application/json")
        except (ClientError, TimeoutError):
            return web.json_response({"error": "PHONE_UNREACHABLE"}, status=502)

    async def phone_events(self, request):
        tab = request.query.get("tab")
        if tab is not None and not TAB_ID.fullmatch(tab):
            raise web.HTTPBadRequest()
        target = self.phone
        if not target or not self.phone_cookies.get("lvc_session"):
            raise web.HTTPUnauthorized()
        if self.phone_ws is not None or self.phone_connecting:
            raise web.HTTPConflict()
        self.phone_connecting = True
        try:
            async with self.http.ws_connect(target.origin + "/api/v1/events", ssl=target.fingerprint,
                                             origin=target.origin, headers={
                                                 "Cookie": SimpleCookie(self.phone_cookies).output(header="", sep=";").strip(),
                                             }) as upstream:
                browser = web.WebSocketResponse(heartbeat=10, max_msg_size=100_000)
                await browser.prepare(request)
                self.phone_ws, self.phone_connecting, self.phone_tab = upstream, False, tab

                async def from_phone():
                    async for message in upstream:
                        if message.type != WSMsgType.TEXT:
                            continue
                        env = json.loads(message.data)
                        if env.get("type") == "session.snapshot":
                            self.phone_session = env.get("session_id")
                        pending = self.phone_pending.get(env.get("request_id"))
                        if env.get("type") == "command.result" and pending and not pending.done():
                            pending.set_result(env.get("payload") or {})
                        await browser.send_str(message.data)

                async def from_browser():
                    async for message in browser:
                        if message.type == WSMsgType.TEXT:
                            await upstream.send_str(message.data)

                await until_first(from_phone(), from_browser())
                await browser.close(code=upstream.close_code if upstream.close_code in (4001, 4002, 4003, 4004) else 1000)
                return browser
        except (ClientError, TimeoutError):
            raise web.HTTPBadGateway() from None
        finally:
            self.phone_ws, self.phone_connecting, self.phone_tab, self.phone_session = None, False, None, None
            for pending in self.phone_pending.values():
                if not pending.done():
                    pending.set_result({"ok": False, "error": "PHONE_DISCONNECTED"})

    async def phone_record(self, action, tab, session):
        upstream = self.phone_ws
        if upstream is None or upstream.closed:
            return outcome(False, "Open the paired phone dashboard first.", "PHONE_DISCONNECTED")
        if (tab is not None and tab != self.phone_tab) or (session is not None and session != self.phone_session):
            return outcome(False, "The phone connection changed before the command finished.", "PHONE_DISCONNECTED")
        key = "laptop:" + uuid.uuid4().hex
        pending = self.phone_pending[key] = asyncio.get_running_loop().create_future()
        try:
            await upstream.send_json({"version": 1, "type": f"recording.{action}", "request_id": key, "payload": {}})
            response = await asyncio.wait_for(pending, 8)
            message = response.get("message")
            if response.get("ok") is True:
                message = message or ("Phone recording started." if action == "start" else "Phone recording stopped.")
            return outcome(response.get("ok") is True, message, response.get("error"))
        except TimeoutError:
            return outcome(False, "The phone did not reply.", "COMMAND_TIMEOUT")
        except (ClientError, ConnectionError):
            return outcome(False, "The phone disconnected.", "PHONE_DISCONNECTED")
        finally:
            self.phone_pending.pop(key, None)

    async def robot_events(self, request):
        tab = request.match_info["tab"]
        if not TAB_ID.fullmatch(tab) or tab in self.robot_tabs:
            raise web.HTTPBadRequest()
        lease = self.robot_tabs[tab] = RecordingLease()
        browser = web.WebSocketResponse(heartbeat=10, max_msg_size=1024)
        try:
            await browser.prepare(request)

            async def from_browser():
                async for message in browser:
                    if message.type == WSMsgType.TEXT and message.data == "viewing":
                        lease.viewed(time.monotonic())

            async def frames():
                async with self.http.get(self.arm + "/stream", allow_redirects=False,
                                         timeout=ClientTimeout(total=None, connect=2, sock_read=2)) as source:
                    if source.status != 200 or not source.content_type.startswith("multipart/"):
                        return
                    parts = MultipartReader.from_response(source)
                    while (part := await parts.next()) is not None:
                        jpeg = await asyncio.wait_for(part.read(), 2)
                        if len(jpeg) > 8_000_000 or not jpeg.startswith(b"\xff\xd8"):
                            return
                        lease.frame(time.monotonic())
                        await browser.send_bytes(jpeg)
                        if self.robot_owner == tab and self.robot_process and lease.live(time.monotonic()):
                            self.robot_process.stdin.write(jpeg)
                            await asyncio.wait_for(self.robot_process.stdin.drain(), 1)

            async def watchdog():
                while True:
                    await asyncio.sleep(0.4)
                    if self.robot_owner == tab and not lease.live(time.monotonic()):
                        return

            await until_first(from_browser(), frames(), watchdog())
        finally:
            self.robot_tabs.pop(tab, None)
            if self.robot_owner == tab:
                await self.end_robot(tab)
            await browser.close()
        return browser

    async def start_robot(self, tab):
        async with self.robot_lock:
            lease = self.robot_tabs.get(tab)
            if lease is None or not lease.live(time.monotonic()):
                return outcome(False, "Keep the robot camera visible and live before recording.", "ROBOT_FEED_NOT_VIEWED")
            if self.robot_process is not None:
                return outcome(False, "The robot camera is already recording.", "ALREADY_RECORDING")
            self.clips.mkdir(parents=True, exist_ok=True)
            name = datetime.now(timezone.utc).strftime("robot-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8] + ".mkv"
            try:
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-n",
                    "-use_wallclock_as_timestamps", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-i", "pipe:0",
                    "-an", "-c:v", "copy", "-f", "matroska", str(self.clips / (name + ".partial")),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except OSError:
                return outcome(False, "FFmpeg is required to record the robot camera.", "FFMPEG_UNAVAILABLE")
            self.robot_process, self.robot_owner, self.robot_name = process, tab, name
            self.robot_started, self.robot_error = time.monotonic(), None
            if self.robot_tabs.get(tab) is not lease or not lease.live(time.monotonic()):
                asyncio.create_task(self.end_robot(tab))
                return outcome(False, "The robot camera is no longer being viewed.", "ROBOT_FEED_NOT_VIEWED")
            return outcome(True, "Robot camera recording started.")

    async def end_robot(self, tab=None):
        async with self.robot_lock:
            process = self.robot_process
            if process is None or (tab is not None and self.robot_owner != tab):
                return outcome(False, "This tab is not recording the robot camera.", "NOT_RECORDING")
            name = self.robot_name
            self.robot_process, self.robot_owner, self.robot_started = None, None, None
            process.stdin.close()
            partial = self.clips / (name + ".partial")
            try:
                code = await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
                code = 1
            if code == 0 and partial.is_file() and partial.stat().st_size:
                partial.rename(self.clips / name)
                self.robot_last = name
                return outcome(True, "Robot camera recording saved.")
            partial.unlink(missing_ok=True)
            self.robot_error = "The robot recording could not be saved."
            return outcome(False, self.robot_error, "RECORDING_FAILED")

    async def recording(self, request):
        try:
            body = await request.json()
            camera, action = body["camera"], body["action"]
            tab, session = body.get("tab"), body.get("session")
            if session is not None and (not isinstance(session, str) or len(session) > 200):
                raise ValueError
            if camera not in ("phone", "robot") or action not in ("start", "stop"):
                raise ValueError
            if tab is not None and (not isinstance(tab, str) or not TAB_ID.fullmatch(tab)):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise web.HTTPBadRequest() from None
        if camera == "phone":
            result = await self.phone_record(action, tab, session)
        else:
            if tab is None and request.headers.get("Origin") is None:
                # Local MCP calls may use the sole visible feed, never an arbitrary tab.
                tabs = [key for key, lease in self.robot_tabs.items() if lease.live(time.monotonic())]
                tab = self.robot_owner if action == "stop" else tabs[0] if len(tabs) == 1 else None
            result = await (self.start_robot(tab) if action == "start"
                            else self.end_robot(tab) if tab is not None
                            else asyncio.sleep(0, result=outcome(False, "No active robot recording.", "NOT_RECORDING")))
        return web.json_response(result)

    async def robot_state(self, request):
        tab = request.match_info["tab"]
        return web.json_response({"recording": self.robot_owner == tab, "busy": self.robot_owner is not None,
                                  "seconds": int(time.monotonic() - self.robot_started) if self.robot_started is not None else 0,
                                  "clip": self.robot_last, "error": self.robot_error})

    async def download(self, request):
        name = request.match_info["name"]
        if not CLIP.fullmatch(name) or not (self.clips / name).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(self.clips / name, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def arm_stop(self, request):
        return await self.local_post(self.arm + "/api/stop", request)

    async def arm_voice(self, request):
        return await self.local_post(self.bridge + "/api/v1/voice-command", request)

    async def claude_settings(self, request):
        raise web.HTTPFound(self.bridge + "/#claude-settings")

    async def local_post(self, url, request):
        try:
            async with self.http.post(url, json=await request.json(), allow_redirects=False) as response:
                return web.Response(status=response.status, body=await response.read(), content_type="application/json")
        except (ClientError, TimeoutError):
            return web.json_response({"ok": False, "error": "ARM_UNREACHABLE"}, status=502)


def outcome(ok, message=None, error=None):
    return {"ok": ok, "message": message, "error": error}


async def until_first(*coroutines):
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception) and not isinstance(result, (ClientError, ConnectionError, TimeoutError)):
                LOG.debug("Relay stopped: %s", type(result).__name__)


def create_app(dashboard):
    app = web.Application(middlewares=[dashboard.protect], client_max_size=16_384)
    app.cleanup_ctx.append(dashboard.lifecycle)
    app.add_routes([
        web.get("/laptop/phone", dashboard.phone_target), web.post("/laptop/phone", dashboard.phone_target),
        web.get("/api/v1/events", dashboard.phone_events), web.route("*", "/api/v1/{path:.*}", dashboard.phone_http),
        web.get("/laptop/robot/events/{tab}", dashboard.robot_events),
        web.get("/laptop/robot/state/{tab}", dashboard.robot_state),
        web.get("/laptop/clips/{name}", dashboard.download), web.post("/laptop/recording", dashboard.recording),
        web.post("/laptop/arm/stop", dashboard.arm_stop), web.post("/laptop/arm/voice", dashboard.arm_voice),
        web.get("/laptop/claude-settings", dashboard.claude_settings),
        web.get("/", dashboard.assets), web.get("/{name}", dashboard.assets),
    ])
    return app


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--port", type=int, default=8300)
    cli.add_argument("--arm-url", default="http://127.0.0.1:8890")
    cli.add_argument("--voice-bridge", default="http://127.0.0.1:8210")
    cli.add_argument("--clips", type=Path, default=ROOT / "robot-recordings")
    args = cli.parse_args()
    if not (ASSETS / "index.html").is_file():
        cli.error("Build the laptop dashboard first (npm run build in dashboard/apps/dashboard).")
    web.run_app(create_app(Dashboard(args.clips, args.arm_url, args.voice_bridge, args.port)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
