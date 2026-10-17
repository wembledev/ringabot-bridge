"""One asyncio task per call: join the xAI realtime session by call_id and run tools.

Audio never touches this process. xAI bridges the SIP caller; we only exchange JSON
control events (config, transcripts, tool calls), so a 1 GB box handles it easily.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque

import websockets

from ringabot.tools import run_tool, tool_definitions
from ringabot.xai import XaiClient

log = logging.getLogger("ringabot.calls")


def _header(sip_headers: list, name: str) -> str:
    for h in sip_headers or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


class CallSession:
    def __init__(self, manager: CallManager, call_id: str, record: dict):
        self.manager = manager
        self.settings = manager.settings
        self.call_id = call_id
        self.record = record
        self._hangup_requested = False
        self._ws = None

    def request_hangup(self) -> None:
        self._hangup_requested = True
        # Fallback in case response.done never arrives.
        self.manager.spawn(self._hangup_later(8.0))

    async def _hangup_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.hangup("end_call")

    def start_transfer(self, uri: str) -> None:
        self.manager.spawn(self._transfer(uri))

    async def _transfer(self, uri: str) -> None:
        await asyncio.sleep(1.5)  # let the agent say "one moment"
        status, _ = await self.manager.xai.refer(self.call_id, uri)
        self.record["events"].append({"t": time.time(), "transfer_status": status})
        log.info("call %s transfer status %s", self.call_id, status)

    async def hangup(self, reason: str) -> None:
        if self.record.get("ended_at"):
            return
        self.record["end_reason"] = self.record.get("end_reason") or reason
        with contextlib.suppress(Exception):
            await self.manager.xai.hangup(self.call_id)

    async def _send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def run(self) -> None:
        s = self.settings
        url = f"{s.realtime_url}?call_id={self.call_id}"
        headers = {"Authorization": f"Bearer {s.xai_api_key}"}
        try:
            async with websockets.connect(
                url, additional_headers=headers, max_size=2**22, open_timeout=10
            ) as ws:
                self._ws = ws
                await self._send(
                    {
                        "type": "session.update",
                        "session": {
                            "voice": s.voice,
                            "instructions": s.instructions,
                            "turn_detection": {"type": "server_vad"},
                            "tools": tool_definitions(s),
                        },
                    }
                )
                await self._send(
                    {
                        "type": "response.create",
                        "response": {"instructions": f"{s.instructions}\n\n{s.greeting}"},
                    }
                )
                self.record["status"] = "in_progress"
                await asyncio.wait_for(self._loop(ws), timeout=s.max_call_seconds)
        except TimeoutError:
            await self.hangup("max_call_seconds")
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:  # noqa: BLE001
            self.record["error"] = type(exc).__name__
            log.warning("call %s failed: %s", self.call_id, type(exc).__name__)
        finally:
            self.record["status"] = "ended"
            self.record["ended_at"] = time.time()
            log.info(
                "call %s ended (%s) after %ss",
                self.call_id,
                self.record.get("end_reason") or "remote",
                int(self.record["ended_at"] - self.record["started_at"]),
            )

    async def _loop(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue  # audio frames are not expected; ignore
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self.handle(event)

    async def handle(self, event: dict) -> None:
        etype = event.get("type", "")
        transcript = self.record["transcript"]
        if etype == "conversation.item.input_audio_transcription.completed":
            transcript.append({"role": "caller", "text": event.get("transcript", "")})
        elif etype == "response.output_audio_transcript.done":
            transcript.append({"role": "agent", "text": event.get("transcript", "")})
        elif etype == "response.function_call_arguments.done":
            self.manager.spawn(self._tool(event))
        elif etype == "response.done" and self._hangup_requested:
            self.manager.spawn(self._hangup_later(1.0))
        elif etype == "error":
            err = event.get("error") or {}
            self.record["events"].append({"t": time.time(), "error": str(err)[:200]})
            log.warning("call %s realtime error: %s", self.call_id, str(err)[:200])
        del transcript[:-200]

    async def _tool(self, event: dict) -> None:
        name = event.get("name", "")
        started = time.monotonic()
        result = await run_tool(self, name, event.get("arguments") or "{}")
        ms = int((time.monotonic() - started) * 1000)
        self.record["tools"].append({"name": name, "ok": "error" not in result, "ms": ms})
        log.info("call %s tool %s (%sms)", self.call_id, name, ms)
        if self._ws is None:
            return
        with contextlib.suppress(websockets.ConnectionClosed):
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": event.get("call_id"),
                        "output": json.dumps(result),
                    },
                }
            )
            await self._send({"type": "response.create"})


class CallManager:
    def __init__(self, settings, xai: XaiClient | None = None):
        self.settings = settings
        self.xai = xai or XaiClient(settings.xai_api_key, settings.api_base)
        self.active: dict[str, CallSession] = {}
        self.recent: deque[dict] = deque(maxlen=settings.keep_calls)
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def accept(self, data: dict) -> tuple[bool, str]:
        """Called from the webhook handler; must return fast. Work runs in a task."""
        call_id = str(data.get("call_id") or "")
        if not call_id:
            return False, "missing call_id"
        if call_id in self.active:
            return True, "duplicate"
        sip = data.get("sip_headers") or []
        to = _header(sip, "To")
        if self.settings.did and self.settings.did.lstrip("+") not in to.replace("+", ""):
            return False, "not our number"
        if len(self.active) >= self.settings.max_concurrent_calls:
            self.spawn(self.xai.hangup(call_id))
            return False, "busy"
        record = {
            "call_id": call_id,
            "direction": "inbound",
            "from": _header(sip, "From"),
            "to": to,
            "started_at": time.time(),
            "status": "connecting",
            "transcript": [],
            "tools": [],
            "events": [],
        }
        self.recent.appendleft(record)
        session = CallSession(self, call_id, record)
        self.active[call_id] = session
        log.info("call %s incoming", call_id)

        async def runner():
            try:
                await session.run()
            finally:
                self.active.pop(call_id, None)

        self.spawn(runner())
        return True, "accepted"
