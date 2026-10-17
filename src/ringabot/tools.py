"""Function tools the voice agent can call during a SIP call."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from ringabot.signature import sign

if TYPE_CHECKING:
    from ringabot.calls import CallSession

BUILTIN = {"end_call", "transfer_call", "get_current_time"}


def tool_definitions(settings) -> list[dict]:
    tools: list[dict] = [
        {
            "type": "function",
            "name": "end_call",
            "description": "Hang up after saying goodbye. Use when the caller is done.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "get_current_time",
            "description": "Current date and time. Optional IANA timezone.",
            "parameters": {
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
            },
        },
    ]
    if settings.transfer_targets:
        tools.append(
            {
                "type": "function",
                "name": "transfer_call",
                "description": "Transfer the caller to a named person or line.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": sorted(settings.transfer_targets)}
                    },
                    "required": ["target"],
                },
            }
        )
    for spec in settings.extra_tools():
        if spec.get("name") in BUILTIN:
            continue
        tools.append({"type": "function", **{k: v for k, v in spec.items() if k != "type"}})
    return tools


async def run_tool(session: CallSession, name: str, raw_args: str) -> dict:
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        return {"error": "arguments were not valid JSON"}
    settings = session.settings
    if name == "end_call":
        session.request_hangup()
        return {"ok": True, "note": "Say a short goodbye; the call ends after it."}
    if name == "get_current_time":
        tz_name = str(args.get("timezone") or "America/Vancouver")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("America/Vancouver")
        return {"now": datetime.now(tz).isoformat(timespec="minutes"), "timezone": str(tz)}
    if name == "transfer_call":
        target = str(args.get("target") or "").lower()
        uri = settings.transfer_targets.get(target)
        if not uri:
            return {"error": "unknown transfer target"}
        session.start_transfer(uri)
        return {"ok": True, "note": "Transfer started. Tell the caller to hold briefly."}
    if not settings.tools_forward_url:
        return {"error": f"tool {name} is not available"}
    return await forward_tool(session, name, args)


async def forward_tool(session: CallSession, name: str, args: dict) -> dict:
    settings = session.settings
    body = json.dumps(
        {
            "type": "tool.call",
            "call_id": session.call_id,
            "caller": session.record.get("from"),
            "tool": name,
            "arguments": args,
        }
    ).encode()
    msg_id = "msg_" + uuid.uuid4().hex
    ts = str(int(time.time()))
    headers = {"content-type": "application/json", "webhook-id": msg_id, "webhook-timestamp": ts}
    if settings.tools_forward_secret:
        headers["webhook-signature"] = sign(settings.tools_forward_secret, msg_id, ts, body)
    try:
        async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as client:
            r = await client.post(settings.tools_forward_url, content=body, headers=headers)
        if r.status_code >= 400:
            return {"error": f"tool backend returned HTTP {r.status_code}"}
        try:
            return r.json()
        except ValueError:
            return {"result": r.text[:2000]}
    except httpx.HTTPError:
        return {"error": "tool backend unreachable or slow"}
