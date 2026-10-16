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


def test_oversized_signature_headers_are_rejected():
    verifier = _verifier()
    body = b"{}"
    headers = verifier.sign(body, timestamp="1700000000")
    headers["X-Ringabot-Timestamp"] = "1" * 40
    assert not verifier.verify(headers, body)
    headers = verifier.sign(body, timestamp="1700000000")
    headers["X-Ringabot-Signature"] = "v1=" + ("ab" * 80)
    assert not verifier.verify(headers, body)


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
async def test_outbound_reuses_client_and_caps_the_response():
    verifier = _verifier(now=time.time)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x" * (70 * 1024))

    client = WebhookClient(
        "http://backend.example/hook",
        verifier,
        timeout=2,
        transport=httpx.MockTransport(handler),
    )
    first = await client.post({"event": "tool"})
    second = await client.post({"event": "tool"})
    assert first["error"] == "response_too_large"
    assert second["error"] == "response_too_large"
    assert calls["n"] == 2
    assert not client._client.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_outbound_blocks_private_targets_at_send_time():
    verifier = _verifier(now=time.time)
    hit = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        hit["n"] += 1
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)

    async def resolver(_host: str, _port: int) -> list[str]:
        return ["203.0.113.10"] if hit["n"] == 0 else ["169.254.169.254"]

    client = WebhookClient(
        "https://hooks.example/tool",
        verifier,
        timeout=2,
        transport=transport,
        resolver=resolver,
    )
    # The first answer is documentation space (203.0.113.0/24), which is not public.
    blocked = await client.post({"event": "tool"})
    assert blocked["error"] == "blocked_url"
    assert hit["n"] == 0

    async def public_then_private(host: str, port: int) -> list[str]:
        public_then_private.calls += 1
        if public_then_private.calls == 1:
            return ["1.1.1.1"]
        return ["10.0.0.8"]

    public_then_private.calls = 0
    seen: list[str] = []

    def pin_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        assert request.headers["host"] == "hooks.example"
        assert request.extensions.get("sni_hostname") == "hooks.example"
        return httpx.Response(204)

    pinned = WebhookClient(
        "https://hooks.example/tool",
        verifier,
        timeout=2,
        transport=httpx.MockTransport(pin_handler),
        resolver=public_then_private,
    )
    ok = await pinned.post({"event": "tool"})
    assert ok["ok"] is True
    assert seen == ["1.1.1.1"]
    rebound = await pinned.post({"event": "tool"})
    assert rebound["error"] == "blocked_url"
    assert seen == ["1.1.1.1"]

    literal = WebhookClient(
        "http://127.0.0.1/hook",
        verifier,
        timeout=2,
        transport=transport,
    )
    assert (await literal.post({"event": "tool"}))["error"] == "blocked_url"
    metadata = WebhookClient(
        "http://169.254.169.254/latest/meta-data",
        verifier,
        timeout=2,
        transport=transport,
    )
    assert (await metadata.post({"event": "tool"}))["error"] == "blocked_url"
    await client.aclose()
    await pinned.aclose()
    await literal.aclose()
    await metadata.aclose()


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
