"""Signup, Stripe checkout, webhooks, and account-scoped calls."""

import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import SECRET, FakeXai
from ringabot.app import create_app
from ringabot.billing import BillingError, PriceBook
from ringabot.config import Settings
from test_mcp import NoRunManager, _rpc, _tool

PRODUCT = "prod_VSVWjrAodSEFv9"
AUTH = {"authorization": "Bearer admin-placeholder"}


def _client(settings):
    manager = NoRunManager(settings, xai=FakeXai())
    return TestClient(create_app(settings, manager)), manager


def _price(price_id="price_month_usd", currency="usd", interval="month", count=1, active=True):
    return {
        "id": price_id,
        "active": active,
        "currency": currency,
        "product": PRODUCT,
        "type": "recurring",
        "recurring": {"interval": interval, "interval_count": count},
    }


def _stripe_sig(secret: str, body: bytes, ts: int) -> str:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _signup(client) -> tuple[str, dict]:
    response = client.post("/signup")
    assert response.status_code == 201
    body = response.json()
    return body["api_token"], {"authorization": f"Bearer {body['api_token']}"}


def _event(etype: str, obj: dict) -> bytes:
    return json.dumps({"type": etype, "data": {"object": obj}}).encode()


def _post_event(client, settings, body: bytes, secret: str | None = None, ts: int | None = None):
    secret = settings.stripe_webhook_secret if secret is None else secret
    ts = int(time.time()) if ts is None else ts
    return client.post(
        "/stripe/webhook",
        content=body,
        headers={"stripe-signature": _stripe_sig(secret, body, ts)},
    )


def test_live_key_refuses_start(tmp_path):
    live = "sk_live_" + "a" * 16
    with pytest.raises(SystemExit, match="RINGABOT_LIVE_CHARGES"):
        Settings.from_env({"STRIPE_SECRET_KEY": live})
    with pytest.raises(SystemExit, match="RINGABOT_LIVE_CHARGES"):
        Settings.from_env({"STRIPE_SECRET_KEY": live, "RINGABOT_LIVE_CHARGES": "true"})
    allowed = Settings.from_env(
        {
            "STRIPE_SECRET_KEY": live,
            "RINGABOT_LIVE_CHARGES": "1",
            "XAI_WEBHOOK_SECRET": SECRET,
            "RINGABOT_DB": str(tmp_path / "live.sqlite"),
        }
    )
    assert allowed.live_charges is True
    assert allowed.stripe_product_id == PRODUCT
    blocked = replace(allowed, live_charges=False)
    with pytest.raises(SystemExit, match="RINGABOT_LIVE_CHARGES"):
        create_app(blocked, NoRunManager(blocked, xai=FakeXai()))


def test_price_lookup_found_missing_and_wrong_interval(monkeypatch):
    seen = []

    def fake(secret, method, path, data):
        seen.append(dict(data))
        return {"data": [_price(), _price("price_week", interval="week")]}

    monkeypatch.setattr("ringabot.billing.stripe_request", fake)
    book = PriceBook()
    assert book.resolve("sk_test_x", PRODUCT, "usd", "", now=0) == "price_month_usd"
    assert seen[0]["product"] == PRODUCT
    assert seen[0]["currency"] == "usd"
    assert seen[0]["active"] == "true"
    assert seen[0]["recurring[interval]"] == "month"

    def empty(secret, method, path, data):
        return {"data": []}

    monkeypatch.setattr("ringabot.billing.stripe_request", empty)
    with pytest.raises(BillingError, match="no active monthly price for cad"):
        PriceBook().resolve("sk_test_x", PRODUCT, "cad", "", now=0)

    def yearly(secret, method, path, data):
        return {"data": [_price("price_year", interval="year")]}

    monkeypatch.setattr("ringabot.billing.stripe_request", yearly)
    with pytest.raises(BillingError, match="no active monthly price"):
        PriceBook().resolve("sk_test_x", PRODUCT, "usd", "", now=0)


def test_price_cache_and_override(monkeypatch):
    calls = {"n": 0}

    def fake(secret, method, path, data):
        calls["n"] += 1
        return {"data": [_price()]}

    monkeypatch.setattr("ringabot.billing.stripe_request", fake)
    book = PriceBook(ttl=60)
    assert book.resolve("sk_test_x", PRODUCT, "usd", "", now=0) == "price_month_usd"
    assert book.resolve("sk_test_x", PRODUCT, "usd", "", now=59) == "price_month_usd"
    assert calls["n"] == 1
    assert book.resolve("sk_test_x", PRODUCT, "usd", "", now=60) == "price_month_usd"
    assert calls["n"] == 2
    assert book.resolve("sk_test_x", PRODUCT, "usd", "price_override", now=61) == "price_override"
    assert calls["n"] == 2


def test_signup_returns_token_once_and_stores_the_hash(settings):
    client, _ = _client(settings)
    token, headers = _signup(client)
    assert _rpc(client, "tools/list", headers=headers).status_code == 200
    assert client.post("/signup").json()["api_token"] != token
    raw = Path(settings.db_path).read_bytes()
    assert token.encode() not in raw
    assert client.post(
        "/call",
        json={"number": "+16045550111", "briefing": "x"},
        headers=headers,
    ).status_code == 401
    err, data = _tool(client, "get_number", {"currency": "usd"})
    assert err and "POST /signup" in data["error"]


def test_get_number_creates_checkout_and_webhook_activates(settings, monkeypatch, caplog):
    seen = []

    def fake(secret, method, path, data):
        seen.append((path, dict(data)))
        if path == "/v1/prices":
            return {"data": [_price()]}
        assert path == "/v1/checkout/sessions"
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}

    monkeypatch.setattr("ringabot.billing.stripe_request", fake)
    client, manager = _client(settings)
    token, headers = _signup(client)
    err, data = _tool(
        client,
        "get_number",
        {"currency": "usd", "area_code": "604"},
        headers=headers,
    )
    assert not err
    assert data["checkout_url"].startswith("https://checkout.stripe.com/")
    assert data["status"] == "pending_payment"
    number_id = data["number_id"]
    query, form = seen
    assert query[0] == "/v1/prices"
    assert form[1]["mode"] == "subscription"
    assert form[1]["line_items[0][price]"] == "price_month_usd"
    assert form[1]["line_items[0][quantity]"] == "1"
    assert form[1]["client_reference_id"] == number_id
    assert form[1]["metadata[area_code]"] == "604"
    assert form[1]["success_url"] == "https://ringabot.example/billing/success"

    err, blocked = _tool(
        client, "place_call", {"number": "+16045550111", "briefing": "hi"}, headers=headers
    )
    assert err and blocked["error"] == "no active number"

    caplog.set_level("INFO")
    body = _event(
        "checkout.session.completed",
        {
            "client_reference_id": number_id,
            "subscription": "sub_123",
            "customer": "cus_123",
            "customer_details": {"email": "a@example.com"},
            "metadata": {"area_code": "604"},
        },
    )
    ok = _post_event(client, settings, body)
    assert ok.status_code == 200 and ok.json()["result"] == "active"
    again = _post_event(client, settings, body)
    assert again.status_code == 200 and again.json()["result"] == "unchanged"
    stubs = [r for r in caplog.records if r.message.startswith("provision stub")]
    assert len(stubs) == 1 and "604" in stubs[0].message and "no Voip.ms" in stubs[0].message

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT e164, status, currency, stripe_subscription_id FROM numbers WHERE id = ?",
        (number_id,),
    ).fetchone()
    assert row["status"] == "active" and row["e164"] is None
    assert row["currency"] == "usd" and row["stripe_subscription_id"] == "sub_123"
    account = conn.execute("SELECT email, stripe_customer_id FROM accounts").fetchone()
    assert account["email"] == "a@example.com" and account["stripe_customer_id"] == "cus_123"

    err, placed = _tool(
        client, "place_call", {"number": "+16045550111", "briefing": "book"}, headers=headers
    )
    assert not err and placed["call_id"].startswith("out_")
    err, listed = _tool(client, "list_calls", {"limit": 5}, headers=headers)
    assert not err and listed["calls"][0]["to"] == "+16045550111"

    seen.clear()
    err, second = _tool(client, "get_number", {"currency": "usd"}, headers=headers)
    assert not err and second["number_id"] != number_id
    assert seen[-1][1]["customer"] == "cus_123"
    assert "metadata[area_code]" not in seen[-1][1]


def test_webhook_rejects_a_bad_signature(settings, monkeypatch):
    monkeypatch.setattr(
        "ringabot.billing.stripe_request",
        lambda *a, **k: {"data": [_price()]}
        if a[2] == "/v1/prices"
        else {"url": "https://checkout.stripe.com/c/pay/cs_test_2"},
    )
    client, _ = _client(settings)
    _token, headers = _signup(client)
    _err, data = _tool(client, "get_number", {"currency": "usd"}, headers=headers)
    body = _event(
        "checkout.session.completed",
        {"client_reference_id": data["number_id"], "subscription": "sub_x", "customer": "cus_x"},
    )
    bad = client.post(
        "/stripe/webhook",
        content=body,
        headers={"stripe-signature": _stripe_sig("wrong-secret", body, int(time.time()))},
    )
    assert bad.status_code == 400 and "signature" in bad.json()["error"]
    stale = _post_event(client, settings, body, ts=int(time.time()) - 3600)
    assert stale.status_code == 400
    err, blocked = _tool(
        client, "place_call", {"number": "+16045550111", "briefing": "x"}, headers=headers
    )
    assert err and blocked["error"] == "no active number"


def test_missing_and_yearly_prices_fail_get_number(settings, monkeypatch):
    client, _ = _client(settings)
    _token, headers = _signup(client)

    monkeypatch.setattr("ringabot.billing.stripe_request", lambda *a, **k: {"data": []})
    err, data = _tool(client, "get_number", {"currency": "cad"}, headers=headers)
    assert err and "no active monthly price for cad" in data["error"]

    monkeypatch.setattr(
        "ringabot.billing.stripe_request",
        lambda *a, **k: {"data": [_price("price_year", interval="year")]},
    )
    err, data = _tool(client, "get_number", {"currency": "usd"}, headers=headers)
    assert err and "no active monthly price" in data["error"]

    monkeypatch.setattr("ringabot.billing.stripe_request", lambda *a, **k: {"data": [_price()]})
    overridden = replace(settings, stripe_price_usd="price_explicit")
    client, _ = _client(overridden)
    _token, headers = _signup(client)
    seen = []

    def fake(secret, method, path, data):
        seen.append(path)
        return {"url": "https://checkout.stripe.com/c/pay/cs_explicit"}

    monkeypatch.setattr("ringabot.billing.stripe_request", fake)
    err, data = _tool(client, "get_number", {"currency": "usd"}, headers=headers)
    assert not err and data["checkout_url"].endswith("cs_explicit")
    assert seen == ["/v1/checkout/sessions"]


def test_subscription_deleted_cancels_and_scopes_calls(settings, monkeypatch):
    monkeypatch.setattr(
        "ringabot.billing.stripe_request",
        lambda *a, **k: {"data": [_price()]}
        if a[2] == "/v1/prices"
        else {"url": "https://checkout.stripe.com/c/pay/cs_test_3"},
    )
    client, _manager = _client(settings)
    _token_a, headers_a = _signup(client)
    _token_b, headers_b = _signup(client)
    _err, bought = _tool(client, "get_number", {"currency": "usd"}, headers=headers_a)
    body = _event(
        "checkout.session.completed",
        {
            "client_reference_id": bought["number_id"],
            "subscription": "sub_scope",
            "customer": "cus_scope",
        },
    )
    assert _post_event(client, settings, body).json()["result"] == "active"

    err, placed = _tool(
        client, "place_call", {"number": "+16045550111", "briefing": "a"}, headers=headers_a
    )
    assert not err
    err, hidden = _tool(client, "list_calls", {}, headers=headers_b)
    assert err and hidden["error"] == "no active number"
    err, denied = _tool(client, "hang_up", {"call_id": placed["call_id"]}, headers=headers_b)
    assert err and "no such" in denied["error"]
    err, own = _tool(client, "list_calls", {}, headers=headers_a)
    assert not err and len(own["calls"]) == 1
    err, admin = _tool(client, "list_calls", {})
    assert not err and len(admin["calls"]) == 1

    cancelled = _event("customer.subscription.deleted", {"id": "sub_scope"})
    assert _post_event(client, settings, cancelled).json()["result"] == "cancelled"
    assert _post_event(client, settings, cancelled).json()["result"] == "cancelled"
    err, after = _tool(
        client, "place_call", {"number": "+16045550111", "briefing": "a"}, headers=headers_a
    )
    assert err and after["error"] == "no active number"
    replay = _post_event(client, settings, body)
    assert replay.json()["result"] == "unchanged"
    err, still = _tool(
        client, "place_call", {"number": "+16045550111", "briefing": "a"}, headers=headers_a
    )
    assert err and still["error"] == "no active number"


def test_bad_currency(settings):
    client, _ = _client(settings)
    _token, headers = _signup(client)
    err, data = _tool(client, "get_number", {"currency": "eur"}, headers=headers)
    assert err and data["error"] == "currency must be usd or cad"
