"""The one HTTP server: call API, MCP, and inbound webhooks."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ringabot.auth import StaticBearerAuth
from ringabot.call import CallService, probe_register
from ringabot.config import Settings
from ringabot.limits import LimitError
from ringabot.mcp import handle_mcp
from ringabot.probes import validate_xai_key
from ringabot.records import enforce_retention
from ringabot.redact import install_logging
from ringabot.setup import SetupService
from ringabot.store import Store
from ringabot.webhooks import WebhookClient, WebhookVerifier

log = logging.getLogger("ringabot.api")

_HTTP = {
    "destination_not_allowed": 400,
    "invalid_number": 400,
    "emergency_not_allowed": 400,
    "briefing_required": 400,
    "briefing_too_long": 400,
    "questions_invalid": 400,
    "max_minutes_too_small": 400,
    "invalid_did": 400,
    "not_configured": 409,
    "tenant_busy": 409,
    "busy": 429,
    "daily_cap": 429,
}


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = asyncio.get_running_loop().time()
        window = [stamp for stamp in self._hits.get(key, []) if now - stamp < 60]
        if len(window) >= self.per_minute:
            self._hits[key] = window
            return False
        window.append(now)
        self._hits[key] = window
        return True


def create_app(
    settings: Settings,
    store: Store,
    *,
    call_service: CallService | None = None,
    setup: SetupService | None = None,
    webhook_client: WebhookClient | None = None,
    key_transport=None,
) -> Starlette:
    auth = StaticBearerAuth(settings.api_token)
    verifier = WebhookVerifier(
        secret=settings.tools_webhook_secret,
        bearer_token=settings.api_token,
        window_seconds=settings.webhook_replay_window,
    )
    if webhook_client is None:
        webhook_client = WebhookClient(
            settings.tools_webhook_url,
            verifier,
            timeout=settings.webhook_timeout,
        )
    if call_service is None:
        call_service = CallService(settings, store, webhook_client)
    if setup is None:

        async def key_probe(api_key: str) -> tuple[bool, str]:
            return await validate_xai_key(settings.xai_api_base, api_key, transport=key_transport)

        setup = SetupService(
            settings,
            store,
            register_probe=probe_register,
            key_probe=key_probe,
        )
    limiter = RateLimiter(settings.rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_app):
        install_logging(settings.log_level)
        store.fail_orphans()
        enforce_retention(store, settings.retention_days)
        yield
        for task in list(call_service.tasks.values()):
            task.cancel()

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/status", status, methods=["GET"]),
            Route("/call", post_call, methods=["POST"]),
            Route("/calls", list_calls, methods=["GET"]),
            Route("/calls/{call_id}", get_call, methods=["GET"]),
            Route("/calls/{call_id}/hangup", post_hangup, methods=["POST"]),
            Route("/hangup", post_hangup, methods=["POST"]),
            Route("/recordings", list_recordings, methods=["GET"]),
            Route("/mcp", mcp, methods=["POST", "GET", "DELETE"]),
            Route("/webhook", webhook, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.auth = auth
    app.state.calls = call_service
    app.state.setup = setup
    app.state.verifier = verifier
    app.state.webhook_client = webhook_client
    app.state.limiter = limiter
    app.state.webhook_jobs: list[asyncio.Task] = []
    return app


def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def status(request: Request) -> Response:
    tenant = _tenant(request)
    if isinstance(tenant, Response):
        return tenant
    body = request.app.state.setup.status(tenant)
    body["rss_bytes"] = _rss()
    return JSONResponse(body)


async def post_call(request: Request) -> Response:
    tenant = _tenant(request)
    if isinstance(tenant, Response):
        return tenant
    payload = await _json(request)
    if isinstance(payload, Response):
        return payload
    try:
        result = await request.app.state.calls.place(
            tenant,
            number=str(payload.get("number", "")),
            briefing=str(payload.get("briefing", "")),
            questions=payload.get("questions"),
            max_minutes=payload.get("max_minutes"),
        )
    except LimitError as exc:
        return _limit(exc)
    return JSONResponse(result, status_code=202)


async def list_calls(request: Request) -> Response:
    tenant = _tenant(request)
    if isinstance(tenant, Response):
        return tenant
    limit = _bounded_int(request.query_params.get("limit"), default=20, upper=100)
    since = _since(request.query_params.get("since"))
    rows = request.app.state.store.list_calls(tenant, limit=limit, since=since)
    return JSONResponse({"calls": [_public_call(request, row) for row in rows]})


async def get_call(request: Request) -> Response:
    tenant = _tenant(request)
    if isinstance(tenant, Response):
        return tenant
    row = request.app.state.store.get_call(tenant, request.path_params["call_id"])
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(_public_call(request, row))


async def post_hangup(request: Request) -> Response:
    tenant = _tenant(request)
    if isinstance(tenant, Response):
        return tenant
    call_id = request.path_params.get("call_id")
    if not call_id:
        payload = await _json(request)
        if isinstance(payload, Response):
            return payload
        call_id = str(payload.get("call_id", ""))
    if not request.app.state.store.get_call(tenant, call_id):
        return JSONResponse({"error": "not_found"}, status_code=404)
    request.app.state.calls.hangup(tenant, call_id)
    return JSONResponse({"ok": True, "call_id": call_id}, status_code=202)


async def list_recordings(request: Request) -> Response:
    tenant = _tenant(request)
    if isinstance(tenant, Response):
        return tenant
    settings: Settings = request.app.state.settings
    rows = request.app.state.store.list_recordings(tenant)
    return JSONResponse(
        {
            "enabled": settings.recordings,
            "retention_days": settings.retention_days,
            "recordings": [
                {"call_id": row["call_id"], "created_at": row["created_at"]} for row in rows
            ],
        }
    )


async def mcp(request: Request) -> Response:
    if request.method != "GET":
        tenant = _tenant(request)
        if isinstance(tenant, Response):
            return tenant
    else:
        tenant = "local"

    async def place(arguments: dict) -> dict:
        try:
            result = await request.app.state.calls.place(
                tenant,
                number=str(arguments.get("number", "")),
                briefing=str(arguments.get("briefing", "")),
                questions=arguments.get("questions"),
                max_minutes=arguments.get("max_minutes"),
            )
        except LimitError as exc:
            return {"ok": False, "error": exc.code}
        return {"ok": True, **result}

    async def get_one(arguments: dict) -> dict:
        row = request.app.state.store.get_call(tenant, str(arguments.get("call_id", "")))
        if row is None:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "call": _public_call(request, row)}

    async def listed(arguments: dict) -> dict:
        limit = _bounded_int(arguments.get("limit"), default=20, upper=100)
        since = _since(arguments.get("since"))
        rows = request.app.state.store.list_calls(tenant, limit=limit, since=since)
        return {"ok": True, "calls": [_public_call(request, row) for row in rows]}

    async def hang(arguments: dict) -> dict:
        call_id = str(arguments.get("call_id", ""))
        if not request.app.state.store.get_call(tenant, call_id):
            return {"ok": False, "error": "not_found"}
        request.app.state.calls.hangup(tenant, call_id)
        return {"ok": True, "call_id": call_id}

    async def connect(arguments: dict) -> dict:
        return await request.app.state.setup.connect_number(
            tenant,
            server=str(arguments.get("voipms_server", "")),
            sip_user=str(arguments.get("sip_user", "")),
            sip_password=str(arguments.get("sip_password", "")),
            did=str(arguments.get("did", "")),
        )

    async def link(arguments: dict) -> dict:
        voice = arguments.get("voice")
        return await request.app.state.setup.link_voice_agent(
            tenant,
            xai_api_key=str(arguments.get("xai_api_key", "")),
            persona_name=str(arguments.get("persona_name", "")),
            instructions=str(arguments.get("instructions", "")),
            voice=str(voice) if voice else None,
        )

    async def setup_status(_arguments: dict) -> dict:
        return {"ok": True, **request.app.state.setup.status(tenant)}

    async def wipe(arguments: dict) -> dict:
        if arguments.get("confirm") is not True:
            return {"ok": False, "error": "confirm_required"}
        return request.app.state.setup.delete_account(tenant)

    async def read_resource(uri: str | None):
        store: Store = request.app.state.store
        if uri is None:
            return [
                {
                    "uri": f"ringabot://calls/{call_id}/transcript",
                    "name": f"Transcript {call_id}",
                    "mimeType": "application/json",
                }
                for call_id in store.recent_call_ids(tenant)
            ]
        prefix = "ringabot://calls/"
        suffix = "/transcript"
        if not (isinstance(uri, str) and uri.startswith(prefix) and uri.endswith(suffix)):
            return None
        call_id = uri[len(prefix) : -len(suffix)]
        row = store.get_call(tenant, call_id)
        if row is None:
            return None
        return json.dumps({"call_id": call_id, "transcript": store.transcript(call_id)})

    return await handle_mcp(
        request,
        {
            "connect_number": connect,
            "link_voice_agent": link,
            "get_setup_status": setup_status,
            "place_call": place,
            "get_call": get_one,
            "list_calls": listed,
            "hang_up": hang,
            "delete_account": wipe,
        },
        read_resource,
    )


async def webhook(request: Request) -> Response:
    if not request.app.state.limiter.allow(_client(request)):
        return JSONResponse(
            {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "30"}
        )
    body = await request.body()
    if len(body) > 65536:
        return JSONResponse({"error": "too_large"}, status_code=413)
    if not request.app.state.verifier.verify(request.headers, body):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "bad_json"}, status_code=400)
    custom = getattr(request.app.state, "webhook_handler", None)
    if custom is not None:
        task = asyncio.create_task(custom(payload))
    else:
        task = asyncio.create_task(_handle_webhook(request.app, payload))
    request.app.state.webhook_jobs.append(task)
    return JSONResponse({"ok": True}, status_code=202)


async def _handle_webhook(app, payload: dict) -> None:
    action = payload.get("action")
    call_id = str(payload.get("call_id", ""))
    if action == "hangup" and call_id:
        app.state.calls.hangup("local", call_id)
    elif action == "note" and call_id and payload.get("text"):
        app.state.store.add_transcript(call_id, "system", str(payload["text"]))


def _tenant(request: Request) -> str | Response:
    if request.url.path != "/health" and not request.app.state.limiter.allow(_client(request)):
        return JSONResponse(
            {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "30"}
        )
    tenant = request.app.state.auth.authenticate(request.headers.get("authorization"))
    if tenant is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return tenant


def _client(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


async def _json(request: Request) -> dict | Response:
    body = await request.body()
    if len(body) > 65536:
        return JSONResponse({"error": "too_large"}, status_code=413)
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "bad_json"}, status_code=400)
    return payload


def _public_call(request: Request, row: dict) -> dict:
    transcript = request.app.state.store.transcript(row["id"])
    summary = None
    if row.get("summary"):
        try:
            summary = json.loads(row["summary"])
        except json.JSONDecodeError:
            summary = {"recap": row["summary"], "answers": []}
    return {
        "call_id": row["id"],
        "destination": row["destination"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "end_reason": row["end_reason"],
        "error": row["error"],
        "max_seconds": row["max_seconds"],
        "transcript": transcript,
        "summary": summary,
    }


def _limit(exc: LimitError) -> JSONResponse:
    status = _HTTP.get(exc.code, 400)
    headers = {"Retry-After": "30"} if status == 429 else {}
    return JSONResponse({"error": exc.code}, status_code=status, headers=headers)


def _bounded_int(value, *, default: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, upper))


def _since(value) -> str | None:
    if value is None or value == "":
        return None
    text = str(value)
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=UTC).isoformat()
    return text


def _rss() -> int | None:
    try:
        import os

        parts = open("/proc/self/statm", encoding="utf-8").read().split()
        return int(parts[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        return None
