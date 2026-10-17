import asyncio
import json
import time

import pytest

import ringabot.calls as calls
from conftest import FakeXai
from ringabot.calls import CallManager, InboundCall, OutboundCall


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class FakeSip:
    def __init__(self):
        self.commands: list[str] = []

    async def command(self, line):
        self.commands.append(line)


def _record():
    return {"call_id": "c1", "started_at": time.time(), "transcript": [], "tools": []}


def _inbound(settings):
    manager = CallManager(settings, xai=FakeXai())
    call = InboundCall(manager, "c1", _record())
    call.ws = FakeWS()
    return manager, call


async def _drain(manager):
    for _ in range(5):
        await asyncio.sleep(0)
        if manager._tasks:
            await asyncio.wait(list(manager._tasks), timeout=0.2)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(calls, "BACKSTOP_SECONDS", 5.0)
    monkeypatch.setattr(InboundCall, "tail_seconds", 0.01)
    monkeypatch.setattr(OutboundCall, "tail_seconds", 0.01)


async def test_transcripts_recorded(settings):
    _, call = _inbound(settings)
    await call.on_event(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hi"}
    )
    await call.on_event({"type": "response.output_audio_transcript.done", "transcript": "hello"})
    assert call.record["transcript"] == [
        {"role": "caller", "text": "hi"},
        {"role": "agent", "text": "hello"},
    ]


async def test_end_call_replies_then_hangs_up_once_after_goodbye(settings):
    manager, call = _inbound(settings)
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "fc1", "name": "end_call"}
    )
    kinds = [m["type"] for m in call.ws.sent]
    assert kinds == ["conversation.item.create", "response.create"]
    assert json.loads(call.ws.sent[0]["item"]["output"]) == {"status": "hanging up"}
    # A response.done from before the goodbye must not hang up yet.
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert manager.xai.hangups == []
    await call.on_event({"type": "response.created"})
    await call.on_event({"type": "response.done"})
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert manager.xai.hangups == ["c1"]
    await call.hangup("api")
    assert manager.xai.hangups == ["c1"]
    assert call.record["end_reason"] == "end_call"


async def test_backstop_hangs_up_if_goodbye_never_finishes(settings, monkeypatch):
    monkeypatch.setattr(calls, "BACKSTOP_SECONDS", 0.01)
    manager, call = _inbound(settings)
    call.arm("end_call")
    await _drain(manager)
    assert manager.xai.hangups == ["c1"]


async def test_idle_timeout_says_goodbye_and_arms(settings, monkeypatch):
    manager, call = _inbound(settings)
    call.last_activity -= settings.idle_seconds + 1
    real_sleep = asyncio.sleep
    monkeypatch.setattr(calls.asyncio, "sleep", lambda _d: real_sleep(0))
    task = asyncio.create_task(call.watchdog())
    await real_sleep(0.01)
    call._hung_up = True
    await asyncio.wait([task], timeout=1)
    assert call.armed and call.record["end_reason"] == "idle"
    assert call.ws.sent[0]["type"] == "response.create"


async def test_unknown_tool_gets_error(settings):
    _, call = _inbound(settings)
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "f", "name": "x"}
    )
    assert "error" in json.loads(call.ws.sent[0]["item"]["output"])
    assert not call.armed


async def test_outbound_end_call_hangs_up_sip_leg(settings):
    manager = CallManager(settings, xai=FakeXai())
    call = OutboundCall(manager, "out_1", _record(), "+16045550111", "", "Hi")
    call.ws = FakeWS()
    call.sip = FakeSip()
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "f", "name": "end_call"}
    )
    await call.on_event({"type": "response.created"})
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert call.sip.commands == ["hangup"]
    assert manager.xai.hangups == []
