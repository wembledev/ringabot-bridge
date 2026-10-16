"""In-process fake SIP user agent and fake Grok realtime WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json

import websockets


class FakeSipServer:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        socket = self._server.sockets[0]
        self.port = socket.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                self.commands.append(text)
                if text.startswith("dial "):
                    writer.write(b'{"type":"CALL_RINGING"}\n{"type":"CALL_ESTABLISHED"}\n')
                elif text == "hangup":
                    writer.write(b'{"type":"CALL_CLOSED","reason":"local"}\n')
                elif text == "register":
                    writer.write(b'{"type":"REGISTER_OK"}\n')
                else:
                    writer.write(b'{"type":"ERROR","reason":"unknown"}\n')
                await writer.drain()
        finally:
            writer.close()


class FakeGrokServer:
    def __init__(self, *, emit_tool: bool = False) -> None:
        self.emit_tool = emit_tool
        self.received: list[dict] = []
        self.port = 0
        self._server = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/realtime"

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, websocket) -> None:
        answered = False
        async for raw in websocket:
            if isinstance(raw, bytes):
                self.received.append({"type": "_binary"})
                continue
            event = json.loads(raw)
            self.received.append(event)
            kind = event.get("type")
            if kind == "session.update":
                await websocket.send(json.dumps({"type": "session.updated", "session": {}}))
                if self.emit_tool:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "response.function_call_arguments.done",
                                "name": "notify_backend",
                                "call_id": "fn_1",
                                "arguments": json.dumps({"note": "picnic confirmed"}),
                            }
                        )
                    )
            elif kind == "conversation.item.create":
                item = event.get("item") or {}
                if item.get("type") == "force_message":
                    text = item["content"][0]["text"]
                    await _speak(websocket, text)
                elif item.get("type") == "message":
                    await _speak(
                        websocket,
                        json.dumps(
                            {
                                "recap": "Called and confirmed.",
                                "answers": [{"question": "When?", "answer": "Noon"}],
                            }
                        ),
                    )
            elif kind == "input_audio_buffer.append" and not answered:
                answered = True
                await websocket.send(
                    json.dumps(
                        {
                            "type": "conversation.item.input_audio_transcription.updated",
                            "transcript": "Yes, noon works.",
                        }
                    )
                )
                await _speak(websocket, "Thanks. I have what I need.")


async def _speak(websocket, text: str) -> None:
    await websocket.send(json.dumps({"type": "response.created"}))
    await websocket.send(
        json.dumps(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(b"\x00\x00" * 16).decode("ascii"),
            }
        )
    )
    await websocket.send(
        json.dumps({"type": "response.output_audio_transcript.done", "transcript": text})
    )
    await websocket.send(json.dumps({"type": "response.done"}))
