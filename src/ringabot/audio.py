"""Per-call audio paths.

Production uses a PulseAudio null-sink pair at 8 kHz so the bridge only
moves telephone audio. Tests and machines without a sound device use the
in-process memory path. No extra media server.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

log = logging.getLogger("ringabot.audio")

SAMPLE_RATE = 8000


class AudioUnavailable(RuntimeError):
    pass


@dataclass
class PulseSinks:
    play: str
    mic: str
    modules: list[str]


def sink_names(call_id: str) -> tuple[str, str]:
    safe = "".join(ch for ch in call_id if ch.isalnum())[:12]
    return f"rb{safe}play", f"rb{safe}mic"


def pulse_commands(play: str, mic: str) -> list[list[str]]:
    return [
        [
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={play}",
            f"sink_properties=device.description={play}",
        ],
        [
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={mic}",
            f"sink_properties=device.description={mic}",
        ],
    ]


class MemoryAudio:
    """Loopback used when AUDIO_MODE=memory. Remote audio is pushed by the caller."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.played = bytearray()
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        await self._incoming.put(None)

    async def push_remote(self, pcm: bytes) -> None:
        await self._incoming.put(pcm)

    async def read_remote(self) -> bytes | None:
        return await self._incoming.get()

    async def write_remote(self, pcm: bytes) -> None:
        if pcm:
            self.played.extend(pcm)


class PulseAudio:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.play_name, self.mic_name = sink_names(call_id)
        self._modules: list[str] = []
        self._parec: asyncio.subprocess.Process | None = None
        self._pacat: asyncio.subprocess.Process | None = None
        self._stdout: asyncio.StreamReader | None = None

    async def start(self) -> None:
        if shutil.which("pactl") is None:
            raise AudioUnavailable("pactl is not installed")
        for command in pulse_commands(self.play_name, self.mic_name):
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise AudioUnavailable(stderr.decode("utf-8", "replace")[:200] or "pactl failed")
            module_id = stdout.decode().strip()
            if module_id:
                self._modules.append(module_id)
        self._parec = await asyncio.create_subprocess_exec(
            "parec",
            "-d",
            f"{self.play_name}.monitor",
            "--format=s16le",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            "--latency-msec=20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._stdout = self._parec.stdout
        self._pacat = await asyncio.create_subprocess_exec(
            "pacat",
            "-d",
            self.mic_name,
            "--format=s16le",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            "--latency-msec=20",
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def stop(self) -> None:
        await _terminate(self._parec)
        await _terminate(self._pacat)
        for module_id in self._modules:
            proc = await asyncio.create_subprocess_exec(
                "pactl",
                "unload-module",
                module_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        self._modules.clear()

    async def read_remote(self) -> bytes | None:
        if self._stdout is None:
            return None
        chunk = await self._stdout.read(320)
        if not chunk:
            return None
        return chunk

    async def write_remote(self, pcm: bytes) -> None:
        if self._pacat is None or self._pacat.stdin is None:
            return
        self._pacat.stdin.write(pcm)
        await self._pacat.stdin.drain()


async def _terminate(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        proc.kill()
