"""Number and voice-agent linking. Secrets are write-only."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from ringabot.config import Settings
from ringabot.credentials import resolve_credentials
from ringabot.inbound import empty_inbound
from ringabot.sip import SipError, safe_token
from ringabot.store import Store

_DID = re.compile(r"^\+\d{8,15}$")

RegisterProbe = Callable[..., Awaitable[tuple[bool, str]]]
KeyProbe = Callable[[str], Awaitable[tuple[bool, str]]]


class SetupService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        register_probe: RegisterProbe,
        key_probe: KeyProbe,
    ) -> None:
        self.settings = settings
        self.store = store
        self.register_probe = register_probe
        self.key_probe = key_probe
        self.inbound_view = lambda _tenant: empty_inbound()

    async def connect_number(
        self, tenant: str, *, server: str, sip_user: str, sip_password: str, did: str
    ) -> dict:
        if len(server) > 253 or len(sip_user) > 80 or len(sip_password) > 128 or len(did) > 16:
            return {"ok": False, "error": "field_too_long"}
        try:
            safe_token(server, "sip_server")
            safe_token(sip_user, "sip_user")
            safe_token(sip_password, "sip_password")
            safe_token(did.lstrip("+"), "did")
        except SipError as exc:
            return {"ok": False, "error": str(exc)}
        if not _DID.fullmatch(did):
            return {"ok": False, "error": "invalid_did"}
        self.store.put_secret(tenant, "sip_password", sip_password)
        self.store.put_setting(tenant, "voipms_sip_server", server)
        self.store.put_setting(tenant, "voipms_sip_user", sip_user)
        self.store.put_setting(tenant, "voipms_did", did)
        self._refresh_revoke(tenant)
        creds = resolve_credentials(self.settings, self.store, tenant)
        registered, detail = await self.register_probe(self.settings, creds)
        self.store.put_setting(
            tenant, "sip_register_status", "registered" if registered else detail
        )
        return {
            "ok": True,
            "sip_user": sip_user,
            "did": did,
            "server": server,
            "registered": registered,
            "detail": detail,
        }

    async def link_voice_agent(
        self,
        tenant: str,
        *,
        xai_api_key: str,
        persona_name: str,
        instructions: str,
        voice: str | None,
    ) -> dict:
        if not xai_api_key or not persona_name or not instructions:
            return {"ok": False, "error": "missing_fields"}
        if (
            len(xai_api_key) > 200
            or len(persona_name) > 80
            or len(instructions) > 4000
            or (voice is not None and len(voice) > 32)
        ):
            return {"ok": False, "error": "field_too_long"}
        valid, detail = await self.key_probe(xai_api_key)
        if not valid:
            return {"ok": False, "error": "xai_key_rejected", "detail": detail}
        self.store.put_secret(tenant, "xai_api_key", xai_api_key)
        self.store.put_setting(tenant, "persona_name", persona_name)
        self.store.put_setting(tenant, "persona_instructions", instructions)
        if voice:
            self.store.put_setting(tenant, "voice", voice)
        self.store.put_setting(tenant, "xai_key_status", "ok")
        self._refresh_revoke(tenant)
        return {
            "ok": True,
            "key_valid": True,
            "persona_name": persona_name,
            "voice": voice or self.settings.xai_voice,
        }

    def status(self, tenant: str) -> dict:
        creds = resolve_credentials(self.settings, self.store, tenant)
        active = self.store.active_call_count()
        key_status = self.store.get_setting(tenant, "xai_key_status")
        if key_status == "ok":
            key_valid: bool | None = True
        elif key_status:
            key_valid = False
        else:
            key_valid = None
        return {
            "sip_configured": creds.sip_ready(),
            "sip_registered": self.store.get_setting(tenant, "sip_register_status") == "registered",
            "xai_key_present": creds.voice_ready(),
            "xai_key_valid": key_valid,
            "persona_name": creds.persona.name,
            "voice": creds.persona.voice,
            "inbound": self.inbound_view(tenant),
            "recordings": self.settings.recordings,
            "credentials_revoked": self.store.get_setting(tenant, "credentials_revoked") == "1",
            "limits": {
                "allowed_destinations": list(self.settings.allowed_destinations),
                "max_calls_per_day": self.settings.max_calls_per_day,
                "calls_today": self.store.calls_today(tenant),
                "max_concurrent_calls": self.settings.max_concurrent_calls,
                "active_calls": active,
                "queue_depth": 0,
                "per_tenant_concurrent": 1,
            },
        }

    def delete_account(self, tenant: str) -> dict:
        paths = self.store.delete_account(tenant)
        removed = 0
        for raw in paths:
            path = Path(raw)
            if path.is_file():
                path.unlink()
                removed += 1
        return {"ok": True, "recordings_removed": removed}

    def _refresh_revoke(self, tenant: str) -> None:
        if self.store.get_secret(tenant, "sip_password") and self.store.get_secret(
            tenant, "xai_api_key"
        ):
            self.store.delete_setting(tenant, "credentials_revoked")
