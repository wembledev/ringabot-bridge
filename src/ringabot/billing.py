"""Stripe Checkout for a monthly number, and the provisioning hook.

The hook shipped here only logs. A real implementation, after Michael's OK, will
create a Voip.ms sub-account, order a DID in the requested area code, route it to
xAI SIP at <e164>@sip.voice.x.ai, and store that e164 on the number row.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Protocol

import httpx

from ringabot.store import Account, Store

log = logging.getLogger("ringabot.billing")

PRICE_CACHE_SECONDS = 60
SIGNATURE_WINDOW = 300


class BillingError(Exception):
    pass


class Provisioner(Protocol):
    def provision(self, number_id: str, account_id: str, currency: str, area_code: str) -> None:
        """Order and route a DID. The shipped implementation only logs."""


class LoggingProvisioner:
    def provision(self, number_id: str, account_id: str, currency: str, area_code: str) -> None:
        log.info(
            "provision stub number_id=%s account_id=%s currency=%s area_code=%s "
            "(no Voip.ms order, e164 stays empty)",
            number_id,
            account_id,
            currency,
            area_code or "-",
        )


def stripe_request(secret: str, method: str, path: str, data: dict[str, str]) -> dict:
    if not secret:
        raise BillingError("STRIPE_SECRET_KEY is not set")
    url = "https://api.stripe.com" + path
    headers = {"Authorization": f"Bearer {secret}"}
    try:
        if method == "GET":
            resp = httpx.request(method, url, params=data, headers=headers, timeout=20)
        else:
            resp = httpx.request(method, url, data=data, headers=headers, timeout=20)
    except httpx.HTTPError as exc:
        raise BillingError(f"stripe request failed: {type(exc).__name__}") from exc
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        raise BillingError("stripe returned non-json") from exc
    if resp.status_code >= 400:
        message = (body.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        raise BillingError(f"stripe: {message}")
    return body


def _monthly(price: dict, currency: str, product: str) -> bool:
    if price.get("active") is False:
        return False
    if str(price.get("currency") or "").lower() != currency:
        return False
    prod = price.get("product")
    if isinstance(prod, dict):
        prod = prod.get("id")
    if prod and prod != product:
        return False
    recurring = price.get("recurring") or {}
    if recurring.get("interval") != "month":
        return False
    return (recurring.get("interval_count") or 1) == 1


class PriceBook:
    """Active monthly price for a product and currency. Hits are cached for a minute."""

    def __init__(self, ttl: float = PRICE_CACHE_SECONDS):
        self.ttl = ttl
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}

    def resolve(
        self,
        secret: str,
        product: str,
        currency: str,
        override: str,
        now: float | None = None,
    ) -> str:
        if override:
            return override
        now = time.time() if now is None else now
        key = (product, currency)
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        price_id = self._fetch(secret, product, currency)
        self._cache[key] = (now, price_id)
        return price_id

    def _fetch(self, secret: str, product: str, currency: str) -> str:
        body = stripe_request(
            secret,
            "GET",
            "/v1/prices",
            {
                "product": product,
                "currency": currency,
                "active": "true",
                "type": "recurring",
                "recurring[interval]": "month",
                "limit": "100",
            },
        )
        if body.get("has_more"):
            raise BillingError(f"too many prices on {product} to choose a monthly {currency} price")
        matches = [
            p
            for p in body.get("data") or []
            if isinstance(p, dict) and _monthly(p, currency, product)
        ]
        if not matches:
            raise BillingError(f"no active monthly price for {currency} on {product}")
        if len(matches) > 1:
            ids = ", ".join(str(p.get("id")) for p in matches)
            raise BillingError(
                f"more than one active monthly price for {currency} on {product}: {ids}"
            )
        price_id = str(matches[0].get("id") or "")
        if not price_id:
            raise BillingError(f"no active monthly price for {currency} on {product}")
        return price_id


class Billing:
    def __init__(
        self,
        settings,
        store: Store,
        provisioner: Provisioner,
        prices: PriceBook | None = None,
    ):
        self.settings = settings
        self.store = store
        self.provisioner = provisioner
        self.prices = prices or PriceBook()

    def checkout(self, account: Account, currency: str, area_code: str) -> dict:
        currency = currency.strip().lower()
        if currency not in ("usd", "cad"):
            raise BillingError("currency must be usd or cad")
        area = area_code.strip()
        if area and not area.isdigit():
            raise BillingError("area_code must be digits")
        if not self.settings.public_url.startswith("http"):
            raise BillingError("PUBLIC_URL is not set")
        if currency == "usd":
            override = self.settings.stripe_price_usd
        else:
            override = self.settings.stripe_price_cad
        price_id = self.prices.resolve(
            self.settings.stripe_secret_key,
            self.settings.stripe_product_id,
            currency,
            override,
        )
        number_id = self.store.create_number(account.id, currency)
        form = {
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "client_reference_id": number_id,
            "success_url": f"{self.settings.public_url}/billing/success",
            "cancel_url": f"{self.settings.public_url}/billing/cancel",
        }
        if account.stripe_customer_id:
            form["customer"] = account.stripe_customer_id
        if area:
            form["metadata[area_code]"] = area
        try:
            session = stripe_request(
                self.settings.stripe_secret_key,
                "POST",
                "/v1/checkout/sessions",
                form,
            )
        except BillingError:
            self.store.delete_number(number_id)
            raise
        url = session.get("url")
        if not isinstance(url, str) or not url:
            self.store.delete_number(number_id)
            raise BillingError("stripe checkout session has no url")
        return {"checkout_url": url, "number_id": number_id, "status": "pending_payment"}


def verify_stripe_signature(
    body: bytes, header: str, secret: str, now: float | None = None
) -> None:
    if not secret:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not set")
    parts: dict[str, list[str]] = {}
    for item in (header or "").split(","):
        key, _, value = item.strip().partition("=")
        if key:
            parts.setdefault(key, []).append(value)
    if not parts.get("t") or not parts.get("v1"):
        raise BillingError("missing stripe signature")
    try:
        ts = int(parts["t"][0])
    except ValueError as exc:
        raise BillingError("bad stripe signature timestamp") from exc
    now = time.time() if now is None else now
    if abs(now - ts) > SIGNATURE_WINDOW:
        raise BillingError("stripe signature timestamp outside window")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts["v1"]):
        raise BillingError("stripe signature mismatch")


def parse_stripe_event(body: bytes, header: str, secret: str, now: float | None = None) -> dict:
    verify_stripe_signature(body, header, secret, now)
    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BillingError("bad json") from exc
    if not isinstance(event, dict):
        raise BillingError("bad json")
    return event


def _id(value) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


def apply_stripe_event(store: Store, provisioner: Provisioner, event: dict) -> str:
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return "ignored"
    etype = event.get("type")
    if etype == "checkout.session.completed":
        number_id = str(obj.get("client_reference_id") or "")
        subscription_id = _id(obj.get("subscription"))
        if not number_id:
            return "ignored"
        if not subscription_id:
            raise BillingError("checkout session has no subscription")
        details = obj.get("customer_details") or {}
        email = details.get("email") if isinstance(details, dict) else None
        email = email or obj.get("customer_email")
        customer_id = _id(obj.get("customer")) or None
        activated = store.activate(
            number_id,
            subscription_id,
            customer_id,
            str(email) if email else None,
        )
        if activated is None:
            return "unchanged"
        meta = obj.get("metadata") or {}
        area = str(meta.get("area_code") or "") if isinstance(meta, dict) else ""
        provisioner.provision(number_id, activated[0], activated[1], area)
        return "active"
    if etype == "customer.subscription.deleted":
        store.cancel_subscription(_id(obj.get("id")))
        return "cancelled"
    return "ignored"
