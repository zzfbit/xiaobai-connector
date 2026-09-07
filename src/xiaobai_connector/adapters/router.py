"""Select adapters only from the Connector's locally saved snapshot."""

from __future__ import annotations

from typing import Any

from ..models import ExecutionAdapter, LocalAgent
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .hermes import HermesAdapter


class AdapterRouter:
    def __init__(self, definitions: list[dict[str, Any]]):
        self._adapters: dict[str, ExecutionAdapter] = {}
        self._adapter_order: list[ExecutionAdapter] = []
        refs: set[str] = set()
        for definition in definitions:
            if not bool(definition.get("enabled")):
                continue
            adapter_name = str(definition.get("adapter") or "").lower()
            if adapter_name == "codex":
                adapter: ExecutionAdapter = CodexAdapter(definition)
            elif adapter_name == "claude":
                adapter = ClaudeAdapter(definition)
            elif adapter_name == "hermes":
                adapter = HermesAdapter(definition)
            else:
                continue
            local_ref = str(definition.get("local_ref") or "").strip()
            if local_ref:
                if local_ref in refs:
                    raise ValueError("同一 Connector 的 local_ref 不能重复")
                self._adapters[local_ref] = adapter
                self._adapter_order.append(adapter)
                refs.add(local_ref)

    def discover(self) -> list[LocalAgent]:
        result: list[LocalAgent] = []
        for local_ref, adapter in self._adapters.items():
            result.extend(adapter.discover())
        return result

    def history_snapshots(self) -> list[dict[str, Any]]:
        """Collect optional durable-history projections from local adapters."""
        result: list[dict[str, Any]] = []
        for adapter in self._adapter_order:
            reader = getattr(adapter, "history_snapshots", None)
            if callable(reader):
                values = reader()
                if values:
                    result.extend(item for item in values if isinstance(item, dict))
        return result

    def status_snapshots(self) -> list[dict[str, Any]]:
        """Collect optional live-work projections from local adapters."""
        result: list[dict[str, Any]] = []
        for adapter in self._adapter_order:
            reader = getattr(adapter, "status_snapshots", None)
            if callable(reader):
                values = reader()
                if values:
                    result.extend(item for item in values if isinstance(item, dict))
        return result

    def adapter_for(self, local_ref: str) -> ExecutionAdapter:
        try:
            return self._adapters[local_ref]
        except KeyError as exc:
            raise RuntimeError("Agent 不属于当前 Connector 或已被禁用") from exc

    def contains(self, local_ref: str) -> bool:
        return local_ref in self._adapters

    async def queue_thread_message(self, *, local_ref: str, thread_id: str,
                                   text: str,
                                   attachments: list[dict[str, Any]] | None = None,
                                   client_message_id: str) -> bool:
        """Route a desktop-thread queue insertion to its local adapter."""
        adapter = self._adapters.get(local_ref)
        if adapter is None:
            return False
        queue = getattr(adapter, "queue_thread_message", None)
        if not callable(queue):
            return False
        return bool(await queue(
            thread_id=thread_id, text=text, attachments=attachments,
            client_message_id=client_message_id))
