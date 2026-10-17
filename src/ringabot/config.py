"""Settings from the environment. Values are never logged."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INSTRUCTIONS = (
    "You are a friendly phone assistant. Keep answers short and natural for a phone call. "
    "If the caller wants to end the call, say goodbye and use the end_call tool."
)
DEFAULT_GREETING = (
    "Greet the caller. Say you are an AI assistant, not a human, then ask how you can help."
)


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    return int(raw) if raw else default


def _list(env: dict[str, str], name: str) -> list[str]:
    return [p.strip() for p in (env.get(name) or "").split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    xai_api_key: str = ""
    webhook_secret: str = ""
    webhook_bearer: str = ""
    api_token: str = ""
    did: str = ""
    public_url: str = ""
    sip_allowed_cidrs: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8787
    voice: str = "eve"
    instructions: str = DEFAULT_INSTRUCTIONS
    greeting: str = DEFAULT_GREETING
    max_call_seconds: int = 600
    max_concurrent_calls: int = 2
    replay_window_seconds: int = 300
    transfer_targets: dict[str, str] = field(default_factory=dict)
    tools_forward_url: str = ""
    tools_forward_secret: str = ""
    tools_file: str = ""
    tool_timeout_seconds: float = 4.0
    keep_calls: int = 20
    api_base: str = "https://api.x.ai"
    realtime_url: str = "wss://api.x.ai/v1/realtime"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        instructions = (env.get("INSTRUCTIONS") or "").strip()
        ifile = (env.get("INSTRUCTIONS_FILE") or "").strip()
        if not instructions and ifile:
            instructions = Path(ifile).read_text(encoding="utf-8").strip()
        targets: dict[str, str] = {}
        for pair in _list(env, "TRANSFER_TARGETS"):
            if "=" in pair:
                name, uri = pair.split("=", 1)
                targets[name.strip().lower()] = uri.strip()
        return cls(
            xai_api_key=(env.get("XAI_API_KEY") or "").strip(),
            webhook_secret=(env.get("XAI_WEBHOOK_SECRET") or "").strip(),
            webhook_bearer=(env.get("XAI_WEBHOOK_BEARER") or "").strip(),
            api_token=(env.get("API_TOKEN") or "").strip(),
            did=(env.get("DID") or "").strip(),
            public_url=(env.get("PUBLIC_URL") or "").strip().rstrip("/"),
            sip_allowed_cidrs=_list(env, "SIP_ALLOWED_CIDRS"),
            host=(env.get("HOST") or "127.0.0.1").strip(),
            port=_int(env, "PORT", 8787),
            voice=(env.get("VOICE") or "eve").strip(),
            instructions=instructions or DEFAULT_INSTRUCTIONS,
            greeting=(env.get("GREETING") or "").strip() or DEFAULT_GREETING,
            max_call_seconds=_int(env, "MAX_CALL_SECONDS", 600),
            max_concurrent_calls=_int(env, "MAX_CONCURRENT_CALLS", 2),
            replay_window_seconds=_int(env, "WEBHOOK_REPLAY_WINDOW_SECONDS", 300),
            transfer_targets=targets,
            tools_forward_url=(env.get("TOOLS_FORWARD_URL") or "").strip(),
            tools_forward_secret=(env.get("TOOLS_FORWARD_SECRET") or "").strip(),
            tools_file=(env.get("TOOLS_FILE") or "").strip(),
            tool_timeout_seconds=float(env.get("TOOL_TIMEOUT_SECONDS") or 4.0),
            keep_calls=_int(env, "KEEP_CALLS", 20),
            api_base=(env.get("XAI_API_BASE") or "https://api.x.ai").rstrip("/"),
            realtime_url=(env.get("XAI_REALTIME_URL") or "wss://api.x.ai/v1/realtime"),
        )

    @property
    def webhook_url(self) -> str:
        return f"{self.public_url}/xai/webhook" if self.public_url else ""

    def extra_tools(self) -> list[dict]:
        """Function definitions forwarded to TOOLS_FORWARD_URL (JSON list in TOOLS_FILE)."""
        if not self.tools_file:
            return []
        data = json.loads(Path(self.tools_file).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("TOOLS_FILE must hold a JSON list of function definitions")
        return data
