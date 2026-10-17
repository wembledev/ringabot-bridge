"""HTTP surface. Webhooks return in milliseconds; calls run as background tasks."""

from __future__ import annotations

import hmac
import json
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ringabot.calls import CallManager
from ringabot.config import Settings
from ringabot.mcp import make_endpoint
from ringabot.signature import SignatureError, Verifier

log = logging.getLogger("ringabot.app")
MAX_BODY = 64 * 1024


def _bearer_ok(request: Request, token: str) -> bool:
    if not token:
        return False
    scheme, _, got = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(got.strip().encode(), token.encode())


def create_app(settings: Settings | None = None, manager: CallManager | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    manager = manager or CallManager(settings)
    verifier = Verifier(settings.webhook_secret)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def xai_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            verifier.verify(request.headers, body)
        except SignatureError as exc:
            log.warning("rejected webhook: %s", exc)
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        etype = event.get("type", "")
        if etype == "realtime.call.incoming":
            ok, why = manager.accept(event.get("data") or {})
            return JSONResponse({"accepted": ok, "detail": why})
        log.info("ignored webhook type %s", etype[:64])
        return JSONResponse({"ignored": etype[:64]})

    def _denied(request: Request) -> JSONResponse | None:
        if not _bearer_ok(request, settings.api_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def place_call(request: Request) -> JSONResponse:
        if denied := _denied(request):
            return denied
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad json"}, status_code=400)
        call_id, detail = manager.place(
            str(payload.get("number", "")),
            str(payload.get("briefing", "")),
        )
        if call_id is None:
            return JSONResponse({"error": detail}, status_code=400)
        return JSONResponse({"call_id": call_id, "status": detail}, status_code=202)

    async def list_calls(request: Request) -> JSONResponse:
        if denied := _denied(request):
            return denied
        return JSONResponse({"calls": list(manager.recent)})

    async def hangup(request: Request) -> JSONResponse:
        if denied := _denied(request):
            return denied
        call = manager.active.get(request.path_params["call_id"])
        if call is None:
            return JSONResponse({"error": "no such active call"}, status_code=404)
        manager.spawn(call.hangup("api"))
        return JSONResponse({"ok": True}, status_code=202)

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/xai/webhook", xai_webhook, methods=["POST"]),
            Route("/call", place_call, methods=["POST"]),
            Route("/calls", list_calls),
            Route("/calls/{call_id}/hangup", hangup, methods=["POST"]),
            Route(
                "/mcp",
                make_endpoint(manager, lambda r: _bearer_ok(r, settings.api_token)),
                methods=["POST"],
            ),
        ]
    )
