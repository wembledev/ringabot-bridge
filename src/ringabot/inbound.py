"""Inbound answering for the configured Voip.ms DID.

One registered baresip (answermode=auto) picks up calls. Each answered call
uses the same voice session, disclosure, idle and max-length goodbye, summary,
SQLite row, and webhook as an outbound call. A caller allow-list is optional.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from ringabot.audio import pulse_commands
from ringabot.call import CallService
from ringabot.config import Settings
from ringabot.credentials import resolve_credentials
from ringabot.limits import LimitError, normalize_e164
from ringabot.sip import SipClient, start_baresip
from ringabot.store import Store

log = logging.getLogger("ringabot.inbound")

INBOUND_PLAY = "rbinboundplay"
INBOUND_MIC = "rbinboundmic"
_URI = re.compile(r"sip:([^@>;\s]+)", re.IGNORECASE)
_EMPTY_INBOUND = {
    "enabled": False,
    "registered": False,
    "did": "",
    "greeting": "",
    "instructions": "",
    "allow": [],
}


def inbound_enabled(settings: Settings, store: Store, tenant: str) -> bool:
    stored = store.get_setting(tenant, "inbound_enabled")
    if stored == "1":
        return True
    if stored == "0":
        return False
    return settings.inbound_mode == "on"


def allow_list(settings: Settings, store: Store, tenant: str) -> list[str]:
    raw = store.get_setting(tenant, "inbound_allow")
    if raw is None:
        values: list = list(settings.inbound_allow)
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        values = parsed if isinstance(parsed, list) else []
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        try:
            cleaned.append(normalize_e164(item))
        except LimitError:
            continue
    return cleaned


def caller_number(event: dict) -> str:
    raw = ""
    for key in ("peeruri", "peer", "peer_uri", "param", "from"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break
    match = _URI.search(raw)
    if match:
        raw = match.group(1)
    if raw.lower() in {"anonymous", "restricted", "unavailable", ""}:
        return ""
    candidate = raw if raw.startswith("+") else f"+{raw}"
    try:
        return normalize_e164(candidate)
    except LimitError:
        return ""


class InboundService:
    def __init__(self, settings: Settings, store: Store, calls: CallService) -> None:
        self.settings = settings
        self.store = store
        self.calls = calls
        self.tenant = "local"
        self.registered = False
        self.waiting = False
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._token: object = object()

    def enabled(self) -> bool:
        return inbound_enabled(self.settings, self.store, self.tenant)

    def view(self, tenant: str) -> dict:
        creds = resolve_credentials(self.settings, self.store, tenant)
        stored_greeting = self.store.get_setting(tenant, "inbound_greeting")
        greeting = creds.persona.greeting if stored_greeting is None else stored_greeting
        stored_instructions = self.store.get_setting(tenant, "inbound_instructions")
        instructions = (
            creds.persona.instructions if stored_instructions is None else stored_instructions
        )
        return {
            "enabled": inbound_enabled(self.settings, self.store, tenant),
            "registered": self.registered and self.tenant == tenant,
            "did": creds.did,
            "greeting": greeting,
            "instructions": instructions,
            "allow": allow_list(self.settings, self.store, tenant),
        }

    async def configure(self, tenant: str, arguments: dict) -> dict:
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            return {"ok": False, "error": "enabled_invalid"}
        if "greeting" in arguments and arguments["greeting"] is not None:
            greeting = arguments["greeting"]
            if not isinstance(greeting, str):
                return {"ok": False, "error": "greeting_invalid"}
            greeting = greeting.strip()
            if len(greeting) > 500:
                return {"ok": False, "error": "field_too_long"}
            self.store.put_setting(tenant, "inbound_greeting", greeting)
        if "instructions" in arguments and arguments["instructions"] is not None:
            instructions = arguments["instructions"]
            if not isinstance(instructions, str) or not instructions.strip():
                return {"ok": False, "error": "instructions_invalid"}
            instructions = instructions.strip()
            if len(instructions) > 4000:
                return {"ok": False, "error": "field_too_long"}
            self.store.put_setting(tenant, "inbound_instructions", instructions)
        if "allow" in arguments and arguments["allow"] is not None:
            numbers = _parse_allow(arguments["allow"])
            if isinstance(numbers, str):
                return {"ok": False, "error": numbers}
            self.store.put_setting(tenant, "inbound_allow", json.dumps(numbers))
        self.store.put_setting(tenant, "inbound_enabled", "1" if enabled else "0")
        self.tenant = tenant
        await self.restart()
        return {"ok": True, **self.view(tenant)}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.restart()

    async def restart(self) -> None:
        await self.stop()
        if not self.enabled():
            return
        self._stop = asyncio.Event()
        token = object()
        self._token = token
        self._task = asyncio.create_task(self._loop(token), name="inbound")

    async def stop(self) -> None:
        self._stop.set()
        self._token = object()
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.registered = False
        self.waiting = False

    async def _loop(self, token: object) -> None:
        try:
            while not self._stop.is_set() and self._token is token:
                if not self.enabled() or not self._ready():
                    self.registered = False
                    if await self._pause():
                        return
                    continue
                try:
                    await self._session(token)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("inbound registrar failed")
                    if self._token is token:
                        self.registered = False
                if self._stop.is_set() or self._token is not token:
                    return
                if await self._pause():
                    return
        finally:
            if self._token is token:
                self.registered = False
                self.waiting = False

    def _ready(self) -> bool:
        creds = resolve_credentials(self.settings, self.store, self.tenant)
        return creds.sip_ready() and creds.voice_ready()

    async def _pause(self) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=0.4)
        except TimeoutError:
            return False
        return True

    async def _session(self, token: object) -> None:
        creds = resolve_credentials(self.settings, self.store, self.tenant)
        sip: SipClient | None = None
        baresip = None
        modules: list[str] = []
        try:
            sip, baresip, modules = await self._open(creds)
            if not await self._register(sip) or self._token is not token:
                return
            self.registered = True
            while not self._stop.is_set() and self._token is token:
                event = await self._wait_incoming(sip)
                if event is None:
                    return
                caller = caller_number(event)
                if not self._permit(caller):
                    await self._discard_leg(sip, event)
                    continue
                if event.get("type") != "CALL_ESTABLISHED":
                    established = await self._wait_established(sip)
                    if established is None:
                        return
                    if established.get("type") != "CALL_ESTABLISHED":
                        await self._discard_leg(sip, established)
                        continue
                    if not caller:
                        caller = caller_number(established)
                        if not self._permit(caller):
                            await self._discard_leg(sip, established)
                            continue
                greeting, briefing = self._script()
                devices = self._devices()
                task = await self.calls.accept_inbound(
                    self.tenant,
                    caller=caller,
                    briefing=briefing,
                    greeting=greeting,
                    sip=sip,
                    devices=devices,
                )
                if task is None:
                    await self._reject(sip)
                    await self._wait_type(sip, ("CALL_CLOSED", "EOF"), 2)
                    continue
                try:
                    await task
                finally:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
        finally:
            if self._token is token:
                self.registered = False
                self.waiting = False
            if sip is not None:
                await sip.close()
            if baresip is not None:
                await baresip.stop()
            await unload_pulse_sinks(modules)

    def _permit(self, caller: str) -> bool:
        allowed = allow_list(self.settings, self.store, self.tenant)
        if not allowed:
            return True
        return bool(caller) and caller in allowed

    def _script(self) -> tuple[str, str]:
        creds = resolve_credentials(self.settings, self.store, self.tenant)
        stored_greeting = self.store.get_setting(self.tenant, "inbound_greeting")
        greeting = creds.persona.greeting if stored_greeting is None else stored_greeting
        stored_instructions = self.store.get_setting(self.tenant, "inbound_instructions")
        briefing = (
            creds.persona.instructions if stored_instructions is None else stored_instructions
        )
        return greeting, briefing or "Answer the incoming call."

    def _devices(self) -> tuple[str, str] | None:
        if self.settings.audio_mode == "pulse" and not self.settings.sip_ctrl:
            return (INBOUND_PLAY, INBOUND_MIC)
        return None

    async def _open(self, creds) -> tuple[SipClient, object | None, list[str]]:
        if self.settings.sip_ctrl:
            host, _, port = self.settings.sip_ctrl.partition(":")
            client = SipClient(host, int(port))
            await client.connect()
            return client, None, []
        modules: list[str] = []
        try:
            if self.settings.audio_mode == "pulse":
                modules = await load_pulse_sinks(INBOUND_PLAY, INBOUND_MIC)
            process = await start_baresip(
                work_root=self.settings.data_dir / "run",
                user=creds.sip_user,
                password=creds.sip_password,
                server=creds.sip_server,
                did=creds.did,
                play_device=INBOUND_PLAY,
                mic_device=INBOUND_MIC,
                regint=60,
                answermode="auto",
            )
            client = SipClient("127.0.0.1", process.port)
            await client.connect()
            return client, process, modules
        except Exception:
            await unload_pulse_sinks(modules)
            raise

    async def _register(self, sip: SipClient) -> bool:
        try:
            await sip.command("register")
        except Exception:
            return False
        event = await self._wait_type(sip, ("REGISTER_OK", "REGISTER_FAIL", "EOF"), 8)
        return event is not None and event.get("type") == "REGISTER_OK"

    async def _wait_incoming(self, sip: SipClient) -> dict | None:
        while not self._stop.is_set():
            self.waiting = True
            try:
                event = await self._wait_type(
                    sip, ("CALL_INCOMING", "CALL_ESTABLISHED", "EOF", "ERROR"), 30
                )
            finally:
                self.waiting = False
            if event is None or event.get("type") in {"STOP", "EOF", "ERROR"}:
                return None
            if event.get("type") == "TIMEOUT":
                continue
            return event
        return None

    async def _wait_established(self, sip: SipClient) -> dict | None:
        event = await self._wait_type(sip, ("CALL_ESTABLISHED", "CALL_CLOSED", "EOF"), 2)
        if event is not None and event.get("type") == "CALL_ESTABLISHED":
            return event
        if event is not None and event.get("type") in {"STOP", "EOF"}:
            return None
        if event is not None and event.get("type") == "CALL_CLOSED":
            return event
        if self._stop.is_set():
            return None
        try:
            await sip.command("accept")
        except Exception:
            return None
        followed = await self._wait_type(sip, ("CALL_ESTABLISHED", "CALL_CLOSED", "EOF"), 5)
        if followed is None or followed.get("type") in {"STOP", "EOF", "TIMEOUT"}:
            return None
        return followed

    async def _discard_leg(self, sip: SipClient, event: dict) -> None:
        if event.get("type") not in {"CALL_ESTABLISHED", "CALL_CLOSED"}:
            nxt = await self._wait_type(sip, ("CALL_ESTABLISHED", "CALL_CLOSED", "EOF"), 2)
            if nxt is None or nxt.get("type") in {"STOP", "EOF", "TIMEOUT", "CALL_CLOSED"}:
                if nxt is not None and nxt.get("type") == "CALL_CLOSED":
                    return
                if event.get("type") == "CALL_INCOMING":
                    await self._reject(sip)
                return
        if event.get("type") != "CALL_CLOSED":
            await self._reject(sip)
            await self._wait_type(sip, ("CALL_CLOSED", "EOF"), 2)

    async def _reject(self, sip: SipClient) -> None:
        try:
            await sip.command("hangup")
        except Exception:
            log.info("inbound reject failed")

    async def _wait_type(
        self, sip: SipClient, types: tuple[str, ...], timeout: float
    ) -> dict | None:
        wait = asyncio.create_task(sip.wait_type(*types, timeout=timeout))
        stop = asyncio.create_task(self._stop.wait())
        done, _pending = await asyncio.wait({wait, stop}, return_when=asyncio.FIRST_COMPLETED)
        if wait in done:
            stop.cancel()
            try:
                return wait.result()
            except TimeoutError:
                return {"type": "TIMEOUT"}
        wait.cancel()
        try:
            await wait
        except (asyncio.CancelledError, TimeoutError):
            pass
        return {"type": "STOP"}


def _parse_allow(allow) -> list[str] | str:
    if not isinstance(allow, list) or len(allow) > 32:
        return "allow_invalid"
    numbers: list[str] = []
    for item in allow:
        if not isinstance(item, str):
            return "allow_invalid"
        try:
            number = normalize_e164(item)
        except LimitError as exc:
            return exc.code
        if number not in numbers:
            numbers.append(number)
    return numbers


async def load_pulse_sinks(play: str, mic: str) -> list[str]:
    module_ids: list[str] = []
    for command in pulse_commands(play, mic):
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            await unload_pulse_sinks(module_ids)
            raise RuntimeError("pulse_sink_failed")
        module_id = stdout.decode().strip()
        if module_id:
            module_ids.append(module_id)
    return module_ids


async def unload_pulse_sinks(module_ids: list[str]) -> None:
    for module_id in module_ids:
        proc = await asyncio.create_subprocess_exec(
            "pactl",
            "unload-module",
            module_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


def empty_inbound() -> dict:
    state = dict(_EMPTY_INBOUND)
    state["allow"] = []
    return state
