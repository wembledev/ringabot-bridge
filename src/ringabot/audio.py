"""Per-call audio paths.

Ubuntu 24.04's baresip 1.0.0 package has alsa.so and no pulse.so. Production
uses the kernel snd-aloop device: baresip plays and captures on
``hw:Loopback,0,N`` and this process runs arecord/aplay on ``hw:Loopback,1,N``.
That is one extra copy in the kernel, no Pulse daemon, and no rebuilt baresip.
aubridge stays inside baresip, and aufile is a WAV file, so neither reaches
the realtime socket. Substream 0 is the inbound registrar. Outbound calls take
1 through 7.

Tests and machines without a sound device use the in-process memory path.
Pulse remains available when pulse.so is actually installed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
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
    def __init__(
        self,
        call_id: str,
        *,
        play_name: str | None = None,
        mic_name: str | None = None,
        manage_sinks: bool = True,
    ) -> None:
        self.call_id = call_id
        if play_name and mic_name:
            self.play_name, self.mic_name = play_name, mic_name
        else:
            self.play_name, self.mic_name = sink_names(call_id)
        self.manage_sinks = manage_sinks
        self._modules: list[str] = []
        self._parec: asyncio.subprocess.Process | None = None
        self._pacat: asyncio.subprocess.Process | None = None
        self._stdout: asyncio.StreamReader | None = None

    async def start(self) -> None:
        if self.manage_sinks:
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
                    detail = stderr.decode("utf-8", "replace")[:200] or "pactl failed"
                    raise AudioUnavailable(detail)
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
        if not self.manage_sinks:
            self._modules.clear()
            return
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


_alsa_lock = threading.Lock()
_alsa_used: set[int] = set()


def baresip_pcm(substream: int) -> str:
    return f"hw:Loopback,0,{substream}"


def python_pcm(substream: int) -> str:
    return f"hw:Loopback,1,{substream}"


def claim_substream() -> int:
    """Outbound substreams. 0 stays free for the inbound registrar."""
    with _alsa_lock:
        for number in range(1, 8):
            if number not in _alsa_used:
                _alsa_used.add(number)
                return number
    raise AudioUnavailable("alsa_substreams_full")


def release_substream(number: int) -> None:
    with _alsa_lock:
        _alsa_used.discard(number)


class AlsaLoop:
    """8 kHz S16 mono duplex across one snd-aloop substream."""

    def __init__(self, call_id: str, substream: int | None = None) -> None:
        self.call_id = call_id
        self.owns = substream is None
        self.substream = claim_substream() if substream is None else substream
        self.baresip_device = baresip_pcm(self.substream)
        self.play_name = python_pcm(self.substream)
        self.mic_name = python_pcm(self.substream)
        self._capture: asyncio.subprocess.Process | None = None
        self._playback: asyncio.subprocess.Process | None = None
        self._stdout: asyncio.StreamReader | None = None

    async def start(self) -> None:
        if shutil.which("arecord") is None or shutil.which("aplay") is None:
            raise AudioUnavailable("alsa-utils is not installed")
        common = [
            "-D",
            self.play_name,
            "-f",
            "S16_LE",
            "-r",
            str(SAMPLE_RATE),
            "-c",
            "1",
            "-t",
            "raw",
            "--period-time=20000",
            "--buffer-time=80000",
        ]
        self._capture = await asyncio.create_subprocess_exec(
            "arecord",
            *common,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._stdout = self._capture.stdout
        self._playback = await asyncio.create_subprocess_exec(
            "aplay",
            *common,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def stop(self) -> None:
        await _terminate(self._capture)
        await _terminate(self._playback)
        if self.owns:
            release_substream(self.substream)

    async def read_remote(self) -> bytes | None:
        if self._stdout is None:
            return None
        chunk = await self._stdout.read(320)
        if not chunk:
            return None
        return chunk

    async def write_remote(self, pcm: bytes) -> None:
        if self._playback is None or self._playback.stdin is None:
            return
        self._playback.stdin.write(pcm)
        await self._playback.stdin.drain()


async def _terminate(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        proc.kill()
