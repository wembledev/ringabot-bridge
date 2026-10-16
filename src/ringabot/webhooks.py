"""Signed webhooks.

Outbound posts are HMAC-SHA256 over ``{timestamp}.{body}`` with a 2 second
timeout by default. Inbound posts are accepted when that HMAC checks out
inside the replay window, or when the static bearer token matches.
Both comparisons are constant-time. There is no broker: the HTTP handler
returns, and the caller schedules any slow work on the same event loop.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

import httpx

from ringabot.auth import ct_eq

SIGNATURE_HEADER = "x-ringabot-signature"
TIMESTAMP_HEADER = "x-ringabot-timestamp"


class WebhookVerifier:
    def __init__(
        self,
        *,
        secret: str,
        bearer_token: str,
        window_seconds: int = 300,
        now=time.time,
        max_seen: int = 10000,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._bearer = bearer_token.encode("utf-8")
        self.window = window_seconds
        self._now = now
        self._seen: dict[str, float] = {}
        self._max_seen = max_seen

    def sign(self, body: bytes, timestamp: str | None = None) -> dict[str, str]:
        stamp = timestamp if timestamp is not None else str(int(self._now()))
        digest = hmac.new(self._secret, _signed_payload(stamp, body), hashlib.sha256).hexdigest()
        return {
            "X-Ringabot-Timestamp": stamp,
            "X-Ringabot-Signature": f"v1={digest}",
            "Content-Type": "application/json",
            "User-Agent": "ringabot-bridge",
        }

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        lowered = {key.lower(): value for key, value in headers.items()}
        bearer_ok = self._bearer_ok(lowered.get("authorization"))
        has_sig = SIGNATURE_HEADER in lowered or TIMESTAMP_HEADER in lowered
        hmac_ok = self._hmac_ok(lowered, body) if has_sig else self._dummy_hmac(body)
        return bearer_ok or hmac_ok

    def _bearer_ok(self, header: str | None) -> bool:
        presented = b""
        if header:
            scheme, _, rest = header.partition(" ")
            if scheme.lower() == "bearer":
                presented = rest.strip().encode("utf-8")
        if len(presented) > 512:
            presented = b""
        if not self._bearer:
            ct_eq(presented, b"")
            return False
        return ct_eq(presented, self._bearer)

    def _dummy_hmac(self, body: bytes) -> bool:
        """Spend the same MAC work when the caller sent no signature headers."""
        expected = hmac.new(self._secret, _signed_payload("0", body), hashlib.sha256).digest()
        ct_eq(b"", expected)
        return False

    def _hmac_ok(self, headers: Mapping[str, str], body: bytes) -> bool:
        stamp = headers.get(TIMESTAMP_HEADER, "")
        signature = headers.get(SIGNATURE_HEADER, "")
        if len(stamp) > 16 or len(signature) > 80:
            self._dummy_hmac(body)
            return False
        presented = _decode_signature(signature)
        expected = hmac.new(self._secret, _signed_payload(stamp, body), hashlib.sha256).digest()
        match = bool(self._secret) and ct_eq(presented, expected)
        fresh = _timestamp_fresh(stamp, self._now(), self.window)
        if not fresh or not match:
            return False
        return self._remember(stamp, signature)

    def _remember(self, stamp: str, signature: str) -> bool:
        now = self._now()
        cutoff = now - self.window
        if len(self._seen) > 256:
            self._seen = {key: exp for key, exp in self._seen.items() if exp >= cutoff}
        if len(self._seen) > self._max_seen:
            oldest = sorted(self._seen.items(), key=lambda item: item[1])
            self._seen = dict(oldest[-self._max_seen :])
        key = f"{stamp}:{signature}"
        if key in self._seen:
            return False
        self._seen[key] = now
        return True


class WebhookClient:
    def __init__(
        self,
        url: str,
        verifier: WebhookVerifier,
        *,
        timeout: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.verifier = verifier
        self.timeout = timeout
        self._transport = transport

    async def post(self, payload: dict) -> dict:
        if not self.url:
            return {"ok": False, "error": "webhook_not_configured"}
        body = _canonical(payload)
        headers = self.verifier.sign(body)
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
                response = await client.post(self.url, content=body, headers=headers)
        except httpx.TimeoutException:
            return {"ok": False, "error": "timeout"}
        except httpx.HTTPError:
            return {"ok": False, "error": "network"}
        return {"ok": response.status_code < 300, "status": response.status_code}


def _canonical(payload: dict) -> bytes:
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _signed_payload(timestamp: str, body: bytes) -> bytes:
    return timestamp.encode("utf-8") + b"." + body


def _decode_signature(header: str) -> bytes:
    value = header.strip()
    if value.startswith("v1="):
        value = value[3:]
    try:
        return bytes.fromhex(value)
    except ValueError:
        return b""


def _timestamp_fresh(stamp: str, now: float, window: int) -> bool:
    try:
        parsed = int(stamp)
    except ValueError:
        return False
    return abs(now - parsed) <= window
