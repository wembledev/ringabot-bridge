from __future__ import annotations

import base64

import httpx
import pytest
from tests.harness import FakeGrokServer, FakeSipServer

from ringabot.api import create_app
from ringabot.config import load_settings
from ringabot.store import Store

ENV_KEYS = [
    "XAI_API_KEY",
    "XAI_VOICE_MODEL",
    "XAI_VOICE",
    "XAI_REALTIME_URL",
    "XAI_API_BASE",
    "VOIPMS_SIP_SERVER",
    "VOIPMS_SIP_USER",
    "SIP_USERNAME",
    "VOIPMS_SIP_PASSWORD",
    "VOIPMS_DID",
    "PERSONA_NAME",
    "PERSONA_INSTRUCTIONS",
    "PERSONA_FILE",
    "GREETING",
    "IDLE_TIMEOUT",
    "MAX_CALL_SECONDS",
    "TOOLS_WEBHOOK_URL",
    "TOOLS_WEBHOOK_SECRET",
    "WEBHOOK_REPLAY_WINDOW_SECONDS",
    "WEBHOOK_TIMEOUT_SECONDS",
    "API_TOKEN",
    "RINGABOT_MASTER_KEY",
    "RINGABOT_MASTER_KEY_FILE",
    "PUBLIC_HOSTNAME",
    "INBOUND_MODE",
    "INBOUND_ALLOW",
    "RECORDINGS",
    "RETENTION_DAYS",
    "ALLOWED_DESTINATIONS",
    "MAX_CALLS_PER_DAY",
    "MAX_CONCURRENT_CALLS",
    "HOST",
    "PORT",
    "DATA_DIR",
    "LOG_LEVEL",
    "RATE_LIMIT_PER_MINUTE",
    "AUDIO_MODE",
    "BARESIP_MODULE_PATH",
    "SIP_CTRL",
    "RINGABOT_ENV_FILE",
]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RINGABOT_MASTER_KEY", base64.b64encode(b"0" * 32).decode())
    monkeypatch.setenv("API_TOKEN", "placeholder-token-value")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "placeholder-xai-key")
    monkeypatch.setenv("VOIPMS_SIP_SERVER", "sip.example.com")
    monkeypatch.setenv("VOIPMS_SIP_USER", "example-user")
    monkeypatch.setenv("VOIPMS_SIP_PASSWORD", "placeholder-sip-pass")
    monkeypatch.setenv("VOIPMS_DID", "+15555550100")
    monkeypatch.setenv("AUDIO_MODE", "memory")
    monkeypatch.setenv("ALLOWED_DESTINATIONS", "+1")
    monkeypatch.setenv("MAX_CALLS_PER_DAY", "20")
    monkeypatch.setenv("MAX_CONCURRENT_CALLS", "4")
    monkeypatch.setenv("IDLE_TIMEOUT", "30")
    monkeypatch.setenv("MAX_CALL_SECONDS", "60")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("TOOLS_WEBHOOK_SECRET", "placeholder-hook-secret")
    monkeypatch.setenv("PERSONA_NAME", "Example Assistant")
    monkeypatch.setenv("GREETING", "I have a short question.")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    return load_settings()


@pytest.fixture
def store(settings):
    database = Store(settings.data_dir / "ringabot.db", settings.master_key)
    yield database
    database.close()


@pytest.fixture
async def fakes():
    sip = FakeSipServer()
    grok = FakeGrokServer()
    await sip.start()
    await grok.start()
    yield sip, grok
    await grok.stop()
    await sip.stop()


@pytest.fixture
async def app(settings, store, fakes, monkeypatch):
    sip, grok = fakes
    monkeypatch.setenv("SIP_CTRL", f"127.0.0.1:{sip.port}")
    monkeypatch.setenv("XAI_REALTIME_URL", grok.url)
    monkeypatch.setenv("IDLE_TIMEOUT", "0.4")
    live = load_settings()
    application = create_app(live, store)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        application.state.client = client
        yield application


@pytest.fixture
def auth_header():
    return {"authorization": "Bearer placeholder-token-value"}
