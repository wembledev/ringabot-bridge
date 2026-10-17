"""Inbound and outbound calls, one asyncio task each.

Inbound: xAI answers the SIP call itself; we join by call_id over a websocket and only
exchange JSON events. Outbound: baresip dials through the carrier and we relay 8 kHz PCM
between snd-aloop and the xAI realtime websocket.

Hang-up: the agent says goodbye itself, then calls end_call. We reply {"ok": true}
(nothing more is said), let the goodbye finish playing, wait a short tail, and send
exactly one hang-up. A backstop timer, an idle timeout and a max call length make sure
no call stays open.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path

import websockets

from ringabot.audio import SAMPLE_RATE, AlsaLoop
from ringabot.sip import SipClient, dial_uri, start_baresip
from ringabot.xai import XaiClient

log = logging.getLogger("ringabot.calls")

END_CALL_TOOL = {
    "type": "function",
    "name": "end_call",
    "description": (
        "End the phone call. Use only after you have said goodbye yourself. "
        "Never use it just because the other person said goodbye or hang up."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}
EMERGENCY = {"911", "112", "999", "000", "988", "110", "119"}
BACKSTOP_SECONDS = 10.0
GREET_AFTER_SECONDS = 4.0
FRAME_BYTES = 320  # 20 ms of 8 kHz s16 mono
PREROLL_BYTES = 2400  # 150 ms cushion written up front so the phone side never underruns
BYTES_PER_SECOND = 16000


DISCLOSURE = "I am an AI assistant, not a human. I cannot contact emergency services."
SAMPLE_FORMAT = {"type": "audio/pcm", "rate": SAMPLE_RATE}
REALTIME_URL = "wss://api.x.ai/v1/realtime"
WORK_DIR = Path("/run/ringabot")
KEEP_CALLS = 20


def instructions_for(settings, briefing: str = "", inbound: bool = False) -> str:
    """The one persona for every call. Outbound adds the briefing; inbound does not."""
    situation = "This is an incoming call to the user's number. " if inbound else ""
    purpose = briefing or "Answer the caller and help with what they need."
    return (
        f"You are {settings.persona_name}, an AI voice assistant on a live phone call. "
        f"{situation}{DISCLOSURE} Never claim to be human. Never place or forward emergency "
        "calls. If the person needs emergency help, tell them to hang up and dial their local "
        "emergency number themselves. Do not claim the call is being recorded.\n"
        f"Additional persona notes:\n{settings.instructions}\n"
        f"Purpose of this call:\n{purpose}\n"
        f"{_first_line(settings, inbound)}\n"
        "Keep turns short. When the task is done, say goodbye, then use the end_call tool. "
        "Never use end_call just because the other person said goodbye or hang up."
    )


def opening_line(settings) -> str:
    """What the agent says first on every call. Keep it short; it must say it is an AI."""
    return settings.greeting or f"Hi, this is {settings.persona_name}, an AI assistant."


def _first_line(settings, inbound: bool) -> str:
    line = opening_line(settings)
    if inbound:
        return f'Start the call by saying: "{line}"'
    return f'You placed this call. Let them speak first, then begin your first reply with: "{line}"'


def session_config(settings, briefing: str = "", inbound: bool = False) -> dict:
    """Same persona for inbound and outbound; outbound only adds its briefing and the PCM
    format it relays (xAI owns the audio on inbound SIP calls)."""
    session = {
        "voice": settings.voice,
        "instructions": instructions_for(settings, briefing, inbound),
        "turn_detection": {"type": "server_vad"},
        "tools": [END_CALL_TOOL],
    }
    if not inbound:
        session["audio"] = {
            "input": {"format": SAMPLE_FORMAT, "transcription": {"model": "grok-transcribe"}},
            "output": {"format": SAMPLE_FORMAT},
        }
    return {"type": "session.update", "session": session}


# First turn: a plain response; the session instructions say what to open with, and
# xAI's turn detection can interrupt it like any other response.
OPENING_EVENT = {"type": "response.create"}


def _header(sip_headers: list, name: str) -> str:
    for h in sip_headers or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _sip_user(header: str) -> str:
    """E.164 of the user part of a To/From value such as "<sip:+17785550100@host>"."""
    user = header.split("sip:", 1)[-1].split("@", 1)[0].split(";", 1)[0]
    return e164(user)


def e164(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


class Call:
    """Shared event handling, end_call, timeouts and the single hang-up."""

    tail_seconds = 1.0

    def __init__(self, manager: CallManager, call_id: str, record: dict):
        self.manager = manager
        self.settings = manager.settings
        self.call_id = call_id
        self.record = record
        self.ws = None
        self.armed = False
        self._tail_spawned = False
        self.play_until = 0.0  # monotonic time the agent's queued speech should end
        self._items: dict[str, dict] = {}
        self._hung_up = False
        self.last_activity = time.monotonic()
        self.started = time.monotonic()

    # -- hang-up -----------------------------------------------------------
    async def hangup(self, reason: str) -> None:
        if self._hung_up:
            return
        self._hung_up = True
        self.record["end_reason"] = self.record.get("end_reason") or reason
        log.info("call %s hanging up (%s)", self.call_id, reason)
        with contextlib.suppress(Exception):
            await self._hangup_leg()
        if self.ws is not None:  # ends the event loop even if the hang-up leg failed
            with contextlib.suppress(Exception):
                await self.ws.close()

    async def _hangup_leg(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def arm(self, reason: str) -> None:
        """Hang up after the next response finishes playing (or the backstop)."""
        if self.armed:
            return
        self.armed = True
        self.record["end_reason"] = self.record.get("end_reason") or reason
        self.manager.spawn(self._backstop())

    async def _backstop(self) -> None:
        await asyncio.sleep(BACKSTOP_SECONDS)
        await self.hangup(self.record.get("end_reason") or "backstop")

    async def _after_tail(self) -> None:
        """Wait until the goodbye has played out, then a short tail (voice-agent pattern)."""
        await asyncio.sleep(max(0.0, self.play_until - time.monotonic()) + self.tail_seconds)
        await self.hangup(self.record.get("end_reason") or "end_call")

    async def say_goodbye(self, reason: str) -> None:
        if self.armed:
            return
        self.arm(reason)
        await self.send(
            {
                "type": "response.create",
                "response": {"instructions": "Say one short goodbye now. The call is ending."},
            }
        )

    async def watchdog(self) -> None:
        s = self.settings
        while not self._hung_up:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if now - self.started >= s.max_call_seconds:
                await self.say_goodbye("max_call_seconds")
            elif now - self.last_activity >= s.idle_seconds:
                await self.say_goodbye("idle")

    # -- realtime events ---------------------------------------------------
    async def send(self, event: dict) -> None:
        if self.ws is not None:
            with contextlib.suppress(websockets.ConnectionClosed):
                await self.ws.send(json.dumps(event))

    def _transcript(self, role: str, event: dict) -> None:
        """Store final lines only, one per conversation item (xAI can repeat events)."""
        text = (event.get("transcript") or "").strip()
        if not text:
            return
        item_id = str(event.get("item_id") or "")
        if item_id and item_id in self._items:
            self._items[item_id]["text"] = text
            return
        lines = self.record["transcript"]
        if not item_id and lines and lines[-1] == {"role": role, "text": text}:
            return
        entry = {"role": role, "text": text}
        lines.append(entry)
        if item_id:
            self._items[item_id] = entry
        del lines[:-200]

    async def on_event(self, event: dict) -> None:
        etype = event.get("type", "")
        now = time.monotonic()
        if etype == "input_audio_buffer.speech_started":
            # xAI's server VAD owns turn-taking and cancels its own response.
            self.last_activity = now
            await self.on_interrupt()
        elif etype == "response.output_audio.delta":
            self.last_activity = now
        elif etype == "conversation.item.input_audio_transcription.completed":
            self.last_activity = now
            self._transcript("caller", event)
        elif etype == "response.output_audio_transcript.done":
            self._transcript("agent", event)
        elif etype == "response.function_call_arguments.done":
            await self._tool(event)
        elif etype == "response.done" and self.armed and not self._tail_spawned:
            self._tail_spawned = True
            self.manager.spawn(self._after_tail())
        elif etype == "error":
            log.warning("call %s realtime error: %s", self.call_id, str(event.get("error"))[:200])

    async def on_interrupt(self) -> None:
        """Inbound: nothing to do, xAI plays the audio. Outbound overrides it."""
        self.play_until = 0.0

    async def _tool(self, event: dict) -> None:
        """end_call gets a bare {"ok": true} and no new response, so nothing more is said;
        we hang up once the goodbye that came before it has played."""
        name = event.get("name", "")
        self.record["tools"] = [*self.record["tools"], name][-20:]
        output = {"ok": True} if name == "end_call" else {"error": "unknown tool"}
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": event.get("call_id"),
                    "output": json.dumps(output),
                },
            }
        )
        if name == "end_call":
            self.arm("end_call")
        else:
            await self.send({"type": "response.create"})

    def finish(self) -> None:
        self._hung_up = True
        self.record["status"] = "ended"
        self.record["ended_at"] = time.time()
        log.info(
            "call %s ended (%s) after %ss",
            self.call_id,
            self.record.get("end_reason") or "remote",
            int(self.record["ended_at"] - self.record["started_at"]),
        )


class InboundCall(Call):
    """xAI owns the SIP leg and plays audio; we estimate playback from the transcript."""

    tail_seconds = 1.5

    def __init__(self, manager, call_id, record):
        super().__init__(manager, call_id, record)
        self._response_started = time.monotonic()

    async def _hangup_leg(self) -> None:
        await self.manager.xai.hangup(self.call_id)

    async def on_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "response.created":
            self._response_started = time.monotonic()
        elif etype == "response.output_audio_transcript.done":
            # Speech runs about 14 characters a second; xAI may still be playing it.
            spoken = len(event.get("transcript") or "") / 14.0
            self.play_until = max(self.play_until, self._response_started + spoken)
        await super().on_event(event)

    async def run(self) -> None:
        s = self.settings
        url = f"{REALTIME_URL}?call_id={self.call_id}"
        headers = {"Authorization": f"Bearer {s.xai_api_key}"}
        try:
            async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
                self.ws = ws
                await self.send(session_config(s, inbound=True))
                await self.send(OPENING_EVENT)
                self.record["status"] = "in_progress"
                dog = self.manager.spawn(self.watchdog())
                try:
                    async for raw in ws:
                        if isinstance(raw, str):
                            with contextlib.suppress(json.JSONDecodeError):
                                await self.on_event(json.loads(raw))
                finally:
                    dog.cancel()
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:  # noqa: BLE001
            self.record["error"] = type(exc).__name__
            log.warning("call %s failed: %s", self.call_id, type(exc).__name__)
            await self.hangup("error")  # xAI already answered; don't leave dead air
        finally:
            self.finish()


class OutboundCall(Call):
    """baresip dials; PCM is relayed between snd-aloop and the realtime websocket."""

    def __init__(self, manager, call_id, record, number: str, briefing: str):
        super().__init__(manager, call_id, record)
        self.number = number
        self.briefing = briefing
        self.sip: SipClient | None = None
        self.audio: AlsaLoop | None = None
        self._muted = False
        self._heard = False
        self._out = bytearray()  # xAI audio waiting to be played at real time
        self._out_ready = asyncio.Event()

    async def _hangup_leg(self) -> None:
        if self.sip is not None:
            await self.sip.command("hangup")

    async def on_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype in ("input_audio_buffer.speech_started", "response.created"):
            self._heard = True
        if etype == "response.created":
            self._muted = False
        elif etype == "response.output_audio.delta" and self.audio is not None and not self._muted:
            with contextlib.suppress(Exception):
                pcm = base64.b64decode(event.get("delta") or "")
                queued = len(pcm) / BYTES_PER_SECOND
                self.play_until = max(self.play_until, time.monotonic()) + queued
                self._out += pcm
                self._out_ready.set()
        await super().on_event(event)

    async def _speaker(self) -> None:
        """Play queued xAI audio at real time: xAI sends it in fast bursts, and writing a
        burst straight into the loopback makes the start of a response choppy."""
        while True:
            await self._out_ready.wait()
            deadline = time.monotonic() + PREROLL_BYTES / BYTES_PER_SECOND
            while len(self._out) < PREROLL_BYTES and time.monotonic() < deadline:
                await asyncio.sleep(0.01)  # let a little audio build up first
            chunk = bytes(self._out[:PREROLL_BYTES])
            del self._out[:PREROLL_BYTES]
            next_at = time.monotonic()
            while chunk:
                with contextlib.suppress(Exception):
                    await self.audio.write(chunk)
                next_at += FRAME_BYTES / BYTES_PER_SECOND
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
                chunk = bytes(self._out[:FRAME_BYTES])
                del self._out[:FRAME_BYTES]
            self._out_ready.clear()

    async def _greet_if_silent(self) -> None:
        await asyncio.sleep(GREET_AFTER_SECONDS)
        if not self._heard and not self._hung_up:
            self._heard = True
            await self.send(OPENING_EVENT)

    async def on_interrupt(self) -> None:
        """Caller started talking: xAI already cancels its response; we only drop the
        speech we have queued for the phone and ignore late chunks of that response."""
        self._muted = True
        self.play_until = 0.0
        self._out.clear()
        if self.audio is not None:
            with contextlib.suppress(Exception):
                await self.audio.flush()

    async def run(self) -> None:
        s = self.settings
        baresip = None
        try:
            self.audio = AlsaLoop()
            await self.audio.start()
            baresip = await start_baresip(
                work_dir=WORK_DIR,
                pcm=self.audio.baresip_device,
                user=s.sip_username,
                password=s.sip_password,
                server=s.sip_server,
                did=e164(s.did).lstrip("+"),
            )
            self.sip = SipClient(baresip.port)
            await self.sip.connect()
            headers = {"Authorization": f"Bearer {s.xai_api_key}"}
            async with websockets.connect(
                REALTIME_URL, additional_headers=headers, open_timeout=10, max_size=2**20
            ) as ws:
                self.ws = ws
                await self.send(session_config(s, self.briefing))
                self.record["status"] = "dialing"
                # Throw away audio captured while ringing so ringback never reaches xAI.
                ringing = self.manager.spawn(self._discard_audio())
                await self.sip.command(f"dial {dial_uri(self.number, s.sip_server)}")
                ev = await self.sip.wait_type("CALL_ESTABLISHED", "CALL_CLOSED", timeout=60)
                ringing.cancel()
                if ev.get("type") != "CALL_ESTABLISHED":
                    self.record["error"] = "not_answered"
                    self.record["end_reason"] = "not_answered"
                    return
                self.record["status"] = "in_progress"
                self.started = self.last_activity = time.monotonic()
                # Wait for "hello": xAI's turn detection makes the first reply. Greet anyway
                # if nobody speaks soon.
                self.manager.spawn(self._greet_if_silent())
                await self._relay(ws)
        except Exception as exc:  # noqa: BLE001
            self.record["error"] = self.record.get("error") or type(exc).__name__
            log.warning("call %s failed: %s", self.call_id, type(exc).__name__)
        finally:
            await self.hangup(self.record.get("end_reason") or "remote_hangup")
            if self.sip is not None:
                await self.sip.close()
            if baresip is not None:
                await baresip.stop()
            if self.audio is not None:
                await self.audio.stop()
            self.finish()

    async def _discard_audio(self) -> None:
        while await self.audio.read() is not None:
            pass

    async def _relay(self, ws) -> None:
        async def mic() -> None:
            while (pcm := await self.audio.read()) is not None:
                await self.send(
                    {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
                )

        async def voice() -> None:
            async for raw in ws:
                if isinstance(raw, str):
                    with contextlib.suppress(json.JSONDecodeError):
                        await self.on_event(json.loads(raw))

        async def remote() -> None:
            while (await self.sip.events.get()).get("type") not in ("CALL_CLOSED", "EOF"):
                pass
            if not self._hung_up:
                self.record["end_reason"] = self.record.get("end_reason") or "remote_hangup"
                self._hung_up = True  # the far end already hung up; no BYE needed

        jobs = (mic, voice, remote, self.watchdog, self._speaker)
        tasks = [asyncio.create_task(c()) for c in jobs]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()


class CallManager:
    def __init__(self, settings, xai: XaiClient | None = None):
        self.settings = settings
        self.xai = xai or XaiClient(settings.xai_api_key)
        self.active: dict[str, Call] = {}
        self.recent: deque[dict] = deque(maxlen=KEEP_CALLS)
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _record(self, call_id: str, direction: str, frm: str, to: str) -> dict:
        record = {
            "call_id": call_id,
            "direction": direction,
            "from": frm,
            "to": to,
            "started_at": time.time(),
            "status": "connecting",
            "transcript": [],
            "tools": [],
        }
        self.recent.appendleft(record)
        return record

    def _start(self, call: Call) -> None:
        self.active[call.call_id] = call

        async def runner():
            try:
                await call.run()
            finally:
                self.active.pop(call.call_id, None)

        self.spawn(runner())

    def accept(self, data: dict) -> tuple[bool, str]:
        """Called from the webhook handler; must return fast. Work runs in a task."""
        call_id = str(data.get("call_id") or "")
        if not call_id:
            return False, "missing call_id"
        if call_id in self.active:
            return True, "duplicate"
        sip = data.get("sip_headers") or []
        to = _header(sip, "To")
        if self.settings.did and _sip_user(to) != e164(self.settings.did):
            return False, "not our number"
        if len(self.active) >= self.settings.max_concurrent_calls:
            self.spawn(self.xai.hangup(call_id))
            return False, "busy"
        record = self._record(call_id, "inbound", _header(sip, "From"), to)
        log.info("call %s incoming", call_id)
        self._start(InboundCall(self, call_id, record))
        return True, "accepted"

    def place(self, number: str, briefing: str = "") -> tuple[str | None, str]:
        s = self.settings
        if not s.outbound_ready:
            return None, "outbound not configured"
        digits = "".join(ch for ch in number if ch.isdigit())
        if digits in EMERGENCY or not 10 <= len(digits) <= 15:
            return None, "number not allowed"
        target = e164(number)
        if not any(target.startswith(p) for p in s.allowed_destinations):
            return None, "number not in ALLOWED_DESTINATIONS"
        if len(self.active) >= s.max_concurrent_calls:
            return None, "busy"
        call_id = "out_" + uuid.uuid4().hex[:16]
        record = self._record(call_id, "outbound", e164(s.did), target)
        self._start(OutboundCall(self, call_id, record, target, briefing[:2000]))
        log.info("call %s outbound dialing", call_id)
        return call_id, "dialing"
