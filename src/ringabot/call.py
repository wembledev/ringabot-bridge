"""One outbound call. The HTTP handler returns before this task runs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from ringabot.audio import AlsaLoop, MemoryAudio, PulseAudio, baresip_pcm
from ringabot.bridge import (
    VoiceClient,
    assistant_text,
    audio_bytes,
    notify_tool,
    session_body,
    user_text,
)
from ringabot.config import Settings
from ringabot.credentials import Credentials, resolve_credentials
from ringabot.limits import LimitError, normalize_destination
from ringabot.persona import GOODBYE, opening_line, session_instructions, summary_prompt
from ringabot.records import enforce_retention, parse_summary
from ringabot.redact import REDACTOR
from ringabot.sip import SipClient, confirm_registration, dial_uri, start_baresip
from ringabot.store import Store, utc_iso
from ringabot.webhooks import WebhookClient

log = logging.getLogger("ringabot.call")


@dataclass
class Control:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = "completed"


class CallService:
    def __init__(self, settings: Settings, store: Store, webhooks: WebhookClient) -> None:
        self.settings = settings
        self.store = store
        self.webhooks = webhooks
        self.tasks: dict[str, asyncio.Task] = {}
        self.controls: dict[str, Control] = {}
        self.audios: dict[str, MemoryAudio | PulseAudio | AlsaLoop] = {}
        self._lock = asyncio.Lock()
        self._global = 0
        self._tenants: dict[str, int] = {}

    async def place(
        self,
        tenant: str,
        *,
        number: str,
        briefing: str,
        questions: list[str] | None,
        max_minutes: float | None,
    ) -> dict:
        briefing = briefing.strip()
        if not briefing:
            raise LimitError("briefing_required")
        if len(briefing) > 4000:
            raise LimitError("briefing_too_long")
        if questions is None:
            asked = []
        elif not isinstance(questions, list) or any(
            not isinstance(item, str) for item in questions
        ):
            raise LimitError("questions_invalid")
        else:
            asked = [item.strip() for item in questions if item.strip()]
        if len(asked) > 8 or any(len(item) > 300 for item in asked):
            raise LimitError("questions_invalid")
        destination = normalize_destination(number, self.settings.allowed_destinations)
        creds = resolve_credentials(self.settings, self.store, tenant)
        if not creds.sip_ready() or not creds.voice_ready():
            raise LimitError("not_configured")
        cap = self.settings.max_call_seconds
        if max_minutes is None:
            max_seconds = cap
        elif isinstance(max_minutes, bool) or not isinstance(max_minutes, (int, float)):
            raise LimitError("max_minutes_invalid")
        else:
            try:
                requested = int(max_minutes * 60)
            except (OverflowError, ValueError) as exc:
                raise LimitError("max_minutes_invalid") from exc
            if requested < 30:
                raise LimitError("max_minutes_too_small")
            max_seconds = min(requested, cap)
        call_id = "call_" + uuid.uuid4().hex[:16]
        async with self._lock:
            if self._tenants.get(tenant, 0) >= 1:
                raise LimitError("tenant_busy")
            if self._global >= self.settings.max_concurrent_calls:
                raise LimitError("busy")
            if self.store.calls_today(tenant) >= self.settings.max_calls_per_day:
                raise LimitError("daily_cap")
            self.store.insert_call(
                call_id=call_id,
                tenant=tenant,
                destination=destination.e164,
                briefing=briefing,
                questions=asked,
                max_seconds=max_seconds,
            )
            self.store.increment_calls_today(tenant)
            self._tenants[tenant] = self._tenants.get(tenant, 0) + 1
            self._global += 1
        control = Control()
        self.controls[call_id] = control
        task = asyncio.create_task(
            self._run(call_id, tenant, destination.digits, creds, asked, max_seconds),
            name=call_id,
        )
        self.tasks[call_id] = task
        task.add_done_callback(lambda done: self._log_task(call_id, done))
        return {"call_id": call_id, "status": "queued"}

    def hangup(self, tenant: str, call_id: str) -> bool:
        row = self.store.get_call(tenant, call_id)
        if row is None:
            return False
        control = self.controls.get(call_id)
        if control is None or row["status"] in {"completed", "failed", "cancelled"}:
            return row["status"] in {"completed", "failed", "cancelled"}
        control.reason = "hangup"
        control.stop.set()
        return True

    async def accept_inbound(
        self,
        tenant: str,
        *,
        caller: str,
        briefing: str,
        greeting: str,
        sip: SipClient,
        devices: tuple[str, str] | None = None,
    ) -> asyncio.Task | None:
        """Start the voice session on a SIP leg baresip already answered.

        The registrar keeps the control socket. This returns None when the
        same caps that block outbound calls are already full.
        """
        creds = resolve_credentials(self.settings, self.store, tenant)
        if not creds.sip_ready() or not creds.voice_ready():
            return None
        text = briefing.strip() or "Answer the incoming call."
        if len(text) > 4000:
            text = text[:4000]
        destination = caller or "anonymous"
        max_seconds = self.settings.max_call_seconds
        call_id = "call_" + uuid.uuid4().hex[:16]
        async with self._lock:
            if self._tenants.get(tenant, 0) >= 1:
                return None
            if self._global >= self.settings.max_concurrent_calls:
                return None
            if self.store.calls_today(tenant) >= self.settings.max_calls_per_day:
                return None
            self.store.insert_call(
                call_id=call_id,
                tenant=tenant,
                destination=destination,
                briefing=text,
                questions=[],
                max_seconds=max_seconds,
                direction="inbound",
            )
            self.store.increment_calls_today(tenant)
            self._tenants[tenant] = self._tenants.get(tenant, 0) + 1
            self._global += 1
        control = Control()
        self.controls[call_id] = control
        task = asyncio.create_task(
            self._run_inbound(call_id, tenant, creds, greeting, max_seconds, sip, devices),
            name=call_id,
        )
        self.tasks[call_id] = task
        task.add_done_callback(lambda done: self._log_task(call_id, done))
        return task

    async def _run_inbound(
        self,
        call_id: str,
        tenant: str,
        creds: Credentials,
        greeting: str,
        max_seconds: int,
        sip: SipClient,
        devices: tuple[str, str] | None,
    ) -> None:
        REDACTOR.add(creds.sip_password)
        REDACTOR.add(creds.xai_api_key)
        REDACTOR.add(self.settings.api_token)
        REDACTOR.add(self.settings.tools_webhook_secret)
        control = self.controls[call_id]
        audio: MemoryAudio | PulseAudio | AlsaLoop | None = None
        voice: VoiceClient | None = None
        try:
            audio = self._new_audio(call_id, devices)
            self.audios[call_id] = audio
            await audio.start()
            self.store.update_call(call_id, status="in_progress", started_at=utc_iso())
            voice = VoiceClient(self.settings.xai_realtime_endpoint, creds.xai_api_key)
            await voice.connect()
            tools = [notify_tool()] if self.settings.tools_webhook_url else []
            row = self.store.get_call(tenant, call_id)
            await voice.configure(
                session_body(
                    voice=creds.persona.voice,
                    instructions=session_instructions(
                        persona=creds.persona,
                        briefing=row["briefing"] if row else "",
                        questions=[],
                        recordings=self.settings.recordings,
                        inbound=True,
                    ),
                    tools=tools,
                )
            )
            if control.stop.is_set():
                self._cancel(call_id, control)
                return
            spoken = opening_line(self.settings.recordings, greeting)
            await voice.force_message(spoken)
            self.store.add_transcript(call_id, "assistant", spoken)
            await self._relay(call_id, tenant, audio, sip, voice, control, max_seconds)
            if control.reason == "failed":
                raise RuntimeError("audio_ended")
            await self._finish(call_id, tenant, voice, [], control.reason)
        except Exception:
            log.exception("call %s failed", call_id)
            row = self.store.get_call(tenant, call_id)
            if row and row["status"] in {"completed", "cancelled"}:
                return
            if control.reason == "completed":
                control.reason = "failed"
            self.store.update_call(
                call_id,
                status="failed",
                error="call_failed",
                end_reason=control.reason,
                ended_at=utc_iso(),
            )
        finally:
            try:
                await sip.command("hangup")
            except Exception:
                pass
            if voice is not None:
                await voice.close()
            if audio is not None:
                await audio.stop()
            async with self._lock:
                self._global = max(0, self._global - 1)
                self._tenants[tenant] = max(0, self._tenants.get(tenant, 1) - 1)
            self.audios.pop(call_id, None)
            self.controls.pop(call_id, None)
            self.tasks.pop(call_id, None)
            enforce_retention(self.store, self.settings.retention_days)

    async def _run(
        self,
        call_id: str,
        tenant: str,
        digits: str,
        creds: Credentials,
        questions: list[str],
        max_seconds: int,
    ) -> None:
        REDACTOR.add(creds.sip_password)
        REDACTOR.add(creds.xai_api_key)
        REDACTOR.add(self.settings.api_token)
        REDACTOR.add(self.settings.tools_webhook_secret)
        control = self.controls[call_id]
        audio: MemoryAudio | PulseAudio | AlsaLoop | None = None
        sip: SipClient | None = None
        voice: VoiceClient | None = None
        baresip = None
        try:
            self.store.update_call(call_id, status="dialing")
            audio = self._new_audio(call_id)
            self.audios[call_id] = audio
            await audio.start()
            play, mic = _devices(audio, call_id)
            sip, baresip = await self._open_sip(creds, play, mic)
            voice = VoiceClient(self.settings.xai_realtime_endpoint, creds.xai_api_key)
            await voice.connect()
            tools = [notify_tool()] if self.settings.tools_webhook_url else []
            await voice.configure(
                session_body(
                    voice=creds.persona.voice,
                    instructions=session_instructions(
                        persona=creds.persona,
                        briefing=self.store.get_call(tenant, call_id)["briefing"],
                        questions=questions,
                        recordings=self.settings.recordings,
                    ),
                    tools=tools,
                )
            )
            if control.stop.is_set():
                self._cancel(call_id, control)
                return
            await sip.command(f"dial {dial_uri(digits, creds.sip_server)}")
            established = await _wait_established(sip, control)
            if established.get("type") == "CANCELLED":
                self._cancel(call_id, control)
                return
            if established.get("type") != "CALL_ESTABLISHED":
                raise RuntimeError("dial_failed")
            self.store.update_call(call_id, status="in_progress", started_at=utc_iso())
            opening = opening_line(self.settings.recordings, creds.persona.greeting)
            await voice.force_message(opening)
            self.store.add_transcript(call_id, "assistant", opening)
            await self._relay(
                call_id,
                tenant,
                audio,
                sip,
                voice,
                control,
                max_seconds,
            )
            if control.reason == "failed":
                raise RuntimeError("audio_ended")
            # Drop the SIP leg before the summary so the model does not read JSON to the person.
            try:
                await sip.command("hangup")
            except Exception:
                pass
            await self._finish(call_id, tenant, voice, questions, control.reason)
        except Exception:
            log.exception("call %s failed", call_id)
            row = self.store.get_call(tenant, call_id)
            if row and row["status"] == "completed":
                return
            if control.reason == "completed":
                control.reason = "failed"
            self.store.update_call(
                call_id,
                status="failed",
                error="call_failed",
                end_reason=control.reason,
                ended_at=utc_iso(),
            )
        finally:
            if sip is not None:
                try:
                    await sip.command("hangup")
                except Exception:
                    pass
                await sip.close()
            if voice is not None:
                await voice.close()
            if audio is not None:
                await audio.stop()
            if baresip is not None:
                await baresip.stop()
            async with self._lock:
                self._global = max(0, self._global - 1)
                self._tenants[tenant] = max(0, self._tenants.get(tenant, 1) - 1)
            self.audios.pop(call_id, None)
            self.controls.pop(call_id, None)
            self.tasks.pop(call_id, None)
            enforce_retention(self.store, self.settings.retention_days)

    def _cancel(self, call_id: str, control: Control) -> None:
        self.store.update_call(
            call_id,
            status="cancelled",
            end_reason=control.reason or "hangup",
            ended_at=utc_iso(),
        )

    async def _relay(
        self,
        call_id: str,
        tenant: str,
        audio: MemoryAudio | PulseAudio | AlsaLoop,
        sip: SipClient,
        voice: VoiceClient,
        control: Control,
        max_seconds: int,
    ) -> None:
        idle_seconds = self.settings.idle_timeout
        deadline = time.monotonic() + idle_seconds
        max_at = time.monotonic() + max_seconds
        recording = _recorder(self.settings, call_id)
        seen_tools: set[str] = set()
        audio_task = asyncio.create_task(audio.read_remote())
        voice_task = asyncio.create_task(voice.next_event())
        sip_task = asyncio.create_task(sip.events.get())
        stop_task = asyncio.create_task(control.stop.wait())
        closing = False
        close_at = 0.0
        heard_goodbye = False
        try:
            while True:
                now = time.monotonic()
                if closing:
                    timeout = close_at - now
                    if timeout <= 0:
                        break
                else:
                    timeout = min(deadline, max_at) - now
                    if timeout <= 0 or control.stop.is_set():
                        if await self._say_goodbye(call_id, voice, control, max_at):
                            closing = True
                            close_at = time.monotonic() + 3
                            if stop_task.done():
                                stop_task = asyncio.create_task(asyncio.sleep(3600))
                            continue
                        break
                done, _pending = await asyncio.wait(
                    {audio_task, voice_task, sip_task, stop_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not closing and (stop_task in done or control.stop.is_set()):
                    if await self._say_goodbye(call_id, voice, control, max_at):
                        closing = True
                        close_at = time.monotonic() + 3
                        stop_task = asyncio.create_task(asyncio.sleep(3600))
                        continue
                    break
                if not done:
                    if closing:
                        break
                    if await self._say_goodbye(call_id, voice, control, max_at):
                        closing = True
                        close_at = time.monotonic() + 3
                        continue
                    break
                if closing and stop_task in done:
                    stop_task = asyncio.create_task(asyncio.sleep(3600))
                if audio_task in done:
                    pcm = audio_task.result()
                    audio_task = asyncio.create_task(audio.read_remote())
                    if pcm is None:
                        if not closing and control.reason == "completed":
                            control.reason = "failed"
                        break
                    if pcm:
                        if audible(pcm) and not closing:
                            deadline = time.monotonic() + idle_seconds
                        if recording is not None:
                            recording.write(pcm)
                        await voice.send_audio(pcm)
                if voice_task in done:
                    event = voice_task.result()
                    voice_task = asyncio.create_task(voice.next_event())
                    if event.get("type") == "_closed":
                        if control.reason == "completed":
                            control.reason = "voice_closed"
                        break
                    if not closing:
                        deadline = await self._on_voice(
                            call_id, event, audio, voice, deadline, idle_seconds, seen_tools
                        )
                    else:
                        await self._on_voice(
                            call_id, event, audio, voice, deadline, idle_seconds, seen_tools
                        )
                        if event.get("type") == "response.created":
                            heard_goodbye = True
                        if heard_goodbye and event.get("type") == "response.done":
                            break
                if sip_task in done:
                    event = sip_task.result()
                    sip_task = asyncio.create_task(sip.events.get())
                    if event.get("type") in {"CALL_CLOSED", "EOF"}:
                        if not closing:
                            control.reason = "remote_hangup"
                        break
        finally:
            for task in (audio_task, voice_task, sip_task, stop_task):
                task.cancel()
            if recording is not None:
                recording.close()
                path = self.settings.data_dir / "recordings" / f"{call_id}.pcm"
                self.store.add_recording(tenant, call_id, str(path))

    async def _on_voice(
        self,
        call_id: str,
        event: dict,
        audio: MemoryAudio | PulseAudio | AlsaLoop,
        voice: VoiceClient,
        deadline: float,
        idle_seconds: float,
        seen_tools: set[str],
    ) -> float:
        if event.get("type") in {
            "input_audio_buffer.speech_started",
            "response.created",
            "response.output_audio.delta",
        }:
            deadline = time.monotonic() + idle_seconds
        pcm = audio_bytes(event)
        if pcm:
            deadline = time.monotonic() + idle_seconds
            await audio.write_remote(pcm)
        spoken = user_text(event)
        if spoken:
            deadline = time.monotonic() + idle_seconds
            self.store.add_transcript(call_id, "user", spoken)
        replied = assistant_text(event)
        if replied:
            deadline = time.monotonic() + idle_seconds
            self.store.add_transcript(call_id, "assistant", replied)
        if event.get("type") == "response.function_call_arguments.done":
            tool_id = str(event.get("call_id") or "")
            if tool_id and tool_id not in seen_tools:
                seen_tools.add(tool_id)
                await self._tool(call_id, voice, event)
        return deadline

    async def _tool(self, call_id: str, voice: VoiceClient, event: dict) -> None:
        name = str(event.get("name") or "")
        raw_args = event.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            arguments = {}
        try:
            result = await self.webhooks.post(
                {
                    "event": "tool",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
            await voice.function_result(str(event.get("call_id")), result)
        except Exception:
            log.info("tool relay failed for %s", call_id)

    async def _finish(
        self,
        call_id: str,
        tenant: str,
        voice: VoiceClient | None,
        questions: list[str],
        reason: str,
    ) -> None:
        self.store.update_call(call_id, status="wrapping_up")
        lines = self.store.transcript(call_id)
        summary = ""
        if voice is not None:
            try:
                await voice.request_summary(summary_prompt(questions))
                summary = await _collect_summary(voice, timeout=4)
            except Exception:
                log.info("summary request failed for %s", call_id)
        summary = parse_summary(summary, questions, lines)
        self.store.update_call(
            call_id,
            status="completed",
            end_reason=reason,
            summary=summary,
            ended_at=utc_iso(),
        )
        row = self.store.get_call(tenant, call_id)
        try:
            await self.webhooks.post(
                {
                    "event": "call.completed",
                    "call_id": call_id,
                    "status": "completed",
                    "end_reason": reason,
                    "summary": summary,
                    "direction": (row or {}).get("direction") or "outbound",
                    "destination": row["destination"] if row else "",
                }
            )
        except Exception:
            log.info("completion webhook failed for %s", call_id)

    async def _say_goodbye(
        self,
        call_id: str,
        voice: VoiceClient,
        control: Control,
        max_at: float,
    ) -> bool:
        if control.reason == "completed":
            control.reason = "max_duration" if time.monotonic() >= max_at else "idle"
        if control.reason not in {"idle", "max_duration", "hangup"}:
            return False
        try:
            await voice.force_message(GOODBYE)
            self.store.add_transcript(call_id, "assistant", GOODBYE)
        except Exception:
            log.info("goodbye skipped for %s", call_id)
        return True

    def _new_audio(
        self, call_id: str, devices: tuple[str, str] | None = None
    ) -> MemoryAudio | PulseAudio | AlsaLoop:
        if self.settings.audio_mode == "memory":
            return MemoryAudio()
        if self.settings.audio_mode == "alsa":
            if devices is not None and devices[0].startswith("hw:Loopback,"):
                substream = int(devices[0].rsplit(",", 1)[-1])
                return AlsaLoop(call_id, substream=substream)
            return AlsaLoop(call_id)
        if devices is None:
            return PulseAudio(call_id)
        return PulseAudio(
            call_id,
            play_name=devices[0],
            mic_name=devices[1],
            manage_sinks=False,
        )

    async def _open_sip(
        self, creds: Credentials, play: str, mic: str
    ) -> tuple[SipClient, object | None]:
        if self.settings.sip_ctrl:
            host, _, port = self.settings.sip_ctrl.partition(":")
            client = SipClient(host, int(port))
            await client.connect()
            return client, None
        driver = "pulse" if self.settings.audio_mode == "pulse" else "alsa"
        process = await start_baresip(
            work_root=self.settings.data_dir / "run",
            user=creds.sip_user,
            password=creds.sip_password,
            server=creds.sip_server,
            did=creds.did,
            play_device=play,
            mic_device=mic,
            regint=0,
            audio_driver=driver,
        )
        client = SipClient("127.0.0.1", process.port)
        await client.connect()
        return client, process

    def _log_task(self, call_id: str, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("call task %s ended with %s", call_id, type(exc).__name__)


def audible(pcm: bytes, threshold: int = 256) -> bool:
    """True when 16-bit PCM is louder than silence. Zeros must not hold the line open."""
    if len(pcm) < 2:
        return False
    for offset in range(0, len(pcm) - 1, 2):
        sample = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        if sample <= -threshold or sample >= threshold:
            return True
    return False


async def _wait_established(sip: SipClient, control: Control) -> dict:
    wait = asyncio.create_task(sip.wait_type("CALL_ESTABLISHED", "CALL_CLOSED", timeout=20))
    stop = asyncio.create_task(control.stop.wait())
    done, pending = await asyncio.wait({wait, stop}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if wait in done:
        return wait.result()
    if stop in done:
        return {"type": "CANCELLED"}
    return {"type": "EOF"}


def _devices(audio: MemoryAudio | PulseAudio | AlsaLoop, call_id: str) -> tuple[str, str]:
    if isinstance(audio, AlsaLoop):
        return audio.baresip_device, audio.baresip_device
    if isinstance(audio, PulseAudio):
        return audio.play_name, audio.mic_name
    safe = "".join(ch for ch in call_id if ch.isalnum())[:12]
    return f"mem{safe}play", f"mem{safe}mic"


def _recorder(settings: Settings, call_id: str):
    if not settings.recordings:
        return None
    directory = settings.data_dir / "recordings"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{call_id}.pcm"
    return path.open("wb")


async def _collect_summary(voice: VoiceClient, timeout: float) -> str:
    last = ""
    json_hit = ""
    try:
        async with asyncio.timeout(timeout):
            while True:
                event = await voice.next_event()
                if event.get("type") == "_closed":
                    break
                text = assistant_text(event)
                if text:
                    last = text
                    if text.lstrip().startswith("{"):
                        json_hit = text
                if event.get("type") == "response.done" and json_hit:
                    return json_hit
    except TimeoutError:
        return json_hit or last
    return json_hit or last


async def probe_register(
    settings: Settings, creds: Credentials, timeout: float = 8
) -> tuple[bool, str]:
    if not creds.sip_ready():
        return False, "sip_not_configured"
    client: SipClient | None = None
    process = None
    try:
        if settings.sip_ctrl:
            host, _, port = settings.sip_ctrl.partition(":")
            client = SipClient(host, int(port))
            await client.connect()
        else:
            if settings.audio_mode == "pulse":
                play, mic, driver = "probeplay", "probemic", "pulse"
            else:
                play = mic = baresip_pcm(7)
                driver = "alsa"
            process = await start_baresip(
                work_root=settings.data_dir / "run",
                user=creds.sip_user,
                password=creds.sip_password,
                server=creds.sip_server,
                did=creds.did,
                play_device=play,
                mic_device=mic,
                regint=60,
                audio_driver=driver,
            )
            client = SipClient("127.0.0.1", process.port)
            await client.connect()
        await client.command("register")
        status = await confirm_registration(client, timeout)
        if status == "ok":
            return True, "registered"
        if status == "pending":
            return False, "register_pending"
        if status == "closed":
            return False, "register_closed"
        return False, "register_failed"
    except Exception as exc:
        code = str(exc).strip() or "register_failed"
        if " " in code or len(code) > 40:
            return False, "register_failed"
        return False, code
    finally:
        if client is not None:
            await client.close()
        if process is not None:
            await process.stop()
