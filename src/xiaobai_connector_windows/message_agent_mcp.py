"""Minimal stdio MCP endpoint for one Connector Agent turn.

The child process never talks to the Xiaobai server. It can only forward a
validated ``message_agent(target, message)`` request to the parent adapter's
ephemeral loopback bridge.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any


TOOL = {
    "name": "message_agent",
    "description": "向当前家庭群中已加入的协作 Agent 投递一条消息；立即返回投递结果，不等待回复。",
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "target": {"type": "string", "minLength": 1, "maxLength": 100},
            "message": {"type": "string", "minLength": 1, "maxLength": 16000},
        },
        "required": ["target", "message"],
    },
}


def _reply(request_id: Any, result: dict[str, Any] | None = None,
           error: str | None = None) -> None:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        value["result"] = result or {}
    else:
        value["error"] = {"code": -32602, "message": error}
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _call_parent(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"target", "message"}:
        raise ValueError("message_agent 只接受 target 和 message")
    target, message = str(arguments["target"] or ""), str(arguments["message"] or "")
    if not target or not message or len(target) > 100 or len(message) > 16_000:
        raise ValueError("message_agent 参数无效")
    address = os.environ.get("XIAOBAI_MCP_BRIDGE", "")
    token = os.environ.get("XIAOBAI_MCP_TOKEN", "")
    host, separator, raw_port = address.rpartition(":")
    if not host or not separator or not token:
        raise RuntimeError("小白协作工具未配置")
    with socket.create_connection((host, int(raw_port)), timeout=10) as conn:
        wire = conn.makefile("rwb")
        wire.write((json.dumps({"token": token, "target": target, "message": message},
                               ensure_ascii=False) + "\n").encode())
        wire.flush()
        raw = wire.readline(32_768)
    if not raw:
        raise RuntimeError("小白协作工具未返回结果")
    result = json.loads(raw)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "投递未完成"))
    return result


def main() -> int:
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        request_id, method = request.get("id"), request.get("method")
        if request_id is None:
            continue
        if method == "initialize":
            _reply(request_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "xiaobai-message-agent", "version": "1"},
            })
        elif method == "tools/list":
            _reply(request_id, {"tools": [TOOL]})
        elif method == "tools/call":
            try:
                result = _call_parent((request.get("params") or {}).get("arguments"))
                _reply(request_id, {"content": [{
                    "type": "text", "text": "已投递；同伴回复会异步回到本群。"}],
                    "structuredContent": {
                        "sent": True, "child_run_id": result.get("child_run_id", "")},
                })
            except Exception as exc:
                _reply(request_id, {"content": [{
                    "type": "text", "text": "投递未完成：" + str(exc)[:200]}],
                    "isError": True})
        else:
            _reply(request_id, error="不支持的方法")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
