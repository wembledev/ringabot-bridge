import json
import time

import httpx
import pytest

from ringabot.webhooks import WebhookClient, WebhookVerifier


def _verifier(now=None):
    return WebhookVerifier(
        secret="placeholder-hook-secret",
        bearer_token="placeholder-token-value",
        window_seconds=300,
        now=now or (lambda: 1_700_000_000),
    )


def test_hmac_accepts_fresh_signature_and_rejects_tamper_and_replay():
    verifier = _verifier()
    body = b'{"event":"tool","note":"ok"}'
    headers = verifier.sign(body, timestamp="1700000000")
    assert verifier.verify(headers, body)
    assert not verifier.verify(headers, body)
    tampered = verifier.sign(b'{"event":"tool","note":"ok"}', timestamp="1700000001")
    assert not verifier.verify(tampered, b'{"event":"tool","note":"changed"}')


def test_timestamp_window():
    verifier = _verifier()
    body = b"{}"
    old = verifier.sign(body, timestamp="1699999000")
    assert not verifier.verify(old, body)
    future = verifier.sign(body, timestamp="1700000401")
    assert not verifier.verify(future, body)


def test_bearer_or_hmac():
    verifier = _verifier()
    body = b"{}"
    assert verifier.verify({"Authorization": "Bearer placeholder-token-value"}, body)
    assert not verifier.verify({"Authorization": "Bearer placeholder-token-valu!"}, body)
    assert not verifier.verify({}, body)


@pytest.mark.asyncio
async def test_outbound_client_is_verifiable_and_times_out_fast():
    verifier = _verifier(now=time.time)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["ok"] = verifier.verify(request.headers, request.content)
        return httpx.Response(204)

    client = WebhookClient(
        "http://backend.example/hook",
        verifier,
        timeout=2,
        transport=httpx.MockTransport(handler),
    )
    result = await client.post({"event": "tool", "note": "picnic"})
    assert result["ok"] is True
    assert seen["ok"] is True
    assert b"placeholder-hook-secret" not in seen["body"]

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    slow_client = WebhookClient(
        "http://backend.example/hook",
        verifier,
        timeout=0.2,
        transport=httpx.MockTransport(slow),
    )
    started = time.monotonic()
    failed = await slow_client.post({"event": "tool"})
    assert failed["error"] == "timeout"
    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_inbound_webhook_returns_before_slow_work(app, auth_header):
    release = __import__("asyncio").Event()
    started = __import__("asyncio").Event()

    async def slow(payload):
        started.set()
        await release.wait()

    app.state.webhook_handler = slow
    verifier = app.state.verifier
    body = json.dumps({"action": "note", "call_id": "call_x", "text": "later"}).encode()
    headers = verifier.sign(body)
    client = app.state.client
    started_at = time.monotonic()
    response = await client.post("/webhook", content=body, headers=headers)
    elapsed = time.monotonic() - started_at
    assert response.status_code == 202
    assert elapsed < 0.3
    await started.wait()
    release.set()
    await app.state.webhook_jobs[-1]


@pytest.mark.asyncio
async def test_inbound_replay_and_bearer(app):
    client = app.state.client
    verifier = app.state.verifier
    body = b'{"action":"note","call_id":"missing","text":"hi"}'
    headers = verifier.sign(body)
    first = await client.post("/webhook", content=body, headers=headers)
    second = await client.post("/webhook", content=body, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 401
    bearer = await client.post(
        "/webhook",
        content=body,
        headers={"authorization": "Bearer placeholder-token-value"},
    )
    assert bearer.status_code == 202
    rejected = await client.post("/webhook", content=body)
    assert rejected.status_code == 401
