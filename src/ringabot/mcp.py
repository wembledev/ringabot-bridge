"""Minimal MCP server (Streamable HTTP, stateless, JSON responses) at POST /mcp.

Tools: place_call, list_calls, hang_up, get_number.
No sessions, no SSE, no extra dependency.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ringabot import __version__
from ringabot.billing import BillingError
from ringabot.store import Account, Admin

VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")
MAX_BODY = 64 * 1024

TOOLS = [
    {
        "name": "place_call",
        "description": (
            "Phone a number and have the Ringabot voice agent handle the call. The agent says "
            "it is an AI. Only numbers in the server's allow list can be called; never "
            "emergency numbers. Returns a call_id; use list_calls to follow it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number, E.164 or 10 digits."},
                "briefing": {
                    "type": "string",
                    "description": "What the call is for and what to find out (max 2000 chars).",
                },
            },
            "required": ["number", "briefing"],
        },
    },
    {
        "name": "list_calls",
        "description": "Recent outbound calls (newest first) with status and transcript.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        },
    },
    {
        "name": "hang_up",
        "description": "End an active call by call_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"call_id": {"type": "string"}},
            "required": ["call_id"],
        },
    },
    {
        "name": "get_number",
        "description": (
            "Start a monthly phone-number subscription and return a Stripe Checkout URL. "
            "The user opens that URL once to approve the charge. currency is usd or cad. "
            "area_code is optional and is kept for when the number is provisioned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "currency": {"type": "string", "enum": ["usd", "cad"]},
                "area_code": {"type": "string", "description": "Optional area code, digits only."},
            },
            "required": ["currency"],
        },
    },
]


def _ok(rid, result) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


def _err(rid, code: int, message: str, status: int = 200) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status)


def _text(data, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}], "isError": is_error}


def _needs_number(store, who) -> dict | None:
    if isinstance(who, Account) and not store.has_active(who.id):
        return _text({"error": "no active number"}, True)
    return None


def _call_tool(manager, store, billing, who, name: str, args: dict) -> dict:
    if name == "get_number":
        if not isinstance(who, Account):
            return _text({"error": "get_number needs an account token from POST /signup"}, True)
        try:
            return _text(
                billing.checkout(who, str(args.get("currency", "")), str(args.get("area_code", "")))
            )
        except BillingError as exc:
            return _text({"error": str(exc)}, True)
    if name == "place_call":
        if blocked := _needs_number(store, who):
            return blocked
        account_id = who.id if isinstance(who, Account) else None
        call_id, detail = manager.place(
            str(args.get("number", "")),
            str(args.get("briefing", "")),
            account_id=account_id,
        )
        if call_id is None:
            return _text({"error": detail}, True)
        return _text({"call_id": call_id, "status": detail})
    if name == "list_calls":
        if blocked := _needs_number(store, who):
            return blocked
        try:
            limit = max(1, min(20, int(args.get("limit", 10))))
        except (TypeError, ValueError):
            limit = 10
        keep = ("call_id", "to", "status", "started_at", "ended_at", "end_reason", "transcript")
        calls = [
            {k: r.get(k) for k in keep if k in r}
            for r in manager.recent
            if r.get("direction") == "outbound"
            and (isinstance(who, Admin) or r.get("account_id") == who.id)
        ]
        return _text({"calls": calls[:limit]})
    if name == "hang_up":
        call = manager.active.get(str(args.get("call_id", "")))
        if call is None or (isinstance(who, Account) and call.record.get("account_id") != who.id):
            return _text({"error": "no such active call"}, True)
        manager.spawn(call.hangup("mcp"))
        return _text({"ok": True})
    raise KeyError(name)


def make_endpoint(manager, identify, store, billing):
    async def mcp(request: Request) -> Response:
        who = identify(request)
        if who is None:
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"www-authenticate": "Bearer"},
            )
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(None, -32600, "request too large", 413)
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            return _err(None, -32700, "parse error", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(None, -32600, "invalid request", 400)
        method, rid = msg.get("method"), msg.get("id")
        if "id" not in msg or "method" not in msg:  # notification or response
            return Response(status_code=202)
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _err(rid, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _ok(
                rid,
                {
                    "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ringabot", "version": __version__},
                },
            )
        if method == "ping":
            return _ok(rid, {})
        if method == "tools/list":
            return _ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return _err(rid, -32602, "arguments must be an object")
            try:
                result = _call_tool(
                    manager, store, billing, who, str(params.get("name", "")), args
                )
                return _ok(rid, result)
            except KeyError:
                return _err(rid, -32602, "unknown tool")
        return _err(rid, -32601, "method not found")

    return mcp
