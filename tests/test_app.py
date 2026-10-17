import json
import time

from starlette.testclient import TestClient

from conftest import SECRET, FakeXai
from ringabot.app import create_app
from ringabot.calls import CallManager
from ringabot.signature import sign


class NoRunManager(CallManager):
    """Records accepted calls without opening a websocket."""

    def spawn(self, coro):
        coro.close()


def _post(client, payload, msg_id="msg_a", secret=SECRET, extra=None):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": sign(secret, msg_id, ts, body),
        **(extra or {}),
    }
    return client.post("/xai/webhook", content=body, headers=headers)


def _incoming(call_id="c1", to="+16045550100"):
    return {
        "object": "event",
        "id": "evt_1",
        "type": "realtime.call.incoming",
        "created_at": 1,
        "data": {
            "call_id": call_id,
            "sip_headers": [{"name": "From", "value": "+16045550123"}, {"name": "To", "value": to}],
        },
    }


def _client(settings):
    manager = NoRunManager(settings, xai=FakeXai())
    return TestClient(create_app(settings, manager)), manager


def test_health(settings):
    client, _ = _client(settings)
    assert client.get("/healthz").json()["ok"] is True


def test_incoming_call_accepted_fast(settings):
    client, manager = _client(settings)
    started = time.monotonic()
    r = _post(client, _incoming())
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert time.monotonic() - started < 0.5
    assert "c1" in manager.active
    assert manager.recent[0]["from"] == "+16045550123"


def test_unsigned_webhook_rejected(settings):
    client, manager = _client(settings)
    r = client.post("/xai/webhook", content=b"{}")
    assert r.status_code == 401
    assert not manager.active


def test_wrong_number_refused(settings):
    client, manager = _client(settings)
    r = _post(client, _incoming(to="+16045550199"))
    assert r.json()["accepted"] is False and not manager.active


def test_busy_limit(settings):
    client, manager = _client(settings)
    for i in range(settings.max_concurrent_calls):
        _post(client, _incoming(call_id=f"c{i}"), msg_id=f"m{i}")
    r = _post(client, _incoming(call_id="cx"), msg_id="mx")
    assert r.json()["detail"] == "busy"


def test_bearer_required_when_configured(settings):
    from dataclasses import replace

    s = replace(settings, webhook_bearer="bearer-placeholder")
    client, _ = _client(s)
    assert _post(client, _incoming()).status_code == 401
    ok = _post(
        client,
        _incoming(),
        msg_id="m2",
        extra={"authorization": "Bearer bearer-placeholder"},
    )
    assert ok.status_code == 200


def test_admin_endpoints_need_token(settings):
    client, _ = _client(settings)
    assert client.get("/calls").status_code == 401
    auth = {"authorization": "Bearer admin-placeholder"}
    assert client.get("/calls", headers=auth).status_code == 200
    assert client.post("/calls/outbound", headers=auth).status_code == 501
    assert client.post("/calls/nope/hangup", headers=auth).status_code == 404


def test_other_event_types_ignored(settings):
    client, manager = _client(settings)
    r = _post(client, {"type": "something.else", "data": {}})
    assert r.status_code == 200 and "ignored" in r.json() and not manager.active
