"""baresip 1.0.0 config, ctrl_tcp framing, and a local registrar."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from ringabot.config import load_env_file, resolve_audio_mode
from ringabot.envfile import quote_env
from ringabot.sip import (
    SipClient,
    SipError,
    baresip_args,
    classify_registration,
    confirm_registration,
    detect_module_dir,
    drain_frames,
    encode_ctrl,
    registration_from_reginfo,
    start_baresip,
    wait_registration,
)


def test_args_do_not_pass_a_bare_interface_flag():
    args = baresip_args("baresip", "/tmp/ringabot-cfg")
    assert args == ["baresip", "-f", "/tmp/ringabot-cfg"]


def test_netstring_round_trip_and_partial_reads():
    frame = encode_ctrl("register", "")
    buf = bytearray(frame[:4])
    assert drain_frames(buf) == []
    buf.extend(frame[4:])
    frames = drain_frames(buf)
    assert frames == [b'{"command":"register","params":""}']
    assert buf == bytearray()
    mixed = bytearray(b'{"type":"REGISTER_OK","event":true}\n' + frame)
    decoded = drain_frames(mixed)
    assert b'"event":true' in decoded[0]
    assert b'"command":"register"' in decoded[1]


def test_digest_challenge_is_not_a_terminal_failure():
    challenge = {"type": "REGISTER_FAIL", "param": "401 Unauthorized", "event": True}
    assert classify_registration(challenge) == "challenge"
    assert classify_registration({"type": "REGISTER_FAIL", "param": "403 Forbidden"}) == "fail"
    assert classify_registration({"type": "REGISTER_OK", "param": "200 OK"}) == "ok"
    text = "\x1b[32mOK\x1b[0m (60)"
    assert registration_from_reginfo({"response": True, "ok": True, "data": text}) == "ok"


def test_env_values_round_trip_with_spaces(tmp_path: Path, monkeypatch):
    quoted = quote_env("Example Assistant")
    assert quoted == '"Example Assistant"'
    path = tmp_path / "ringabot.env"
    path.write_text(f"PERSONA_NAME={quoted}\n", encoding="utf-8")
    monkeypatch.delenv("PERSONA_NAME", raising=False)
    load_env_file(path)
    assert os.environ["PERSONA_NAME"] == "Example Assistant"
    with pytest.raises(ValueError):
        quote_env('bad "quote"')


def test_missing_pulse_module_uses_alsa(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BARESIP_MODULE_PATH", str(tmp_path))
    assert resolve_audio_mode("pulse") == "alsa"
    assert resolve_audio_mode("") == "alsa"
    assert resolve_audio_mode("memory") == "memory"
    (tmp_path / "pulse.so").write_bytes(b"")
    assert resolve_audio_mode("pulse") == "pulse"


@pytest.mark.asyncio
async def test_wait_registration_ignores_401_then_accepts_200():
    client = SipClient("127.0.0.1", 1)
    await client.events.put({"type": "REGISTER_FAIL", "param": "401 Unauthorized"})
    await client.events.put({"type": "REGISTER_OK", "param": "200 OK"})
    assert await wait_registration(client, 1) == "ok"
    assert await wait_registration(SipClient("127.0.0.1", 1), 0.05) == "pending"


@pytest.mark.asyncio
async def test_reginfo_confirms_a_missed_register_ok():
    client = SipClient("127.0.0.1", 9)

    async def connect():
        return None

    client.connect = connect  # type: ignore[method-assign]

    async def command(line: str) -> None:
        assert line == "reginfo"
        await client.events.put({"response": True, "ok": True, "data": "OK (60)"})

    client.command = command  # type: ignore[method-assign]
    assert await confirm_registration(client, 0.05) == "ok"


def _distro_baresip() -> bool:
    if shutil.which("baresip") is None:
        return False
    try:
        root = Path(detect_module_dir())
    except SipError:
        return False
    return (root / "ctrl_tcp.so").is_file() and (root / "account.so").is_file()


class _Registrar(asyncio.DatagramProtocol):
    """First REGISTER is 401. The next one, with Authorization, is 200 OK."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.unauth = 0
        self.auth = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        text = data.decode("utf-8", "replace")
        if not text.startswith("REGISTER"):
            return
        head = text.split("\r\n\r\n", 1)[0]
        picked: dict[str, list[tuple[str, str]]] = {
            "via": [],
            "from": [],
            "to": [],
            "call-id": [],
            "cseq": [],
            "contact": [],
        }
        authorized = False
        for line in head.split("\r\n")[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            key = name.lower()
            if key == "authorization":
                authorized = True
            if key in picked:
                picked[key].append((name, value.strip()))
        if authorized:
            self.auth += 1
            status = "SIP/2.0 200 OK"
        else:
            self.unauth += 1
            status = "SIP/2.0 401 Unauthorized"
        lines = [status]
        for key in ("via", "from"):
            for name, value in picked[key]:
                lines.append(f"{name}: {value}")
        if picked["to"]:
            name, value = picked["to"][0]
            if "tag=" not in value.lower():
                value = f"{value};tag=ringabot"
            lines.append(f"{name}: {value}")
        for key in ("call-id", "cseq"):
            for name, value in picked[key]:
                lines.append(f"{name}: {value}")
        if authorized:
            for name, value in picked["contact"]:
                lines.append(f"{name}: {value}")
            lines.append("Expires: 60")
        else:
            lines.append('WWW-Authenticate: Digest realm="ringabot", nonce="nonce1", algorithm=MD5')
        lines.extend(["Content-Length: 0", "", ""])
        assert self.transport is not None
        self.transport.sendto("\r\n".join(lines).encode(), addr)


@pytest.mark.asyncio
@pytest.mark.skipif(not _distro_baresip(), reason="baresip 1.0.0 modules are not installed")
async def test_distro_baresip_registers_with_digest(tmp_path: Path):
    loop = asyncio.get_running_loop()
    registrar = _Registrar()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: registrar, local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    process = None
    client = None
    try:
        process = await start_baresip(
            work_root=tmp_path,
            user="example-user",
            password="placeholder-sip-pass",
            server=f"127.0.0.1:{port}",
            did="+15555550100",
            play_device="hw:Loopback,0,7",
            mic_device="hw:Loopback,0,7",
            regint=60,
            audio_driver="alsa",
        )
        cmdline = Path(f"/proc/{process.process.pid}/cmdline").read_bytes().split(b"\0")
        assert b"-n" not in cmdline
        account = next(Path(process.directory).glob("accounts")).read_text(encoding="utf-8")
        assert "<sip:example-user@127.0.0.1:" in account
        assert "sip:15555550100@" not in account
        client = SipClient("127.0.0.1", process.port)
        await client.connect()
        await client.command("register")
        status = await confirm_registration(client, 8)
        assert status == "ok"
        assert registrar.unauth >= 1
        assert registrar.auth >= 1
        await asyncio.sleep(1)
        assert process.process.returncode is None
    finally:
        if client is not None:
            await client.close()
        if process is not None:
            await process.stop()
        transport.close()
