import asyncio
import json

from conftest import FakeXai
from ringabot.calls import CallManager, CallSession
from ringabot.tools import tool_definitions


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _session(settings):
    manager = CallManager(settings, xai=FakeXai())
    record = {
        "call_id": "c1",
        "from": "+16045550123",
        "started_at": 0,
        "transcript": [],
        "tools": [],
        "events": [],
    }
    s = CallSession(manager, "c1", record)
    s._ws = FakeWS()
    return manager, s


def test_tool_definitions_flat_format(settings):
    tools = tool_definitions(settings)
    names = {t["name"] for t in tools}
    assert {"end_call", "get_current_time", "transfer_call"} <= names
    assert all(t["type"] == "function" and "parameters" in t for t in tools)


async def test_transcripts_recorded(settings):
    _, s = _session(settings)
    await s.handle(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hi"}
    )
    await s.handle({"type": "response.output_audio_transcript.done", "transcript": "hello"})
    assert s.record["transcript"] == [
        {"role": "caller", "text": "hi"},
        {"role": "agent", "text": "hello"},
    ]


async def test_tool_call_returns_output_then_response(settings):
    manager, s = _session(settings)
    await s.handle(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "fc1",
            "name": "get_current_time",
            "arguments": "{}",
        }
    )
    await asyncio.gather(*manager._tasks)
    kinds = [m["type"] for m in s._ws.sent]
    assert kinds == ["conversation.item.create", "response.create"]
    item = s._ws.sent[0]["item"]
    assert item["type"] == "function_call_output" and item["call_id"] == "fc1"
    assert "now" in json.loads(item["output"])


async def test_transfer_uses_configured_target(settings, monkeypatch):
    manager, s = _session(settings)
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    await s.handle(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "fc2",
            "name": "transfer_call",
            "arguments": json.dumps({"target": "desk"}),
        }
    )
    for _ in range(3):
        await asyncio.gather(*list(manager._tasks))
    assert manager.xai.refers == [("c1", "tel:+16045550111")]


async def test_unknown_tool_without_forwarder(settings):
    manager, s = _session(settings)
    await s.handle(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "fc3",
            "name": "book_table",
            "arguments": "{}",
        }
    )
    await asyncio.gather(*manager._tasks)
    assert "error" in json.loads(s._ws.sent[0]["item"]["output"])


async def test_end_call_hangs_up_after_response(settings, monkeypatch):
    manager, s = _session(settings)
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    await s.handle(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "fc4",
            "name": "end_call",
            "arguments": "{}",
        }
    )
    await s.handle({"type": "response.done"})
    for _ in range(3):
        await asyncio.gather(*list(manager._tasks))
    assert "c1" in manager.xai.hangups


_real_sleep = asyncio.sleep


async def _nosleep(_delay):
    await _real_sleep(0)
