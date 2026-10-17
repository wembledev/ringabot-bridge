"""baresip runtime.

Outbound calls each get a config directory and, unless SIP_CTRL points at an
existing control socket, their own baresip process. Inbound answering uses one
long-lived process: regint keeps the Voip.ms registration up and
answermode=auto picks up calls to the DID.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ringabot.sip")


class SipError(RuntimeError):
    pass


def safe_token(value: str, field: str) -> str:
    if not value or any(ch in value for ch in " \t\r\n;\"'<>"):
        raise SipError(f"unsafe_{field}")
    return value


def account_line(
    *,
    user: str,
    password: str,
    server: str,
    did: str,
    regint: int,
    answermode: str = "manual",
) -> str:
    if answermode not in {"manual", "auto"}:
        raise SipError("bad_answermode")
    user = safe_token(user, "sip_user")
    password = safe_token(password, "sip_password")
    server = safe_token(server, "sip_server")
    identity = safe_token(did.lstrip("+"), "did") if did else user
    return (
        f"<sip:{identity}@{server}>;auth_user={user};auth_pass={password};"
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
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    config = "\n".join(
        [
            "sip_listen 127.0.0.1:0",
            f"audio_player pulse,{play_device}",
            f"audio_source pulse,{mic_device}.monitor",
            "audio_srate 8000",
            "audio_channels 1",
            "audio_buffer 20-160",
            f"ctrl_tcp_listen 127.0.0.1:{ctrl_port}",
            "module stdio.so",
            "module g711.so",
            "module pulse.so",
            "module account.so",
            "module ctrl_tcp.so",
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


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SipClient:
    """Line protocol: one JSON object or a plain command per line.

    Plain commands (``dial``, ``hangup``, ``register``) are what baresip's
    ctrl_tcp module accepts. The fake UA and baresip both emit JSON events
    with a ``type`` field (CALL_ESTABLISHED, CALL_CLOSED, REGISTER_OK, ...).
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
                if len(buf) > 65536 and b"\n" not in buf:
                    await self.events.put({"type": "EOF"})
                    return
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)
                    if len(line) > 8192:
                        await self.events.put({"type": "EOF"})
                        return
                    await self._emit_line(line)
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
        self._writer.write((line.strip() + "\n").encode())
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
    )
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "-f",
            str(directory),
            "-n",
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
