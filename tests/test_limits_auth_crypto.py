import base64

import pytest

from ringabot.auth import StaticBearerAuth, ct_eq
from ringabot.crypto import decrypt, encrypt
from ringabot.limits import LimitError, normalize_destination


def test_constant_time_compare():
    assert ct_eq(b"placeholder-token-value", b"placeholder-token-value")
    assert not ct_eq(b"placeholder-token-value", b"placeholder-token-valu!")
    assert not ct_eq(b"short", b"placeholder-token-value")


def test_bearer_auth_rejects_near_miss():
    auth = StaticBearerAuth("placeholder-token-value")
    assert auth.authenticate("Bearer placeholder-token-value") == "local"
    assert auth.authenticate("Bearer placeholder-token-valu!") is None
    assert auth.authenticate("placeholder-token-value") is None
    assert auth.authenticate(None) is None
    assert StaticBearerAuth("").authenticate("Bearer placeholder-token-value") is None


def test_allowlist_and_emergency():
    ok = normalize_destination("+1 555-555-0100", ("+1",))
    assert ok.e164 == "+15555550100"
    with pytest.raises(LimitError) as denied:
        normalize_destination("+442075550100", ("+1",))
    assert denied.value.code == "destination_not_allowed"
    with pytest.raises(LimitError) as emergency:
        normalize_destination("+1911", ("+1",))
    assert emergency.value.code == "emergency_not_allowed"
    with pytest.raises(LimitError):
        normalize_destination("5555550100", ("+1",))


def test_abuse_settings_are_clamped(monkeypatch, tmp_path):
    from ringabot.config import load_settings

    monkeypatch.setenv("RINGABOT_MASTER_KEY", base64.b64encode(b"1" * 32).decode())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEBHOOK_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WEBHOOK_REPLAY_WINDOW_SECONDS", "10")
    monkeypatch.setenv("MAX_CONCURRENT_CALLS", "100")
    monkeypatch.setenv("MAX_CALLS_PER_DAY", "0")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    monkeypatch.setenv("IDLE_TIMEOUT", "0")
    settings = load_settings()
    assert settings.webhook_timeout == 5
    assert settings.webhook_replay_window == 30
    assert settings.max_concurrent_calls == 4
    assert settings.max_calls_per_day == 1
    assert settings.rate_limit_per_minute == 1
    assert settings.idle_timeout == 0.2


def test_database_files_stay_private(tmp_path):
    from ringabot.store import Store

    store = Store(tmp_path / "ringabot.db", b"2" * 32)
    store.put_setting("local", "persona_name", "Example")
    store.close()
    for suffix in ("", "-wal", "-shm"):
        candidate = tmp_path / f"ringabot.db{suffix}"
        if candidate.exists():
            assert candidate.stat().st_mode & 0o777 == 0o600


def test_update_call_rejects_unknown_columns(tmp_path):
    from ringabot.store import Store

    store = Store(tmp_path / "ringabot.db", b"3" * 32)
    store.insert_call(
        call_id="call_abc",
        tenant="local",
        destination="+15555550100",
        briefing="hello",
        questions=[],
        max_seconds=60,
    )
    with pytest.raises(ValueError):
        store.update_call("call_abc", not_a_column="nope")
    store.close()


def test_secret_box_roundtrip_and_binding():
    key = b"0" * 32
    nonce, ciphertext = encrypt(key, b"placeholder-sip-pass", b"local:sip_password")
    assert b"placeholder-sip-pass" not in ciphertext
    assert decrypt(key, nonce, ciphertext, b"local:sip_password") == b"placeholder-sip-pass"
    with pytest.raises(Exception):
        decrypt(key, nonce, ciphertext, b"other:sip_password")
    assert len(base64.b64encode(key)) > 0
