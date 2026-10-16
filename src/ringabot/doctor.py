"""Local and live checks for a self-hosted install."""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass

from ringabot.config import Settings
from ringabot.credentials import resolve_credentials
from ringabot.probes import validate_xai_key
from ringabot.store import Store


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def format_report(checks: list[Check]) -> str:
    return "\n".join(f"{item.status:<8} {item.name:<16} {item.detail}" for item in checks)


async def run_doctor(
    settings: Settings,
    store: Store,
    *,
    offline: bool = False,
    key_probe=None,
    register_probe=None,
    tls_probe=None,
) -> list[Check]:
    checks: list[Check] = []
    checks.append(_token(settings))
    checks.append(_master_key(settings))
    checks.append(_data_dir(settings))
    checks.append(_env_file(settings))
    if settings.inbound_mode != "off":
        checks.append(Check("inbound", "warn", "v1 does not answer inbound; set INBOUND_MODE=off"))
    else:
        checks.append(Check("inbound", "ok", "off"))
    checks.append(Check("recordings", "ok", "on" if settings.recordings else "off"))
    if offline:
        checks.append(Check("xai", "warn", "skipped offline"))
        checks.append(Check("sip", "warn", "skipped offline"))
        checks.append(Check("tls", "warn", "skipped offline"))
        return checks

    tenant = "local"
    creds = resolve_credentials(settings, store, tenant)
    probe_key = key_probe or (lambda key: validate_xai_key(settings.xai_api_base, key, timeout=3))
    ok, detail = await probe_key(creds.xai_api_key)
    store.put_setting(tenant, "xai_key_status", "ok" if ok else detail)
    checks.append(Check("xai", "ok" if ok else "fail", detail if not ok else "key accepted"))

    if register_probe is None:
        from ringabot.call import probe_register

        register_probe = probe_register
    registered, sip_detail = await register_probe(settings, creds)
    store.put_setting(tenant, "sip_register_status", "registered" if registered else sip_detail)
    checks.append(Check("sip", "ok" if registered else "fail", sip_detail))

    probe_tls = tls_probe or probe_tls_hostname
    tls_ok, tls_detail = await probe_tls(settings.public_hostname)
    checks.append(Check("tls", "ok" if tls_ok else "fail", tls_detail))
    return checks


def _token(settings: Settings) -> Check:
    if len(settings.api_token) >= 16:
        return Check("api_token", "ok", "present")
    return Check("api_token", "fail", "set API_TOKEN to at least 16 characters")


def _master_key(settings: Settings) -> Check:
    if len(settings.master_key) == 32:
        return Check("master_key", "ok", "32 bytes")
    return Check("master_key", "fail", "missing")


def _data_dir(settings: Settings) -> Check:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return Check("data_dir", "fail", "not writable")
    return Check("data_dir", "ok", str(settings.data_dir))


def _env_file(settings: Settings) -> Check:
    path = settings.env_file
    if path is None or not path.exists():
        return Check("env_file", "warn", "not set")
    mode = path.stat().st_mode & 0o777
    if mode not in {0o600, 0o400}:
        return Check("env_file", "fail", f"mode {mode:o} should be 600")
    return Check("env_file", "ok", "mode 600")


async def probe_tls_hostname(hostname: str) -> tuple[bool, str]:
    if not hostname:
        return False, "set PUBLIC_HOSTNAME"
    context = ssl.create_default_context()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(hostname, 443, ssl=context, server_hostname=hostname),
            timeout=3,
        )
    except Exception:
        return False, "tls handshake failed"
    writer.close()
    await writer.wait_closed()
    return True, "handshake ok"


def failed(checks: list[Check]) -> bool:
    return any(item.status == "fail" for item in checks)
