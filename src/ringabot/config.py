"""Environment-only configuration. No built-in credentials."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines. Existing process environment wins."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _raw(name: str) -> str:
    return os.environ.get(name, "").strip()


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number") from exc


def _clamp_int(name: str, default: int, low: int, high: int) -> int:
    return max(low, min(_int(name, default), high))


def _clamp_float(name: str, default: float, low: float, high: float) -> float:
    return max(low, min(_float(name, default), high))


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _raw(name).split(",") if part.strip())


def _mode(name: str) -> str:
    raw = _raw(name).lower()
    if raw in {"", "off", "0", "false", "no"}:
        return "off"
    if raw in {"on", "1", "true", "yes"}:
        return "on"
    raise SystemExit(f"{name} must be on or off")


def _on(name: str, default: bool = False) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    return raw in {"1", "on", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    xai_api_key: str
    xai_voice_model: str
    xai_voice: str
    xai_realtime_url: str
    xai_api_base: str
    voipms_sip_server: str
    voipms_sip_user: str
    sip_username: str
    voipms_sip_password: str
    voipms_did: str
    persona_name: str
    persona_instructions: str
    persona_file: str
    greeting: str
    idle_timeout: float
    max_call_seconds: int
    tools_webhook_url: str
    tools_webhook_secret: str
    webhook_replay_window: int
    webhook_timeout: float
    api_token: str
    master_key: bytes
    public_hostname: str
    inbound_mode: str
    inbound_allow: tuple[str, ...]
    recordings: bool
    retention_days: int
    allowed_destinations: tuple[str, ...]
    max_calls_per_day: int
    max_concurrent_calls: int
    host: str
    port: int
    data_dir: Path
    log_level: str
    rate_limit_per_minute: int
    audio_mode: str
    sip_ctrl: str
    env_file: Path | None

    @property
    def xai_realtime_endpoint(self) -> str:
        base = self.xai_realtime_url.rstrip("/")
        return f"{base}?model={self.xai_voice_model}"


def master_key_from_env() -> bytes:
    raw = _raw("RINGABOT_MASTER_KEY")
    if raw:
        try:
            key = base64.b64decode(raw, validate=True)
        except Exception:
            key = raw.encode("utf-8")
        if len(key) != 32:
            raise SystemExit("RINGABOT_MASTER_KEY must decode to 32 bytes")
        return key
    path = Path(_raw("RINGABOT_MASTER_KEY_FILE") or "/etc/ringabot/master.key")
    if path.is_file():
        key = path.read_bytes()
        if len(key) != 32:
            raise SystemExit(f"{path} must be exactly 32 bytes")
        return key
    raise SystemExit("set RINGABOT_MASTER_KEY or create the master key file")


def resolve_audio_mode(raw: str) -> str:
    """Empty defaults to alsa. pulse falls back when the 1.0.0 package has no pulse.so."""
    mode = raw.lower() if raw else "alsa"
    if mode not in {"pulse", "memory", "alsa"}:
        raise SystemExit("AUDIO_MODE must be alsa, pulse, or memory")
    if mode == "pulse":
        from ringabot.sip import pulse_module_present

        if not pulse_module_present():
            return "alsa"
    return mode


def load_settings() -> Settings:
    destinations = _raw("ALLOWED_DESTINATIONS")
    prefixes = tuple(part.strip() for part in destinations.split(",") if part.strip())
    if not prefixes:
        prefixes = ("+1",)
    audio_mode = resolve_audio_mode(_raw("AUDIO_MODE"))
    inbound = _mode("INBOUND_MODE")
    env_path = _raw("RINGABOT_ENV_FILE")
    return Settings(
        xai_api_key=_raw("XAI_API_KEY"),
        xai_voice_model=_raw("XAI_VOICE_MODEL") or "grok-voice-latest",
        xai_voice=_raw("XAI_VOICE") or "aurora",
        xai_realtime_url=_raw("XAI_REALTIME_URL") or "wss://api.x.ai/v1/realtime",
        xai_api_base=_raw("XAI_API_BASE") or "https://api.x.ai",
        voipms_sip_server=_raw("VOIPMS_SIP_SERVER"),
        voipms_sip_user=_raw("VOIPMS_SIP_USER"),
        sip_username=_raw("SIP_USERNAME"),
        voipms_sip_password=_raw("VOIPMS_SIP_PASSWORD"),
        voipms_did=_raw("VOIPMS_DID"),
        persona_name=_raw("PERSONA_NAME") or "Assistant",
        persona_instructions=_raw("PERSONA_INSTRUCTIONS"),
        persona_file=_raw("PERSONA_FILE"),
        greeting=_raw("GREETING"),
        idle_timeout=_clamp_float("IDLE_TIMEOUT", 25, 0.2, 120),
        max_call_seconds=_clamp_int("MAX_CALL_SECONDS", 600, 30, 1800),
        tools_webhook_url=_raw("TOOLS_WEBHOOK_URL"),
        tools_webhook_secret=_raw("TOOLS_WEBHOOK_SECRET"),
        webhook_replay_window=_clamp_int("WEBHOOK_REPLAY_WINDOW_SECONDS", 300, 30, 900),
        webhook_timeout=_clamp_float("WEBHOOK_TIMEOUT_SECONDS", 2, 0.2, 5),
        api_token=_raw("API_TOKEN"),
        master_key=master_key_from_env(),
        public_hostname=_raw("PUBLIC_HOSTNAME"),
        inbound_mode=inbound,
        inbound_allow=_csv("INBOUND_ALLOW"),
        recordings=_on("RECORDINGS", False),
        retention_days=_int("RETENTION_DAYS", 30),
        allowed_destinations=prefixes,
        max_calls_per_day=_clamp_int("MAX_CALLS_PER_DAY", 20, 1, 500),
        max_concurrent_calls=_clamp_int("MAX_CONCURRENT_CALLS", 4, 1, 4),
        host=_raw("HOST") or "127.0.0.1",
        port=_int("PORT", 8787),
        data_dir=Path(_raw("DATA_DIR") or "./data"),
        log_level=_raw("LOG_LEVEL") or "INFO",
        rate_limit_per_minute=_clamp_int("RATE_LIMIT_PER_MINUTE", 60, 1, 600),
        audio_mode=audio_mode,
        sip_ctrl=_raw("SIP_CTRL"),
        env_file=Path(env_path) if env_path else None,
    )
