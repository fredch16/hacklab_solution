"""Laptop gateway and recording policy tests. Run with python -m unittest discover -s tests -p test_laptop_dashboard.py."""
import asyncio
import hashlib
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

import voice_bridge

from laptop_dashboard import Dashboard, PhoneTarget, RecordingLease, create_app
from mcp_server import Control, SceneStore, build_server
from phone_discovery import DiscoveredPhone


class LaptopDashboardTest(unittest.TestCase):
    def test_only_pinned_private_phone_targets(self):
        target = PhoneTarget.parse("https://192.168.1.20:8443", "ab:" * 31 + "ab")
        self.assertEqual(target.origin, "https://192.168.1.20:8443")
        for url, pin in [
            ("http://192.168.1.20:8443", "ab" * 32),
            ("https://8.8.8.8:443", "ab" * 32),
            ("https://192.168.1.20:8443/path", "ab" * 32),
            ("https://192.168.1.20:8443", "bad"),
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                PhoneTarget.parse(url, pin)

    def test_recording_lease_requires_current_view_and_fresh_frames(self):
        lease = RecordingLease()
        self.assertFalse(lease.live(10))
        lease.viewed(10)
        self.assertFalse(lease.live(10))
        lease.frame(10)
        self.assertTrue(lease.live(11))
        lease.viewed(13)
        self.assertFalse(lease.live(13))
        lease.frame(13)
        self.assertFalse(lease.live(17))


class DashboardIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.client = ClientSession()
        self.addAsyncCleanup(self.client.close)

    async def serve(self, app, tls=None):
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
        await site.start()
        return f"{'https' if tls else 'http'}://127.0.0.1:{runner.addresses[0][1]}"

    async def dashboard(self, arm="http://127.0.0.1:9"):
        dashboard = Dashboard(self.folder, arm, "http://127.0.0.1:9", 0)
        base = await self.serve(create_app(dashboard))
        dashboard.origins = {base}
        return base

    async def test_phone_pairing_uses_pinned_tls_and_native_recording_ack(self):
        key, cert = self.folder / "key.pem", self.folder / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)

        async def session(request):
            return web.json_response({"paired": request.cookies.get("lvc_session") == "paired"})

        async def pair(request):
            self.assertEqual(request.headers["Origin"], f"https://{request.host}")
            response = web.json_response({"paired": True})
            response.set_cookie("lvc_session", "paired", secure=True, httponly=True)
            return response

        async def events(request):
            self.assertEqual(request.cookies.get("lvc_session"), "paired")
            self.assertEqual(request.headers["Origin"], f"https://{request.host}")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "session.snapshot", "session_id": "active"})
            async for item in ws:
                command = item.json()
                self.assertEqual(command["type"], "recording.start")
                await ws.send_json({"type": "command.result", "request_id": command["request_id"], "payload": {"ok": True}})
            return ws

        app = web.Application()
        app.add_routes([web.get("/api/v1/session", session), web.post("/api/v1/pair", pair),
                        web.get("/api/v1/events", events)])
        phone = await self.serve(app, tls)
        base = await self.dashboard()
        async with self.client.post(base + "/laptop/phone", json={"address": phone, "fingerprint": "0" * 64}) as r:
            self.assertEqual(r.status, 502)
        async with self.client.post(base + "/laptop/phone", json={"address": phone, "fingerprint": fingerprint}) as r:
            self.assertEqual(r.status, 200)
        async with self.client.post(base + "/api/v1/pair", json={"code": "123456"}) as r:
            self.assertEqual(r.status, 200)
            self.assertNotIn("Set-Cookie", r.headers)
        async with self.client.ws_connect(base + "/api/v1/events?tab=" + "1" * 32, origin=base) as ws:
            self.assertEqual((await ws.receive_json())["type"], "session.snapshot")
            mcp = build_server(Control("http://127.0.0.1:9"), SceneStore(self.folder / "scene.json"),
                               [{"name": "top"}], Control(base))
            bridge = voice_bridge.Bridge(mcp)
            stale = await bridge._run("start camera recording", "1" * 32, "start recording", "stale")
            self.assertEqual(stale["error"], "PHONE_DISCONNECTED")
            result = await bridge._run("start camera recording", "1" * 32, "start recording", "active")
            self.assertEqual(result, {"ok": True, "command": "recording.start",
                                      "message": "Phone recording started.", "error": None})

    async def test_discovered_phone_cannot_pair_without_the_optical_secret(self):
        key, cert = self.folder / "key.pem", self.folder / "cert.pem"
        await asyncio.to_thread(
            subprocess.run,
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        observed = {}
        proof = "forged"

        async def session(request):
            return web.json_response({"paired": True})

        async def pair(request):
            observed.update(await request.json())
            response = web.json_response({"request_id": "A" * 22, "qr": "untrusted"})
            response.set_cookie("lvc_pending", "fresh")
            response.set_cookie("lvc_session", "must-not-be-copied")
            return response

        async def poll(request):
            self.assertEqual(request.cookies.get("lvc_pending"), "fresh")
            response = web.json_response({"state": "approved", "proof": proof})
            response.set_cookie("lvc_session", "approved")
            return response

        fake = web.Application()
        fake.add_routes([web.get("/api/v1/session", session), web.post("/api/v1/pair/request", pair),
                         web.get("/api/v1/pair/request/status", poll)])
        phone = await self.serve(fake, tls)
        dashboard = Dashboard(self.folder, "http://127.0.0.1:9", "http://127.0.0.1:9", 0)
        base = await self.serve(create_app(dashboard))
        dashboard.origins = {base}
        service = "test._localvision._tcp.local."
        dashboard.discovery.phones[service] = DiscoveredPhone("Test phone", int(phone.rsplit(":", 1)[1]),
                                                              fingerprint, ("127.0.0.1",))

        async with self.client.get(base + "/laptop/discovery") as r:
            self.assertEqual(await r.json(), {"phones": [{"id": service, "label": "Test phone"}]})
        async with self.client.post(base + "/laptop/phone", json={"service": service}) as r:
            self.assertTrue((await r.json())["qr_only"])
        async with self.client.get(base + "/api/v1/session") as r:
            self.assertEqual(await r.json(), {"paired": False})
        async with self.client.post(base + "/api/v1/pair", json={"code": "123456"}) as r:
            self.assertEqual(r.status, 403)

        async def request_qr():
            async with self.client.post(base + "/api/v1/pair/request", json={}) as r:
                qr = (await r.json())["qr"]
            self.assertTrue(qr.startswith("lvc-pair:2:" + "A" * 22 + ":"))
            secret = qr.split(":")[-1]
            self.assertEqual(observed, {"challenge": hashlib.sha256(secret.encode()).hexdigest()})
            return secret

        await request_qr()
        self.assertNotIn("lvc_session", dashboard.phone_cookies)
        async with self.client.get(base + "/api/v1/pair/request/status") as r:
            self.assertEqual((await r.json())["error"], "QR_PROOF_INVALID")
        self.assertNotIn("lvc_session", dashboard.phone_cookies)

        proof = await request_qr()
        async with self.client.get(base + "/api/v1/pair/request/status") as r:
            self.assertEqual(await r.json(), {"state": "approved"})
        self.assertEqual(dashboard.phone_cookies["lvc_session"], "approved")

    async def test_director_calls_are_relayed_to_the_arm_server_only(self):
        seen = []

        async def director(request):
            seen.append((request.method, request.match_info["path"], request.query_string,
                         await request.json() if request.method == "POST" else None))
            if request.match_info["path"] == "frame.jpg":
                return web.Response(body=b"\xff\xd8jpeg", content_type="image/jpeg")
            return web.json_response({"ok": True}, status=201)

        arm = web.Application()
        arm.router.add_route("*", "/api/director/{path:.*}", director)
        base = await self.dashboard(await self.serve(arm))
        async with self.client.get(base + "/laptop/director/state") as response:
            self.assertEqual((response.status, await response.json()), (201, {"ok": True}))
        async with self.client.get(base + "/laptop/director/frame.jpg?id=7") as response:
            self.assertEqual((response.content_type, await response.read()), ("image/jpeg", b"\xff\xd8jpeg"))
        async with self.client.post(base + "/laptop/director/select", headers={"Origin": base},
                                    json={"frame_id": 7, "box": [0, 0, 1, 1]}) as response:
            self.assertEqual(response.status, 201)
        for bad in ["../stop", "state%2F..%2F..%2Fapi%2Fstop", "STATE", "a/b/c/d"]:
            async with self.client.get(base + "/laptop/director/" + bad) as response:
                self.assertEqual(response.status, 404, bad)
        async with self.client.post(base + "/laptop/director/select", headers={"Origin": "http://evil.example"},
                                    json={}) as response:
            self.assertEqual(response.status, 403)
        self.assertEqual(seen, [("GET", "state", "", None), ("GET", "frame.jpg", "id=7", None),
                                ("POST", "select", "", {"frame_id": 7, "box": [0, 0, 1, 1]})])

    async def test_robot_closes_and_finalizes_valid_clip_when_view_socket_closes(self):
        jpeg = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=size=64x48",
                               "-frames:v", "1", "-threads", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                              check=True, capture_output=True).stdout

        async def stream(request):
            response = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"})
            await response.prepare(request)
            part = b"--frame\r\nContent-Type: image/jpeg\r\n" + f"Content-Length: {len(jpeg)}".encode() + b"\r\n\r\n" + jpeg + b"\r\n"
            try:
                while True:
                    await response.write(part)
                    await asyncio.sleep(0.08)
            except ConnectionResetError:
                return response

        arm = web.Application()
        arm.router.add_get("/stream", stream)
        base = await self.dashboard(await self.serve(arm))
        tab, other = "1" * 32, "2" * 32

        async def record(action, owner):
            async with self.client.post(base + "/laptop/recording", headers={"Origin": base},
                                        json={"camera": "robot", "action": action, "tab": owner}) as response:
                return await response.json()

        self.assertEqual((await record("start", tab))["error"], "ROBOT_FEED_NOT_VIEWED")
        async with self.client.ws_connect(base + "/laptop/robot/events/" + tab, origin=base) as ws:
            await ws.send_str("viewing")
            self.assertEqual(await asyncio.wait_for(ws.receive_bytes(), 3), jpeg)
            self.assertEqual((await record("start", other))["error"], "ROBOT_FEED_NOT_VIEWED")
            self.assertTrue((await record("start", tab))["ok"])
            self.assertEqual((await record("stop", other))["error"], "NOT_RECORDING")
            self.assertEqual((await record("start", tab))["error"], "ALREADY_RECORDING")
            for _ in range(8):
                await asyncio.wait_for(ws.receive_bytes(), 2)
        for _ in range(30):
            async with self.client.get(base + "/laptop/robot/state/" + tab) as response:
                status = await response.json()
            if status["clip"]:
                break
            await asyncio.sleep(0.1)
        self.assertFalse(status["recording"])
        self.assertTrue(status["clip"], status)
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                 "-of", "csv=p=0", str(self.folder / status["clip"])],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "video")

        class Scripted:
            def interpret(self, text, views, current):
                return {"action": "start_robot_recording", "view": ""}

        async def arm_state(request):
            return web.json_response({"cards": [{"name": "top", "checked": True}], "current_view": "top"})

        control = web.Application()
        control.router.add_get("/api/state", arm_state)
        mcp = build_server(Control(await self.serve(control)), SceneStore(self.folder / "scene.json"),
                           [{"name": "top"}], Control(base))
        bridge = voice_bridge.Bridge(mcp, interpreter=Scripted())
        rejected = await bridge._run("could you film this with the robot camera", tab)
        self.assertEqual(rejected["error"], "ROBOT_FEED_NOT_VIEWED")
        async with self.client.ws_connect(base + "/laptop/robot/events/" + tab, origin=base) as ws:
            await ws.send_str("viewing")
            await asyncio.wait_for(ws.receive_bytes(), 2)
            result = await bridge._run("could you film this with the robot camera", tab)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["command"], "robot.recording.start")
            for _ in range(6):
                await asyncio.wait_for(ws.receive_bytes(), 2)
            stopped = await bridge._run("stop robot recording", tab)
            self.assertEqual(stopped, {"ok": True, "command": "robot.recording.stop",
                                       "message": "Robot camera recording saved.", "error": None})

    def test_claude_context_preserves_the_acoustic_gate_and_target(self):
        check = voice_bridge.recording_context
        self.assertEqual(check("robot camera recording", "start robot recording"), ("robot", "start"))
        self.assertEqual(check("start camera recording", None), ("phone", "reject"))
        self.assertEqual(check("stop robot recording", None), ("robot", "stop"))
        self.assertEqual(check("do not stop robot recording", None), ("robot", "reject"))
        self.assertEqual(check("stop recording", None), ("phone", "reject"))
        self.assertEqual(check("don't start robot recording", "start robot recording"), ("robot", "reject"))
        self.assertEqual(check("could you capture and start filming", None), ("phone", "reject"))
        self.assertEqual(check("could you film this with the robot camera", None), ("robot", None))
        self.assertEqual(check("where is the camera", None), ("phone", "unrelated"))
        self.assertEqual(check("stout recording", None), ("phone", "reject"))


if __name__ == "__main__":
    unittest.main()
