"""xAI Grok realtime voice session.

Public protocol: wss://api.x.ai/v1/realtime with server-side VAD.
Audio is 8 kHz PCM16, JSON base64 frames, which matches the SIP side and
keeps resampling off the 1 GB host.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import websockets

from ringabot.audio import SAMPLE_RATE
from ringabot.outbound import voice_url_allowed

log = logging.getLogger("ringabot.bridge")

_ASSISTANT_TEXT = (
    "response.output_audio_transcript.done",
    "response.output_audio_transcript.delta",
    "response.output_text.done",
    "response.output_text.delta",
    "response.audio_transcript.done",
)


class VoiceClient:
    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)
        self._ws = None
        self._task: asyncio.Task | None = None

    async def connect(self) -> None:
        if not voice_url_allowed(self.url):
            raise RuntimeError("voice_url_blocked")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        limits = {
            "open_timeout": 5,
            "close_timeout": 2,
            "max_size": 1_048_576,
            "max_queue": 32,
            "ping_interval": 20,
            "ping_timeout": 10,
        }
        try:
            connector = websockets.connect(self.url, additional_headers=headers, **limits)
        except TypeError:
            connector = websockets.connect(self.url, extra_headers=headers, **limits)
        self._ws = await connector
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    await self.events.put({"type": "_binary", "audio": message})
                    continue
                try:
                    await self.events.put(json.loads(message))
                except json.JSONDecodeError:
                    await self.events.put({"type": "RAW", "line": message})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("voice socket closed")
        finally:
            await self.events.put({"type": "_closed"})

    async def send(self, event: dict) -> None:
        if self._ws is None:
            raise RuntimeError("voice_not_connected")
        await self._ws.send(json.dumps(event))

    async def configure(self, session: dict) -> None:
        await self.send({"type": "session.update", "session": session})

    async def force_message(self, text: str) -> None:
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "force_message",
                    "role": "assistant",
                    "interruptible": False,
                    "content": [{"type": "output_text", "text": text}],
                },
            }
        )

    async def send_audio(self, pcm: bytes) -> None:
        await self.send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    async def request_summary(self, prompt: str) -> None:
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        )
        await self.send(
            {
                "type": "response.create",
                "response": {"instructions": "Reply with JSON only."},
            }
        )

    async def function_result(self, call_id: str, output: dict) -> None:
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output),
                },
            }
        )
        await self.send({"type": "response.create"})

    async def next_event(self) -> dict:
        return await self.events.get()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._ws is not None:
            await self._ws.close()


def session_body(*, voice: str, instructions: str, tools: list[dict]) -> dict:
    body: dict = {
        "voice": voice,
        "instructions": instructions,
        "turn_detection": {"type": "server_vad"},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                "transcription": {"model": "grok-transcribe"},
            },
            "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
        },
    }
    if tools:
        body["tools"] = tools
    return body


def notify_tool() -> dict:
    return {
        "type": "function",
        "name": "notify_backend",
        "description": "Send one short note to the operator backend.",
        "parameters": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    }


def assistant_text(event: dict) -> str | None:
    if event.get("type") not in _ASSISTANT_TEXT and "transcript" not in event:
        return None
    if event.get("type", "").endswith(".delta"):
        return None
    text = event.get("transcript") or event.get("text") or event.get("delta")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def user_text(event: dict) -> str | None:
    if event.get("type") != "conversation.item.input_audio_transcription.updated":
        return None
    text = event.get("transcript")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def audio_bytes(event: dict) -> bytes | None:
    if event.get("type") == "_binary":
        data = event.get("audio")
        return data if isinstance(data, bytes) else None
    if event.get("type") not in {"response.output_audio.delta", "response.audio.delta"}:
        return None
    delta = event.get("delta") or event.get("audio")
    if not isinstance(delta, str) or not delta:
        return None
    try:
        return base64.b64decode(delta)
    except Exception:
        return None
