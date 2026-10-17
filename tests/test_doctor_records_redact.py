import logging
from datetime import timedelta

import pytest

from ringabot.doctor import failed, run_doctor
from ringabot.records import enforce_retention, local_summary
from ringabot.redact import REDACTOR, RedactFilter
from ringabot.store import utc_iso, utc_now


@pytest.mark.asyncio
async def test_doctor_offline_and_injected_probes(settings, store, tmp_path):
    env = tmp_path / "ringabot.env"
    env.write_text("API_TOKEN=placeholder-token-value\n", encoding="utf-8")
    env.chmod(0o600)
    settings = settings.__class__(**{**settings.__dict__, "env_file": env})
    checks = await run_doctor(settings, store, offline=True)
    assert not failed(checks)
    assert any(item.name == "xai" and item.status == "warn" for item in checks)

    async def key_probe(_key: str):
        return True, "ok"

    async def register_probe(_settings, _creds):
        return True, "registered"

    async def tls_probe(_host: str):
        return True, "handshake ok"

    live = await run_doctor(
        settings,
        store,
        key_probe=key_probe,
        register_probe=register_probe,
        tls_probe=tls_probe,
    )
    assert not failed(live)
    assert store.get_setting("local", "xai_key_status") == "ok"
    inbound = next(item for item in live if item.name == "inbound")
    assert inbound.status == "ok"
    assert inbound.detail == "off"

    enabled = settings.__class__(**{**settings.__dict__, "inbound_mode": "on"})
    answering = await run_doctor(
        enabled,
        store,
        key_probe=key_probe,
        register_probe=register_probe,
        tls_probe=tls_probe,
    )
    inbound_on = next(item for item in answering if item.name == "inbound")
    assert inbound_on.status == "ok"
    assert inbound_on.detail.startswith("enabled")

    missing = settings.__class__(
        **{**settings.__dict__, "inbound_mode": "on", "voipms_sip_user": "", "xai_api_key": ""}
    )
    blocked = await run_doctor(missing, store, offline=True)
    assert failed(blocked)
    assert any(item.name == "inbound" and item.status == "fail" for item in blocked)


def test_redactor_scrubs_runtime_secrets():
    REDACTOR.add("placeholder-sip-pass")
    record = logging.LogRecord(
        name="ringabot",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="using placeholder-sip-pass and Bearer placeholder-token-value",
        args=(),
        exc_info=None,
    )
    assert RedactFilter().filter(record)
    assert "placeholder-sip-pass" not in record.msg
    assert "Bearer [redacted]" in record.msg


def test_retention_deletes_old_recordings(settings, store, tmp_path):
    path = tmp_path / "old.pcm"
    path.write_bytes(b"\x00\x00")
    store.add_recording("local", "call_old", str(path))
    old = utc_iso(utc_now() - timedelta(days=40))
    store._conn.execute("UPDATE recordings SET created_at=? WHERE call_id=?", (old, "call_old"))
    store._conn.commit()
    store.add_transcript("call_old", "assistant", "stale")
    store._conn.execute("UPDATE transcript SET created_at=? WHERE call_id=?", (old, "call_old"))
    store._conn.commit()
    removed = enforce_retention(store, 30)
    assert str(path) in removed
    assert not path.exists()
    assert store.transcript("call_old") == []
    assert "Call ended" in local_summary([], [])
