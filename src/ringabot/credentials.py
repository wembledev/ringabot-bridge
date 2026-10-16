"""Resolve SIP and xAI credentials from the encrypted store, then env."""

from __future__ import annotations

from dataclasses import dataclass

from ringabot.config import Settings
from ringabot.persona import Persona, resolve_persona
from ringabot.store import Store


@dataclass(frozen=True)
class Credentials:
    sip_server: str
    sip_user: str
    sip_password: str
    did: str
    xai_api_key: str
    persona: Persona

    def sip_ready(self) -> bool:
        return bool(self.sip_server and self.sip_user and self.sip_password)

    def voice_ready(self) -> bool:
        return bool(self.xai_api_key)


def resolve_credentials(settings: Settings, store: Store, tenant: str) -> Credentials:
    revoked = store.get_setting(tenant, "credentials_revoked") == "1"
    stored_password = store.get_secret(tenant, "sip_password")
    stored_key = store.get_secret(tenant, "xai_api_key")
    if revoked:
        password = stored_password or ""
        api_key = stored_key or ""
    else:
        password = stored_password or settings.voipms_sip_password
        api_key = stored_key or settings.xai_api_key
    persona = resolve_persona(
        name=store.get_setting(tenant, "persona_name") or settings.persona_name,
        instructions=store.get_setting(tenant, "persona_instructions")
        or settings.persona_instructions,
        greeting=store.get_setting(tenant, "greeting") or settings.greeting,
        voice=store.get_setting(tenant, "voice") or settings.xai_voice,
        file_path=settings.persona_file,
    )
    return Credentials(
        sip_server=store.get_setting(tenant, "voipms_sip_server") or settings.voipms_sip_server,
        sip_user=store.get_setting(tenant, "voipms_sip_user") or settings.voipms_sip_user,
        sip_password=password,
        did=store.get_setting(tenant, "voipms_did") or settings.voipms_did,
        xai_api_key=api_key,
        persona=persona,
    )
