"""Local DNS-SD candidates. Advertisements are hints until QR pairing succeeds."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

SERVICE_TYPE = "_localvision._tcp.local."
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredPhone:
    label: str
    port: int
    fingerprint: str
    addresses: tuple[str, ...]


class PhoneDiscovery:
    def __init__(self):
        self.phones: dict[str, DiscoveredPhone] = {}
        self.pending: dict[str, asyncio.Task] = {}
        self.client = None
        self.browser = None

    def start(self):
        try:
            self.client = AsyncZeroconf(ip_version=IPVersion.V4Only)
            self.browser = AsyncServiceBrowser(self.client.zeroconf, SERVICE_TYPE, handlers=[self._changed])
        except OSError as error:
            LOG.warning("Phone discovery unavailable: %s", error)

    async def close(self):
        if self.browser:
            await self.browser.async_cancel()
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.async_close()

    def _changed(self, zeroconf, service_type, name, state_change):
        pending = self.pending.pop(name, None)
        if pending:
            pending.cancel()
        if state_change is ServiceStateChange.Removed:
            self.phones.pop(name, None)
        elif len(self.pending) < 32:
            self.pending[name] = asyncio.create_task(self._resolve(zeroconf, service_type, name))

    async def _resolve(self, zeroconf, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            if not await info.async_request(zeroconf, 2500):
                return
            properties = info.properties
            digest = properties.get(b"sha256")
            addresses = info.parsed_addresses(IPVersion.V4Only)
            if properties.get(b"v") != b"2" or not isinstance(digest, bytes) or not addresses or not info.port:
                self.phones.pop(name, None)
                return
            with suppress(UnicodeDecodeError):
                if len(self.phones) < 32 or name in self.phones:
                    self.phones[name] = DiscoveredPhone(
                        name.removesuffix("." + SERVICE_TYPE), info.port, digest.decode("ascii"), tuple(addresses[:8]))
        finally:
            if self.pending.get(name) is asyncio.current_task():
                self.pending.pop(name, None)
