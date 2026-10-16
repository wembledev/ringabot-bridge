import asyncio
import json

import httpx
import pytest
from tests.harness import FakeGrokServer

from ringabot.api import create_app
from ringabot.config import load_settings
from ringabot.persona import DISCLOSURE
from ringabot.webhooks import WebhookClient, WebhookVerifier


async def _wait_status(store, call_id, statuses, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            row = store.get_call("local", call_id)
            if row and row["status"] in statuses:
                return row
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_rate_limit_and_open_health(app):
    client = app.state.client
    headers = {"authorization": "Bearer placeholder-token-value"}
    app.state.limiter.per_minute = 2
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/status", headers=headers)).status_code == 200
    assert (await client.get("/status", headers=headers)).status_code == 200
    limited = await client.get("/status", headers=headers)
    assert limited.status_code == 429
    assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_health_is_open_and_status_requires_bearer(app):
    client = app.state.client
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"ok": True}
    denied = await client.get("/status")
    assert denied.status_code == 401
    ok = await client.get("/status", headers={"authorization": "Bearer placeholder-token-value"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["inbound"] == "unsupported"
    assert "placeholder-sip-pass" not in ok.text
    assert "placeholder-xai-key" not in ok.text


@pytest.mark.asyncio
async def test_place_call_returns_before_the_call_finishes(app):
    client = app.state.client
    headers = {"authorization": "Bearer placeholder-token-value"}
    started = asyncio.get_running_loop().time()
    response = await client.post(
        "/call",
        headers=headers,
        json={
            "number": "+1-555-555-0100",
            "briefing": "Confirm the picnic time.",
            "questions": ["When?"],
        },
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 202
    assert elapsed < 1
    call_id = response.json()["call_id"]
    await _wait_status(app.state.store, call_id, {"in_progress"})
    audio = app.state.calls.audios[call_id]
    await audio.push_remote(b"\x01\x00" * 80)
    await app.state.calls.tasks[call_id]
    row = app.state.store.get_call("local", call_id)
    assert row["status"] == "completed"
    assert row["end_reason"] == "idle"
    transcript = app.state.store.transcript(call_id)
    text = " ".join(item["text"] for item in transcript)
    assert DISCLOSURE in text
    assert "Yes, noon works." in text
    summary = json.loads(row["summary"])
    assert summary["recap"] == "Called and confirmed."
    assert summary["answers"][0]["answer"] == "Noon"
    fetched = await client.get(f"/calls/{call_id}", headers=headers)
    assert fetched.json()["summary"]["recap"] == "Called and confirmed."
    listed = await client.get("/calls", headers=headers)
    assert listed.json()["calls"][0]["call_id"] == call_id


@pytest.mark.asyncio
async def test_disclosure_on_the_wire(app, fakes):
    _sip, grok = fakes
    client = app.state.client
    response = await client.post(
        "/call",
        headers={"authorization": "Bearer placeholder-token-value"},
        json={"number": "+15555550102", "briefing": "Confirm the picnic time."},
    )
    call_id = response.json()["call_id"]
    await app.state.calls.tasks[call_id]
    updates = [event for event in grok.received if event.get("type") == "session.update"]
    assert updates
    instructions = updates[0]["session"]["instructions"]
    assert DISCLOSURE in instructions
    assert "Confirm the picnic time." in instructions
    assert "placeholder-sip-pass" not in instructions
    assert updates[0]["session"]["turn_detection"]["type"] == "server_vad"
    assert updates[0]["session"]["voice"] == "aurora"
    forced = [
        event
        for event in grok.received
        if event.get("type") == "conversation.item.create"
        and (event.get("item") or {}).get("type") == "force_message"
    ]
    spoken = forced[0]["item"]["content"][0]["text"]
    assert spoken.startswith(DISCLOSURE)
    assert "I have a short question." in spoken
    assert any(item["text"] == "Goodbye." for item in app.state.store.transcript(call_id))


@pytest.mark.asyncio
async def test_hangup_and_limits(app, monkeypatch):
    client = app.state.client
    headers = {"authorization": "Bearer placeholder-token-value"}
    denied = await client.post(
        "/call",
        headers=headers,
        json={"number": "+442075550100", "briefing": "Nope."},
    )
    assert denied.status_code == 400
    assert denied.json()["error"] == "destination_not_allowed"
    emergency = await client.post(
        "/call",
        headers=headers,
        json={"number": "+1911", "briefing": "Nope."},
    )
    assert emergency.json()["error"] == "emergency_not_allowed"
    questions = await client.post(
        "/call",
        headers=headers,
        json={"number": "+15555550100", "briefing": "Nope.", "questions": "When?"},
    )
    assert questions.status_code == 400
    assert questions.json()["error"] == "questions_invalid"
    first = await client.post(
        "/call",
        headers=headers,
        json={"number": "+15555550103", "briefing": "Hold."},
    )
    call_id = first.json()["call_id"]
    await _wait_status(app.state.store, call_id, {"in_progress"})
    second = await client.post(
        "/call",
        headers=headers,
        json={"number": "+15555550104", "briefing": "Also."},
    )
    assert second.status_code == 409
    assert second.json()["error"] == "tenant_busy"
    hang = await client.post(f"/calls/{call_id}/hangup", headers=headers)
    assert hang.status_code == 202
    await app.state.calls.tasks[call_id]
    assert app.state.store.get_call("local", call_id)["end_reason"] == "hangup"


@pytest.mark.asyncio
async def test_daily_cap(settings, store, fakes, monkeypatch):
    sip, grok = fakes
    monkeypatch.setenv("SIP_CTRL", f"127.0.0.1:{sip.port}")
    monkeypatch.setenv("XAI_REALTIME_URL", grok.url)
    monkeypatch.setenv("MAX_CALLS_PER_DAY", "1")
    monkeypatch.setenv("IDLE_TIMEOUT", "0.2")
    live = load_settings()
    application = create_app(live, store)
    transport = httpx.ASGITransport(app=application)
    headers = {"authorization": "Bearer placeholder-token-value"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/call", headers=headers, json={"number": "+15555550105", "briefing": "One."}
        )
        assert first.status_code == 202
        await application.state.calls.tasks[first.json()["call_id"]]
        second = await client.post(
            "/call", headers=headers, json={"number": "+15555550106", "briefing": "Two."}
        )
        assert second.status_code == 429
        assert second.json()["error"] == "daily_cap"


@pytest.mark.asyncio
async def test_mcp_tools_and_resources(app):
    client = app.state.client
    headers = {
        "authorization": "Bearer placeholder-token-value",
        "content-type": "application/json",
    }
    init = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
        },
    )
    assert init.status_code == 200
    assert init.json()["result"]["serverInfo"]["name"] == "ringabot-bridge"
    listed = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert "place_call" in names
    assert "hang_up" in names
    assert "connect_number" in names
    assert "set_inbound" not in names
    note = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert note.status_code == 202
    missing = await client.get("/mcp")
    assert missing.status_code == 405
    placed = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "place_call",
                "arguments": {"number": "+15555550107", "briefing": "MCP picnic."},
            },
        },
    )
    payload = json.loads(placed.json()["result"]["content"][0]["text"])
    assert payload["ok"] is True
    call_id = payload["call_id"]
    await app.state.calls.tasks[call_id]
    got = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_call", "arguments": {"call_id": call_id}},
        },
    )
    call = json.loads(got.json()["result"]["content"][0]["text"])["call"]
    assert DISCLOSURE in " ".join(line["text"] for line in call["transcript"])
    resources = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    )
    uri = resources.json()["result"]["resources"][0]["uri"]
    read = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert call_id in read.json()["result"]["contents"][0]["text"]


@pytest.mark.asyncio
async def test_connect_number_does_not_echo_the_password(app, fakes):
    client = app.state.client
    headers = {"authorization": "Bearer placeholder-token-value"}
    response = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "connect_number",
                "arguments": {
                    "voipms_server": "sip.example.com",
                    "sip_user": "example-user",
                    "sip_password": "placeholder-sip-pass",
                    "did": "+15555550100",
                },
            },
        },
    )
    text = response.text
    assert "placeholder-sip-pass" not in text
    body = json.loads(response.json()["result"]["content"][0]["text"])
    assert body["registered"] is True
    assert "register" in fakes[0].commands
    row = app.state.store._conn.execute("SELECT ciphertext FROM secrets").fetchone()
    assert b"placeholder-sip-pass" not in row["ciphertext"]


@pytest.mark.asyncio
async def test_link_voice_agent_rejects_before_store(app, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer placeholder-xai-key"
        return httpx.Response(401)

    application = app
    from ringabot.probes import validate_xai_key

    async def probe(key: str):
        return await validate_xai_key(
            "https://api.example",
            key,
            transport=httpx.MockTransport(handler),
        )

    application.state.setup.key_probe = probe
    response = await application.state.client.post(
        "/mcp",
        headers={"authorization": "Bearer placeholder-token-value"},
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "link_voice_agent",
                "arguments": {
                    "xai_api_key": "placeholder-xai-key",
                    "persona_name": "Example Assistant",
                    "instructions": "Be brief.",
                },
            },
        },
    )
    body = json.loads(response.json()["result"]["content"][0]["text"])
    assert body["ok"] is False
    assert application.state.store.get_secret("local", "xai_api_key") is None


@pytest.mark.asyncio
async def test_tool_webhook_during_call(settings, store, monkeypatch):
    from tests.harness import FakeSipServer

    sip = FakeSipServer()
    grok = FakeGrokServer(emit_tool=True)
    await sip.start()
    await grok.start()
    try:
        monkeypatch.setenv("SIP_CTRL", f"127.0.0.1:{sip.port}")
        monkeypatch.setenv("XAI_REALTIME_URL", grok.url)
        monkeypatch.setenv("IDLE_TIMEOUT", "0.3")
        monkeypatch.setenv("TOOLS_WEBHOOK_URL", "http://backend.example/hook")
        monkeypatch.setenv("TOOLS_WEBHOOK_SECRET", "placeholder-hook-secret")
        live = load_settings()
        verifier = WebhookVerifier(
            secret=live.tools_webhook_secret,
            bearer_token=live.api_token,
        )
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.content, verifier.verify(request.headers, request.content)))
            return httpx.Response(200, json={"ok": True})

        webhook = WebhookClient(
            live.tools_webhook_url,
            verifier,
            timeout=2,
            transport=httpx.MockTransport(handler),
        )
        application = create_app(live, store, webhook_client=webhook)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/call",
                headers={"authorization": "Bearer placeholder-token-value"},
                json={"number": "+15555550108", "briefing": "Collect a note."},
            )
            call_id = response.json()["call_id"]
            await application.state.calls.tasks[call_id]
        assert seen
        assert seen[0][1] is True
        assert b"placeholder-sip-pass" not in seen[0][0]
        assert b"placeholder-xai-key" not in seen[0][0]
        assert b"notify_backend" in seen[0][0]
    finally:
        await grok.stop()
        await sip.stop()


@pytest.mark.asyncio
async def test_delete_account_wipes_rows(app):
    client = app.state.client
    headers = {"authorization": "Bearer placeholder-token-value"}
    await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "connect_number",
                "arguments": {
                    "voipms_server": "sip.example.com",
                    "sip_user": "example-user",
                    "sip_password": "placeholder-sip-pass",
                    "did": "+15555550100",
                },
            },
        },
    )
    denied = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "delete_account", "arguments": {"confirm": False}},
        },
    )
    assert json.loads(denied.json()["result"]["content"][0]["text"])["error"] == "confirm_required"
    wiped = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "delete_account", "arguments": {"confirm": True}},
        },
    )
    assert json.loads(wiped.json()["result"]["content"][0]["text"])["ok"] is True
    assert app.state.store.get_secret("local", "sip_password") is None
