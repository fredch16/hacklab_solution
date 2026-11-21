"""M0.8: grab camera frames from Station's usb-video driver.

    python camera.py                 # save one frame to frame.jpg
    python camera.py --list          # list camera queues Station knows about
    python camera.py --queue usbvideo/<id> -o top.jpg

Station stores each USB camera in its own queue, usbvideo/<md5 of camera id>.
We find them in the "inference-states" queue, which lists every live queue
with its data type. Frames are JPEG, resized so the short side is
`resize_target` px (station.yaml, default 224; we use 720).
"""

import argparse
import asyncio
import time
from pathlib import Path

import station_arm  # noqa: F401  (sets up sys.path for the vendored SDK)
from station_arm import logger, new_station_client, run
from target.gen_python.protobuf.drivers.usbvideo import usbvideo
from target.gen_python.protobuf.station import drivers, inference


def _queue_name(queue: str) -> str:
    # The viewer adds the prefix when an entry carries only the camera hash.
    return queue if "/" in queue else f"usbvideo/{queue}"


async def list_camera_queues(client, timeout_s: float = 5.0) -> list[str]:
    q: asyncio.Queue = asyncio.Queue()
    client.follow("inference-states", q)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            entry = await asyncio.wait_for(q.get(), deadline - time.monotonic())
        except asyncio.TimeoutError:
            break
        if entry is None:
            break
        state = inference.InferenceRxReader(entry.Data)
        cams = sorted({
            _queue_name(e.get_queue())
            for e in (state.get_entries() or [])
            if e.get_type() == drivers.QueueDataType.QDT_USB_VIDEO_FRAMES
        })
        if cams:
            return cams
    return []


class Camera:
    """Follows one usbvideo queue and keeps the latest JPEG."""

    def __init__(self, client, queue: str):
        self.client = client
        self.queue = queue
        self.jpeg: bytes | None = None
        self.size: tuple[int, int] = (0, 0)
        self.stamp = 0.0
        self._q: asyncio.Queue = asyncio.Queue()
        self._errors = client.follow(queue, self._q)
        self._task = asyncio.create_task(self._drain())

    @classmethod
    async def connect(cls, server: str = "localhost", queue: str | None = None, client=None) -> "Camera":
        client = client or await new_station_client(server, logger)
        if queue is None:
            cams = await list_camera_queues(client)
            if not cams:
                raise RuntimeError("Station reports no USB camera; check the live view in the web UI")
            if len(cams) > 1:
                logger.warning("several cameras %s, using %s (pass --queue to pick)", cams, cams[0])
            queue = cams[0]
        return cls(client, queue)

    async def _drain(self):
        while True:
            if not self._errors.empty():
                raise RuntimeError(f"{self.queue} stream error: {self._errors.get_nowait()}")
            entry = await self._q.get()
            if entry is None:
                raise RuntimeError(f"{self.queue} stream closed")
            env = usbvideo.RxEnvelopeReader(entry.Data)
            if env.get_type() != usbvideo.RxEnvelopeType.ET_FRAMES:
                continue
            frames = env.get_frames()
            data = frames.get_frames_data() if frames else None
            if not data:
                continue
            fmt = frames.get_format()
            self.size = (fmt.get_width(), fmt.get_height()) if fmt else (0, 0)
            self.jpeg = bytes(data[-1])
            self.stamp = time.monotonic()

    async def get_frame(self, max_age_s: float = 1.0, timeout_s: float = 5.0) -> bytes:
        """Latest JPEG, waiting up to timeout_s for one newer than max_age_s."""
        deadline = time.monotonic() + timeout_s
        while self.jpeg is None or time.monotonic() - self.stamp > max_age_s:
            if self._task.done():
                self._task.result()
            if time.monotonic() > deadline:
                raise RuntimeError(f"no fresh frame on {self.queue} after {timeout_s}s")
            await asyncio.sleep(0.02)
        return self.jpeg

    def close(self):
        self._task.cancel()


async def main(args):
    client = await new_station_client(args.server, logger)
    try:
        if args.list:
            cams = await list_camera_queues(client)
            print("\n".join(cams) if cams else "no cameras")
            return
        cam = await Camera.connect(queue=args.queue, client=client)
        jpeg = await cam.get_frame()
        Path(args.output).write_bytes(jpeg)
        print(f"saved {args.output} ({cam.size[0]}x{cam.size[1]}, {len(jpeg)} bytes) from {cam.queue}")
        cam.close()
    finally:
        client.close_connection()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="localhost")
    p.add_argument("--queue", help="camera queue, e.g. usbvideo/<id> (default: first camera)")
    p.add_argument("--list", action="store_true", help="list camera queues and exit")
    p.add_argument("-o", "--output", default="frame.jpg")
    run(main(p.parse_args()))
