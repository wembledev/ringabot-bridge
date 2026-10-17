import asyncio
import json

import httpx
import pytest

from ringabot.api import create_app
from ringabot.config import load_settings
from ringabot.inbound import caller_number
from ringabot.persona import DISCLOSURE

HEADERS = {
    "authorization": "Bearer placeholder-token-value",
    "content-type": "application/json",
}


def _rpc(name, arguments, msg_id=1):
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


async def _tool(client, name, arguments, msg_id=1):
    response = await client.post("/mcp", headers=HEADERS, json=_rpc(name, arguments, msg_id))
    body = response.json()
    return json.loads(body["result"]["content"][0]["text"])


async def _wait_registered(inbound):
    async with asyncio.timeout(3):
        while not inbound.registered:
            await asyncio.sleep(0.02)


async def _wait_inbound(store, statuses):
    async with asyncio.timeout(5):
        while True:
            for row in store.list_calls("local", limit=10, since=None):
                if row["direction"] == "inbound" and row["status"] in statuses:
                    return row
            await asyncio.sleep(0.02)


def test_caller_number_reads_sip_uri():
    event = {"type": "CALL_INCOMING", "peeruri": "sip:15555550123@sip.example.com"}
    assert caller_number(event) == "+15555550123"
    assert caller_number({"peeruri": "sip:anonymous@sip.example.com"}) == ""
    assert caller_number({"param": "sip:+15555550124@sip.example.com"}) == "+15555550124"


@pytest.mark.asyncio
async def test_set_inbound_reports_status_and_rejects_bad_allow(app):
    client = app.state.client
    bad = await _tool(client, "set_inbound", {"enabled": True, "allow": ["911"]})
    assert bad["ok"] is False
    turned_on = await _tool(
        client,
        "set_inbound",
        {
            "enabled": True,
            "greeting": "Thanks for calling.",
            "instructions": "Help the caller with the picnic.",
            "allow": ["+1 555-555-0123"],
        },
        msg_id=2,
    )
    assert turned_on["ok"] is True
    assert turned_on["enabled"] is True
    assert turned_on["greeting"] == "Thanks for calling."
    assert turned_on["allow"] == ["+15555550123"]
    await _wait_registered(app.state.inbound)
    status = await _tool(client, "get_setup_status", {}, msg_id=3)
    assert status["ok"] is True
    assert status["inbound"]["enabled"] is True
    assert status["inbound"]["registered"] is True
    assert status["inbound"]["did"] == "+15555550100"
    assert "Help the caller with the picnic." in status["inbound"]["instructions"]
    off = await _tool(client, "set_inbound", {"enabled": False}, msg_id=4)
    assert off["enabled"] is False
    assert app.state.inbound.registered is False


@pytest.mark.asyncio
async def test_pending_registration_does_not_restart_baresip(app, fakes, monkeypatch):
    async def pending(_client, _timeout):
        return "pending"

    fakes[0].auto_register = False
    monkeypatch.setattr("ringabot.inbound.confirm_registration", pending)
    await _tool(app.state.client, "set_inbound", {"enabled": True})
    async with asyncio.timeout(2):
        while not app.state.inbound.waiting:
            await asyncio.sleep(0.02)
    await asyncio.sleep(0.6)
    try:
        registers = [item for item in fakes[0].commands if item == "register"]
        assert registers == ["register"]
        assert app.state.inbound.registered is False
    finally:
        await app.state.inbound.stop()


@pytest.mark.asyncio
async def test_inbound_call_lifecycle(app, fakes):
    client = app.state.client
    seen = []

    async def spy(payload):
        seen.append(payload)
        return {"ok": True}

    app.state.calls.webhooks.post = spy
    try:
        await _tool(
            client,
            "set_inbound",
            {
                "enabled": True,
                "greeting": "Thanks for calling.",
                "instructions": "Help the caller with the picnic.",
                "allow": [],
            },
        )
        await _wait_registered(app.state.inbound)
        await fakes[0].push_incoming("+15555550123")
        row = await _wait_inbound(app.state.store, {"in_progress"})
        call_id = row["id"]
        audio = app.state.calls.audios[call_id]
        await audio.push_remote(b"\x01\x00" * 80)
        await app.state.calls.tasks[call_id]
        saved = app.state.store.get_call("local", call_id)
        assert saved["status"] == "completed"
        assert saved["direction"] == "inbound"
        assert saved["destination"] == "+15555550123"
        assert saved["end_reason"] == "idle"
        transcript = app.state.store.transcript(call_id)
        text = " ".join(item["text"] for item in transcript)
        assert transcript[0]["text"].startswith(DISCLOSURE)
        assert "Thanks for calling." in transcript[0]["text"]
        assert "Yes, noon works." in text
        assert any(item["text"] == "Goodbye." for item in transcript)
        summary = json.loads(saved["summary"])
        assert summary["recap"] == "Called and confirmed."
        updates = [event for event in fakes[1].received if event.get("type") == "session.update"]
        instructions = updates[0]["session"]["instructions"]
        assert DISCLOSURE in instructions
        assert "Help the caller with the picnic." in instructions
        assert "incoming call" in instructions
        completed = [item for item in seen if item.get("event") == "call.completed"]
        assert completed
        assert completed[0]["call_id"] == call_id
        assert completed[0]["direction"] == "inbound"
        assert completed[0]["destination"] == "+15555550123"
        fetched = await client.get(f"/calls/{call_id}", headers=HEADERS)
        assert fetched.json()["direction"] == "inbound"
        assert app.state.store.calls_today("local") == 1
    finally:
        await app.state.inbound.stop()


@pytest.mark.asyncio
async def test_allow_list_hangs_up_other_callers(app, fakes):
    client = app.state.client
    try:
        await _tool(
            client,
            "set_inbound",
            {"enabled": True, "allow": ["+15555550111"], "greeting": "You are on the list."},
        )
        await _wait_registered(app.state.inbound)
        await fakes[0].push_incoming("+15555550999")
        async with asyncio.timeout(2):
            while "hangup" not in fakes[0].commands or not app.state.inbound.waiting:
                await asyncio.sleep(0.02)
        assert app.state.store.list_calls("local", limit=10, since=None) == []
        assert fakes[1].received == []
        assert app.state.store.calls_today("local") == 0
        await fakes[0].push_incoming("+15555550111")
        row = await _wait_inbound(app.state.store, {"in_progress"})
        await app.state.calls.audios[row["id"]].push_remote(b"\x01\x00" * 80)
        await app.state.calls.tasks[row["id"]]
        assert app.state.store.get_call("local", row["id"])["destination"] == "+15555550111"
    finally:
        await app.state.inbound.stop()


@pytest.mark.asyncio
async def test_inbound_busy_hangs_up_without_a_second_session(settings, store, fakes, monkeypatch):
    sip, grok = fakes
    monkeypatch.setenv("SIP_CTRL", f"127.0.0.1:{sip.port}")
    monkeypatch.setenv("XAI_REALTIME_URL", grok.url)
    monkeypatch.setenv("IDLE_TIMEOUT", "5")
    monkeypatch.setenv("MAX_CALL_SECONDS", "60")
    live = load_settings()
    application = create_app(live, store)
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _tool(client, "set_inbound", {"enabled": True, "allow": []})
            await _wait_registered(application.state.inbound)
            outbound = await client.post(
                "/call",
                headers=HEADERS,
                json={"number": "+15555550108", "briefing": "Hold the line."},
            )
            assert outbound.status_code == 202
            outbound_id = outbound.json()["call_id"]
            async with asyncio.timeout(5):
                while store.get_call("local", outbound_id)["status"] != "in_progress":
                    await asyncio.sleep(0.02)
            before = len(sip.commands)
            await sip.push_incoming("+15555550123")
            async with asyncio.timeout(2):
                while True:
                    hung_up = sip.commands[before:].count("hangup") >= 1
                    if hung_up and application.state.inbound.waiting:
                        break
                    await asyncio.sleep(0.02)
            assert store.get_call("local", outbound_id)["status"] == "in_progress"
            assert [
                row
                for row in store.list_calls("local", limit=10, since=None)
                if row["direction"] == "inbound"
            ] == []
            application.state.calls.hangup("local", outbound_id)
            await application.state.calls.tasks[outbound_id]
    finally:
        await application.state.inbound.stop()
        for task in list(application.state.calls.tasks.values()):
            task.cancel()


@pytest.mark.asyncio
async def test_inbound_counts_toward_the_daily_cap(settings, store, fakes, monkeypatch):
    sip, grok = fakes
    monkeypatch.setenv("SIP_CTRL", f"127.0.0.1:{sip.port}")
    monkeypatch.setenv("XAI_REALTIME_URL", grok.url)
    monkeypatch.setenv("IDLE_TIMEOUT", "0.3")
    monkeypatch.setenv("MAX_CALLS_PER_DAY", "1")
    live = load_settings()
    application = create_app(live, store)
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _tool(client, "set_inbound", {"enabled": True})
            await _wait_registered(application.state.inbound)
            await sip.push_incoming("+15555550124")
            row = await _wait_inbound(store, {"in_progress"})
            await application.state.calls.audios[row["id"]].push_remote(b"\x01\x00" * 80)
            await application.state.calls.tasks[row["id"]]
            assert store.calls_today("local") == 1
            async with asyncio.timeout(2):
                while not application.state.inbound.waiting:
                    await asyncio.sleep(0.02)
            before = len(sip.commands)
            await sip.push_incoming("+15555550125")
            async with asyncio.timeout(2):
                while sip.commands[before:].count("hangup") < 1:
                    await asyncio.sleep(0.02)
            assert store.calls_today("local") == 1
            assert len(store.list_calls("local", limit=10, since=None)) == 1
    finally:
        await application.state.inbound.stop()
