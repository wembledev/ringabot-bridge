"""Streamable-HTTP MCP at /mcp. JSON responses, no side-channel broker."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ringabot import __version__

PROTOCOL = "2025-03-26"
_SUPPORTED = {PROTOCOL, "2024-11-05"}

ToolFn = Callable[[dict], Awaitable[dict]]


def tool_specs() -> list[dict]:
    return [
        {
            "name": "connect_number",
            "description": (
                "Store Voip.ms SIP settings and try a test REGISTER. The password is write-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "voipms_server": {"type": "string"},
                    "sip_user": {"type": "string"},
                    "sip_password": {"type": "string"},
                    "did": {
                        "type": "string",
                        "description": "E.164 caller ID, fictional examples use +15555550100",
                    },
                },
                "required": ["voipms_server", "sip_user", "sip_password", "did"],
            },
        },
        {
            "name": "link_voice_agent",
            "description": "Validate an xAI key and store the persona. The key is write-only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "xai_api_key": {"type": "string"},
                    "persona_name": {"type": "string"},
                    "instructions": {"type": "string"},
                    "voice": {"type": "string"},
                },
                "required": ["xai_api_key", "persona_name", "instructions"],
            },
        },
        {
            "name": "get_setup_status",
            "description": (
                "Report whether a number and voice key are configured, plus current limits."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "place_call",
            "description": (
                "Place one outbound call. The assistant always opens by saying it is an AI. "
                "Destinations must match the allowlist. There is no bulk-dial tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "number": {"type": "string"},
                    "briefing": {"type": "string"},
                    "questions": {"type": "array", "items": {"type": "string"}},
                    "max_minutes": {"type": "number"},
                },
                "required": ["number", "briefing"],
            },
        },
        {
            "name": "get_call",
            "description": "Status, transcript, and summary for one call.",
            "inputSchema": {
                "type": "object",
                "properties": {"call_id": {"type": "string"}},
                "required": ["call_id"],
            },
        },
        {
            "name": "list_calls",
            "description": "Recent calls for this tenant.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "since": {"type": "string"},
                },
            },
        },
        {
            "name": "hang_up",
            "description": "Hang up one active call.",
            "inputSchema": {
                "type": "object",
                "properties": {"call_id": {"type": "string"}},
                "required": ["call_id"],
            },
        },
        {
            "name": "delete_account",
            "description": "Wipe stored credentials, recordings, and transcripts for this tenant.",
            "inputSchema": {
                "type": "object",
                "properties": {"confirm": {"type": "boolean"}},
                "required": ["confirm"],
            },
        },
    ]


async def read_limited(request: Request, limit: int = 65536) -> bytes | None:
    """Read the body, but stop once it passes the cap so it cannot fill RAM."""
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def handle_mcp(request: Request, tools: dict[str, ToolFn], read_resource) -> Response:
    if request.method == "GET":
        return JSONResponse({"error": "method_not_allowed"}, status_code=405)
    if request.method == "DELETE":
        return Response(status_code=204)
    raw = await read_limited(request)
    if raw is None:
        return JSONResponse({"error": "too_large"}, status_code=413)
    try:
        message = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _rpc_error(None, -32700, "parse error")
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "invalid request")
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(msg_id, -32600, "invalid request")
    if not isinstance(method, str):
        return _rpc_error(msg_id, -32600, "invalid request")
    if method.startswith("notifications/"):
        return Response(status_code=202)
    if method == "ping":
        return _result(msg_id, {})
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in _SUPPORTED:
            version = PROTOCOL
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {}},
                "serverInfo": {"name": "ringabot-bridge", "version": __version__},
            },
        )
    if method == "tools/list":
        return _result(msg_id, {"tools": tool_specs()})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(msg_id, -32602, "invalid arguments")
        if name not in tools:
            return _rpc_error(msg_id, -32602, "unknown tool")
        try:
            payload = await tools[name](arguments)
        except Exception:
            payload = {"ok": False, "error": "internal"}
        is_error = payload.get("ok") is False or payload.get("error")
        return _result(
            msg_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": bool(is_error),
            },
        )
    if method == "resources/list":
        items = await read_resource(None)
        return _result(msg_id, {"resources": items})
    if method == "resources/read":
        uri = params.get("uri")
        body = await read_resource(uri)
        if body is None:
            return _rpc_error(msg_id, -32002, "resource not found")
        return _result(
            msg_id,
            {"contents": [{"uri": uri, "mimeType": "application/json", "text": body}]},
        )
    return _rpc_error(msg_id, -32601, "method not found")


def _result(msg_id, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    )
