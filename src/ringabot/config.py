"""Settings from the environment. Values are never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Persona from the Oct 16 outbound test call (the one that sounded best).
DEFAULT_PERSONA_NAME = "Ringabot"
DEFAULT_VOICE = "aurora"
DEFAULT_NOTES = "Be brief and polite."
# Wemble Stripe product (acct_1PJio5Ke24N6hhcu). Monthly prices are created in the dashboard.
DEFAULT_STRIPE_PRODUCT = "prod_VSVWjrAodSEFv9"
DEFAULT_DB_PATH = "/var/lib/ringabot/ringabot.sqlite"


def guard_live_charges(secret: str, live_charges: bool) -> None:
    """Refuse to boot when a live secret key is set and live charges are off."""
    if secret.startswith("sk_live_") and not live_charges:
        raise SystemExit(
            "refusing to start: STRIPE_SECRET_KEY starts with sk_live_. "
            "Set RINGABOT_LIVE_CHARGES=1 to allow live charges."
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
    api_token: str = ""
    did: str = ""
    public_url: str = ""
    sip_allowed_cidrs: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8787
    persona_name: str = DEFAULT_PERSONA_NAME
    voice: str = DEFAULT_VOICE
    instructions: str = DEFAULT_NOTES
    greeting: str = ""
    max_call_seconds: int = 600
    idle_seconds: int = 30
    max_concurrent_calls: int = 2
    # Outbound (baresip dials through the carrier; audio relayed over snd-aloop).
    sip_server: str = ""
    sip_username: str = ""
    sip_password: str = ""
    allowed_destinations: list[str] = field(default_factory=list)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_product_id: str = DEFAULT_STRIPE_PRODUCT
    stripe_price_usd: str = ""
    stripe_price_cad: str = ""
    live_charges: bool = False
    db_path: str = DEFAULT_DB_PATH

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        settings = cls(
            xai_api_key=(env.get("XAI_API_KEY") or "").strip(),
            webhook_secret=(env.get("XAI_WEBHOOK_SECRET") or "").strip(),
            api_token=(env.get("API_TOKEN") or "").strip(),
            did=(env.get("DID") or "").strip(),
            public_url=(env.get("PUBLIC_URL") or "").strip().rstrip("/"),
            sip_allowed_cidrs=_list(env, "SIP_ALLOWED_CIDRS"),
            host=(env.get("HOST") or "127.0.0.1").strip(),
            port=_int(env, "PORT", 8787),
            persona_name=(env.get("PERSONA_NAME") or "").strip() or DEFAULT_PERSONA_NAME,
            voice=(env.get("VOICE") or "").strip() or DEFAULT_VOICE,
            instructions=(env.get("INSTRUCTIONS") or "").strip() or DEFAULT_NOTES,
            greeting=(env.get("GREETING") or "").strip(),
            max_call_seconds=_int(env, "MAX_CALL_SECONDS", 600),
            idle_seconds=_int(env, "IDLE_TIMEOUT", 30),
            max_concurrent_calls=_int(env, "MAX_CONCURRENT_CALLS", 2),
            sip_server=(env.get("SIP_SERVER") or "").strip(),
            sip_username=(env.get("SIP_USERNAME") or "").strip(),
            sip_password=(env.get("SIP_PASSWORD") or "").strip(),
            allowed_destinations=_list(env, "ALLOWED_DESTINATIONS"),
            stripe_secret_key=(env.get("STRIPE_SECRET_KEY") or "").strip(),
            stripe_webhook_secret=(env.get("STRIPE_WEBHOOK_SECRET") or "").strip(),
            stripe_product_id=(env.get("STRIPE_PRODUCT_ID") or "").strip()
            or DEFAULT_STRIPE_PRODUCT,
            stripe_price_usd=(env.get("STRIPE_PRICE_USD") or "").strip(),
            stripe_price_cad=(env.get("STRIPE_PRICE_CAD") or "").strip(),
            live_charges=(env.get("RINGABOT_LIVE_CHARGES") or "").strip() == "1",
            db_path=(env.get("RINGABOT_DB") or "").strip() or DEFAULT_DB_PATH,
        )
        guard_live_charges(settings.stripe_secret_key, settings.live_charges)
        return settings

    @property
    def webhook_url(self) -> str:
        return f"{self.public_url}/xai/webhook" if self.public_url else ""

    @property
    def outbound_ready(self) -> bool:
        return bool(self.sip_server and self.sip_username and self.sip_password and self.did)
