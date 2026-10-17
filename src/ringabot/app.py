"""HTTP surface. Webhooks return in milliseconds; calls run as background tasks."""

from __future__ import annotations

import hmac
import json
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from ringabot.billing import (
    Billing,
    BillingError,
    LoggingProvisioner,
    apply_stripe_event,
    parse_stripe_event,
)
from ringabot.calls import CallManager
from ringabot.config import Settings, guard_live_charges
from ringabot.mcp import make_endpoint
from ringabot.signature import SignatureError, Verifier
from ringabot.store import Admin, Store

log = logging.getLogger("ringabot.app")
MAX_BODY = 64 * 1024


def _bearer_token(request: Request) -> str | None:
    scheme, _, got = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    got = got.strip()
    return got or None


def _bearer_ok(request: Request, token: str) -> bool:
    got = _bearer_token(request)
    if not token or got is None:
        return False
    return hmac.compare_digest(got.encode(), token.encode())


def create_app(
    settings: Settings | None = None,
    manager: CallManager | None = None,
    store: Store | None = None,
    provisioner=None,
) -> Starlette:
    settings = settings or Settings.from_env()
    guard_live_charges(settings.stripe_secret_key, settings.live_charges)
    manager = manager or CallManager(settings)
    store = store or Store(settings.db_path)
    provisioner = provisioner or LoggingProvisioner()
    billing = Billing(settings, store, provisioner)
    verifier = Verifier(settings.webhook_secret)

    def identify(request: Request):
        token = _bearer_token(request)
        if token is None:
            return None
        if settings.api_token and hmac.compare_digest(token.encode(), settings.api_token.encode()):
            return Admin()
        return store.account_by_token(token)

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

    async def signup(_request: Request) -> JSONResponse:
        _account, token = store.create_account()
        return JSONResponse({"account_id": _account.id, "api_token": token}, status_code=201)

    async def stripe_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            event = parse_stripe_event(
                body,
                request.headers.get("stripe-signature", ""),
                settings.stripe_webhook_secret,
            )
            result = apply_stripe_event(store, provisioner, event)
        except BillingError as exc:
            log.warning("stripe webhook rejected: %s", exc)
            text = str(exc)
            status = 400 if ("signature" in text or text == "bad json") else 500
            return JSONResponse({"error": text}, status_code=status)
        return JSONResponse({"ok": True, "result": result})

    def billing_success(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "Payment received. The number activates when Stripe confirms it.\n"
        )

    def billing_cancel(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("Checkout cancelled. No charge.\n")

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/xai/webhook", xai_webhook, methods=["POST"]),
            Route("/call", place_call, methods=["POST"]),
            Route("/calls", list_calls),
            Route("/calls/{call_id}/hangup", hangup, methods=["POST"]),
            Route("/signup", signup, methods=["POST"]),
            Route("/stripe/webhook", stripe_webhook, methods=["POST"]),
            Route("/billing/success", billing_success),
            Route("/billing/cancel", billing_cancel),
            Route("/mcp", make_endpoint(manager, identify, store, billing), methods=["POST"]),
        ]
    )
