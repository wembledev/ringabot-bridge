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


async def test_end_call_is_silent_and_hangs_up_once_after_goodbye(settings):
    manager, call = _inbound(settings)
    await call.on_event({"type": "response.created"})
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "fc1", "name": "end_call"}
    )
    # Only the tool output, no new response, so the model has nothing to say.
    assert [m["type"] for m in call.ws.sent] == ["conversation.item.create"]
    assert json.loads(call.ws.sent[0]["item"]["output"]) == {"ok": True}
    await call.on_event({"type": "response.done"})
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert manager.xai.hangups == ["c1"]
    await call.hangup("api")
    assert manager.xai.hangups == ["c1"]
    assert call.record["end_reason"] == "end_call"


async def test_hangup_waits_for_goodbye_playback(settings):
    manager, call = _inbound(settings)
    call.arm("end_call")
    call.play_until = time.monotonic() + 0.2
    await call.on_event({"type": "response.done"})
    await asyncio.sleep(0.05)
    assert manager.xai.hangups == []
    await _drain(manager)
    await asyncio.sleep(0.25)
    assert manager.xai.hangups == ["c1"]


async def test_idle_goodbye_waits_for_its_own_response(settings):
    manager, call = _inbound(settings)
    await call.say_goodbye("idle")
    await call.on_event({"type": "response.done"})  # an older response finishing
    await _drain(manager)
    assert manager.xai.hangups == []
    await call.on_event({"type": "response.created"})
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert manager.xai.hangups == ["c1"]


async def test_caller_transcript_deduped_by_item(settings):
    _, call = _inbound(settings)
    ev = {"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1"}
    for _ in range(3):
        await call.on_event({**ev, "transcript": "That's weird."})
    await call.on_event({**ev, "item_id": "i2", "transcript": "Okay."})
    assert [x["text"] for x in call.record["transcript"]] == ["That's weird.", "Okay."]


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
    assert call.ws.sent[1]["type"] == "response.create"
    assert not call.armed


async def test_outbound_end_call_hangs_up_sip_leg(settings):
    manager = CallManager(settings, xai=FakeXai())
    call = OutboundCall(manager, "out_1", _record(), "+16045550111", "")
    call.ws = FakeWS()
    call.sip = FakeSip()
    await call.on_event(
        {"type": "response.function_call_arguments.done", "call_id": "f", "name": "end_call"}
    )
    await call.on_event({"type": "response.done"})
    await _drain(manager)
    assert call.sip.commands == ["hangup"]
    assert manager.xai.hangups == []


def test_inbound_and_outbound_share_persona(settings):
    from ringabot.calls import DISCLOSURE, opening_event, session_config

    inbound = session_config(settings, inbound=True)["session"]
    outbound = session_config(settings, "Ask about lunch")["session"]
    for key in ("voice", "turn_detection", "tools"):
        assert inbound[key] == outbound[key]
    assert inbound["voice"] == "aurora"
    assert "Be brief and polite." in inbound["instructions"]
    assert "Ask about lunch" in outbound["instructions"]
    assert "audio" in outbound and "audio" not in inbound
    text = opening_event(settings)["item"]["content"][0]["text"]
    assert text.startswith(DISCLOSURE)


class FakeAudio:
    def __init__(self):
        self.written: list[bytes] = []
        self.flushes = 0

    async def write(self, pcm):
        self.written.append(pcm)

    async def flush(self):
        self.flushes += 1


async def test_outbound_barge_in_drops_queued_speech(settings):
    import base64

    manager = CallManager(settings, xai=FakeXai())
    call = OutboundCall(manager, "out_1", _record(), "+16045550111", "")
    call.ws, call.sip, call.audio = FakeWS(), FakeSip(), FakeAudio()
    chunk = {"type": "response.output_audio.delta", "delta": base64.b64encode(b"ab").decode()}
    await call.on_event({"type": "response.created"})
    await call.on_event(chunk)
    await call.on_event({"type": "input_audio_buffer.speech_started"})
    await call.on_event(chunk)  # late chunk of the cancelled response
    assert call.audio.written == [b"ab"] and call.audio.flushes == 1
    assert call.ws.sent == []  # no response.cancel: xAI's server VAD handles that
    await call.on_event({"type": "response.created"})
    await call.on_event(chunk)
    assert len(call.audio.written) == 2
