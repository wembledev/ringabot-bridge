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
from ringabot.signature import SignatureError, Verifier

log = logging.getLogger("ringabot.app")
MAX_BODY = 64 * 1024


def _bearer_ok(request: Request, token: str) -> bool:
    if not token:
        return False
    got = request.headers.get("authorization", "")
    return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())


def create_app(settings: Settings | None = None, manager: CallManager | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    manager = manager or CallManager(settings)
    verifier = Verifier(settings.webhook_secret, settings.replay_window_seconds)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "active_calls": len(manager.active)})

    async def xai_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        if settings.webhook_bearer and not _bearer_ok(request, settings.webhook_bearer):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
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

    def _auth(request: Request) -> JSONResponse | None:
        if not _bearer_ok(request, settings.api_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def list_calls(request: Request) -> JSONResponse:
        if denied := _auth(request):
            return denied
        return JSONResponse({"calls": list(manager.recent)})

    async def hangup(request: Request) -> JSONResponse:
        if denied := _auth(request):
            return denied
        call_id = request.path_params["call_id"]
        if call_id not in manager.active:
            return JSONResponse({"error": "no such active call"}, status_code=404)
        manager.spawn(manager.active[call_id].hangup("api"))
        return JSONResponse({"ok": True}, status_code=202)

    async def transfer(request: Request) -> JSONResponse:
        """Dial out from an active call (xAI SIP REFER). Target must be configured."""
        if denied := _auth(request):
            return denied
        call_id = request.path_params["call_id"]
        session = manager.active.get(call_id)
        if session is None:
            return JSONResponse({"error": "no such active call"}, status_code=404)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        uri = settings.transfer_targets.get(str(payload.get("target", "")).lower())
        if not uri:
            return JSONResponse({"error": "unknown target"}, status_code=400)
        session.start_transfer(uri)
        return JSONResponse({"ok": True}, status_code=202)

    async def outbound(request: Request) -> JSONResponse:
        if denied := _auth(request):
            return denied
        return JSONResponse(
            {
                "error": "not_supported",
                "detail": "xAI's API has no call-origination endpoint yet. Inbound calls work; "
                "dial-out works mid-call via /calls/{id}/transfer (SIP REFER).",
            },
            status_code=501,
        )

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/xai/webhook", xai_webhook, methods=["POST"]),
            Route("/calls", list_calls),
            Route("/calls/outbound", outbound, methods=["POST"]),
            Route("/calls/{call_id}/hangup", hangup, methods=["POST"]),
            Route("/calls/{call_id}/transfer", transfer, methods=["POST"]),
        ]
    )
