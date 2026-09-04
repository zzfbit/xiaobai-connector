"""Background asyncio lifecycle for the desktop UI."""

from __future__ import annotations

import asyncio
import threading
from typing import Callable

from .adapters.router import AdapterRouter
from .config import ConnectorConfig
from .credentials import CredentialStore, SERVICE
from .gateway import GatewayClient
from .paths import spool_path


class ConnectorRuntime:
    def __init__(self, config: ConnectorConfig,
                 *, on_status: Callable[[str, str], None] | None = None):
        self.config = config
        self.on_status = on_status
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._client: GatewayClient | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        token = CredentialStore(SERVICE).get(self.config.device_id)
        if not token:
            self._notify("error", "没有找到设备凭据，请重新配对")
            return
        self._thread = threading.Thread(target=self._thread_main, args=(token,),
                                         name="xiaobai-connector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop, event = self._loop, self._stop
        if loop and event:
            loop.call_soon_threadsafe(event.set)

    def _thread_main(self, token: str) -> None:
        try:
            asyncio.run(self._run(token))
        except Exception as exc:
            self._notify("error", str(exc)[:300])

    async def _run(self, token: str) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        router = AdapterRouter(self.config.selected_agents)
        self._client = GatewayClient(self.config, token, router,
                                     spool_path=spool_path(),
                                     on_status=self._notify)
        try:
            await self._client.run_forever(self._stop)
        finally:
            self._client = None
            self._loop = None
            self._stop = None

    def _notify(self, state: str, detail: str = "") -> None:
        if self.on_status:
            try:
                self.on_status(state, detail)
            except Exception:
                pass
