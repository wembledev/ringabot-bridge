"""baresip runtime.

Outbound calls each get a config directory and, unless SIP_CTRL points at an
existing control socket, their own baresip process. Inbound answering uses one
long-lived process: regint keeps the Voip.ms registration up and
answermode=auto picks up calls to the DID.

Ubuntu 24.04 ships baresip 1.0.0. That build's ctrl_tcp speaks netstring-wrapped
JSON (not a line of JSON), ``-n`` requires a network interface name, and a
REGISTER challenge is a REGISTER_FAIL event before the 200 OK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ringabot.sip")

MODULE_CANDIDATES = (
    "/usr/lib/baresip/modules",
    "/usr/lib/x86_64-linux-gnu/baresip/modules",
    "/usr/local/lib/baresip/modules",
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CHALLENGE = {"401", "407"}


class SipError(RuntimeError):
    pass


def safe_token(value: str, field: str) -> str:
    if not value or any(ch in value for ch in " \t\r\n;\"'<>"):
        raise SipError(f"unsafe_{field}")
    return value


def detect_module_dir() -> str:
    """Directory of baresip modules. BARESIP_MODULE_PATH overrides detection."""
    override = os.environ.get("BARESIP_MODULE_PATH", "").strip()
    if override:
        if any(ch.isspace() for ch in override):
            raise SipError("unsafe_module_path")
        return override
    for candidate in MODULE_CANDIDATES:
        if Path(candidate).is_dir():
            return candidate
    return MODULE_CANDIDATES[0]


def pulse_module_present() -> bool:
    return (Path(detect_module_dir()) / "pulse.so").is_file()


def account_line(
    *,
    user: str,
    password: str,
    server: str,
    did: str,
    regint: int,
    answermode: str = "manual",
) -> str:
    """SIP user is the sub-account. The DID is only the display name.

    Voip.ms returns 403 when the URI user is the DID. A hand-written account
    with the sub-account as the URI user gets 401, then 200 OK.
    """
    if answermode not in {"manual", "auto"}:
        raise SipError("bad_answermode")
    user = safe_token(user, "sip_user")
    password = safe_token(password, "sip_password")
    server = safe_token(server, "sip_server")
    display = ""
    if did:
        display = f'"{safe_token(did, "did")}" '
    return (
        f"{display}<sip:{user}@{server}>;auth_user={user};auth_pass={password};"
        f"regint={regint};answermode={answermode}"
    )


def write_config(
    directory: Path,
    *,
    user: str,
    password: str,
    server: str,
    did: str,
    ctrl_port: int,
    play_device: str,
    mic_device: str,
    regint: int,
    answermode: str = "manual",
    audio_driver: str = "alsa",
) -> None:
    if audio_driver not in {"alsa", "pulse"}:
        raise SipError("bad_audio_driver")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    if audio_driver == "alsa":
        # Same PCM for play and capture. On snd-aloop, device 0 substream N
        # meets Python on device 1 substream N. The value may contain commas;
        # baresip splits the driver on the first one.
        player = f"audio_player alsa,{play_device}"
        source = f"audio_source alsa,{play_device}"
        audio_module = "module alsa.so"
    else:
        player = f"audio_player pulse,{play_device}"
        source = f"audio_source pulse,{mic_device}.monitor"
        audio_module = "module pulse.so"
    config = "\n".join(
        [
            "sip_listen 0.0.0.0:0",
            f"module_path {detect_module_dir()}",
            player,
            source,
            "audio_srate 8000",
            "audio_channels 1",
            "audio_buffer 20-160",
            f"ctrl_tcp_listen 127.0.0.1:{ctrl_port}",
            "module g711.so",
            audio_module,
            "module account.so",
            "module ctrl_tcp.so",
            "module menu.so",
            "module uuid.so",
            "",
        ]
    )
    _write_private(directory / "config", config)
    _write_private(
        directory / "accounts",
        account_line(
            user=user,
            password=password,
            server=server,
            did=did,
            regint=regint,
            answermode=answermode,
        )
        + "\n",
    )


def dial_uri(digits: str, server: str) -> str:
    return f"sip:{safe_token(digits, 'number')}@{safe_token(server, 'sip_server')}"


def baresip_args(binary: str, directory: Path | str) -> list[str]:
    """1.0.0 treats a bare ``-n`` as a missing interface name and exits."""
    return [binary, "-f", str(directory)]


def encode_ctrl(command: str, params: str = "") -> bytes:
    body = json.dumps(
        {"command": command, "params": params},
        separators=(",", ":"),
    ).encode()
    return f"{len(body)}:".encode() + body + b","


def split_command(line: str) -> tuple[str, str]:
    command, _, params = line.strip().partition(" ")
    return command, params


def drain_frames(buf: bytearray) -> list[bytes]:
    """Pull netstrings and newline-delimited JSON out of a TCP buffer.

    1.0.0 ctrl_tcp frames are ``len:json,`` with no newline. The fake UA and
    older experiments still emit one JSON object per line. Incomplete input
    stays in ``buf``.
    """
    frames: list[bytes] = []
    while buf:
        if buf[0] in b" \t\r\n":
            del buf[0]
            continue
        if buf[0] == ord("{"):
            nl = buf.find(b"\n")
            if nl < 0:
                if len(buf) > 65536:
                    raise ValueError("line_too_long")
                break
            if nl > 8192:
                raise ValueError("line_too_long")
            frames.append(bytes(buf[:nl]))
            del buf[: nl + 1]
            continue
        if not chr(buf[0]).isdigit():
            raise ValueError("bad_frame")
        colon = buf.find(b":", 0, 10)
        if colon < 0:
            if len(buf) > 10:
                raise ValueError("bad_frame")
            break
        length_text = bytes(buf[:colon])
        if not length_text.isdigit() or (len(length_text) > 1 and length_text[:1] == b"0"):
            raise ValueError("bad_frame")
        length = int(length_text)
        if length > 65536:
            raise ValueError("frame_too_long")
        end = colon + 1 + length
        if len(buf) < end + 1:
            break
        if buf[end] != ord(","):
            raise ValueError("bad_netstring")
        frames.append(bytes(buf[colon + 1 : end]))
        del buf[: end + 1]
    return frames


def classify_registration(event: dict) -> str | None:
    """ok, challenge, fail, or closed. 401 and 407 are digest challenges."""
    kind = event.get("type")
    if kind == "REGISTER_OK":
        return "ok"
    if kind == "REGISTER_FAIL":
        param = str(event.get("param") or "")
        code = param.split(" ", 1)[0]
        if code in _CHALLENGE:
            return "challenge"
        return "fail"
    if kind in {"EOF", "ERROR"}:
        return "closed"
    return None


def registration_from_reginfo(event: dict) -> str | None:
    """Read menu ``reginfo`` output. Events emitted before ctrl connects are dropped."""
    if event.get("response") is not True:
        return None
    data = event.get("data")
    if not isinstance(data, str) or not data:
        return None
    text = _ANSI.sub("", data)
    if "ERR" in text:
        return "fail"
    if re.search(r"\bOK\b", text):
        return "ok"
    if "zzz" in text:
        return "pending"
    return None


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SipClient:
    """ctrl_tcp client.

    Commands are netstring JSON ``{"command","params"}``, which 1.0.0 accepts.
    Events are netstring JSON with boolean ``event`` and string ``type``, or a
    newline JSON object from the test double.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self._reader is not None
        buf = bytearray()
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    if buf:
                        await self._emit_line(bytes(buf))
                    await self.events.put({"type": "EOF"})
                    return
                buf.extend(chunk)
                if len(buf) > 1_000_000:
                    await self.events.put({"type": "EOF"})
                    return
                try:
                    frames = drain_frames(buf)
                except ValueError:
                    await self.events.put({"type": "EOF"})
                    return
                for frame in frames:
                    await self._emit_line(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sip read failed")
            await self.events.put({"type": "EOF"})

    async def _emit_line(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        if text.startswith("{"):
            try:
                await self.events.put(json.loads(text))
                return
            except json.JSONDecodeError:
                pass
        await self.events.put({"type": "RAW", "line": text})

    async def command(self, line: str) -> None:
        if self._writer is None:
            raise SipError("sip_not_connected")
        command, params = split_command(line)
        self._writer.write(encode_ctrl(command, params))
        await self._writer.drain()

    async def wait_type(self, *types: str, timeout: float) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if event.get("type") in types or event.get("type") in {"ERROR", "EOF"}:
                    return event

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
        writer = self._writer
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def wait_registration(client: SipClient, timeout: float) -> str:
    """Return ok, fail, pending, or closed.

    A 401/407 REGISTER_FAIL is the digest challenge. Keep waiting for 200.
    Timeout is pending: the process is still up, and the OK event may already
    have been dropped because ctrl_tcp only sends events to a connected client.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "pending"
        try:
            event = await asyncio.wait_for(client.events.get(), timeout=remaining)
        except TimeoutError:
            return "pending"
        status = classify_registration(event)
        if status == "challenge":
            continue
        if status:
            return status


async def confirm_registration(client: SipClient, timeout: float) -> str:
    """Event loop first, then one ``reginfo`` if the OK was missed."""
    status = await wait_registration(client, timeout)
    if status != "pending":
        return status
    try:
        await client.command("reginfo")
    except Exception:
        return "pending"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "pending"
        try:
            event = await asyncio.wait_for(client.events.get(), timeout=remaining)
        except TimeoutError:
            return "pending"
        status = classify_registration(event)
        if status == "challenge":
            continue
        if status:
            return status
        info = registration_from_reginfo(event)
        if info in {"ok", "fail"}:
            return info
        if info == "pending":
            return "pending"


@dataclass
class BaresipProcess:
    directory: Path
    port: int
    process: asyncio.subprocess.Process

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
        shutil.rmtree(self.directory, ignore_errors=True)


async def start_baresip(
    *,
    work_root: Path,
    user: str,
    password: str,
    server: str,
    did: str,
    play_device: str,
    mic_device: str,
    regint: int,
    answermode: str = "manual",
    audio_driver: str = "alsa",
) -> BaresipProcess:
    binary = shutil.which("baresip")
    if binary is None:
        raise SipError("baresip_not_installed")
    port = free_port()
    directory = work_root / f"baresip-{port}"
    write_config(
        directory,
        user=user,
        password=password,
        server=server,
        did=did,
        ctrl_port=port,
        play_device=play_device,
        mic_device=mic_device,
        regint=regint,
        answermode=answermode,
        audio_driver=audio_driver,
    )
    process: asyncio.subprocess.Process | None = None
    try:
        # stderr is discarded. The account file holds the SIP password, and a
        # dumped startup log can repeat it.
        process = await asyncio.create_subprocess_exec(
            *baresip_args(binary, directory),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise SipError("baresip_exited")
            try:
                _probe_reader, probe_writer = await asyncio.open_connection("127.0.0.1", port)
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.05)
                continue
            probe_writer.close()
            await probe_writer.wait_closed()
            return BaresipProcess(directory=directory, port=port, process=process)
        raise SipError("baresip_ctrl_timeout")
    except Exception:
        if process is not None and process.returncode is None:
            process.kill()
        shutil.rmtree(directory, ignore_errors=True)
        raise
