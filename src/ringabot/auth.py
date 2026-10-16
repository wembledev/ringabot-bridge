"""Request authentication.

Self-host uses a static bearer token compared in constant time.
Hosted OAuth 2.1 (authorization-code + PKCE, dynamic client registration)
can implement the same Authenticator later without changing the routes.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Protocol


def ct_eq(left: bytes, right: bytes) -> bool:
    """Constant-time equality. Unequal lengths still run a compare."""
    if len(left) != len(right):
        hmac.compare_digest(hashlib.sha256(left).digest(), hashlib.sha256(right).digest())
        return False
    return hmac.compare_digest(left, right)


class Authenticator(Protocol):
    def authenticate(self, authorization_header: str | None) -> str | None:
        """Return the tenant id, or None when the request is rejected."""


class StaticBearerAuth:
    """Single-tenant bearer token. The tenant id is always ``local``."""

    def __init__(self, token: str, tenant_id: str = "local") -> None:
        self._token = token.encode("utf-8")
        self._tenant_id = tenant_id

    def authenticate(self, authorization_header: str | None) -> str | None:
        presented = _bearer_bytes(authorization_header)
        if presented is None or not self._token:
            return None
        if ct_eq(presented, self._token):
            return self._tenant_id
        return None

    def check_token(self, presented: str) -> bool:
        return ct_eq(presented.encode("utf-8"), self._token)


def _bearer_bytes(header: str | None) -> bytes | None:
    if not header:
        return None
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer" or not rest.strip():
        return None
    return rest.strip().encode("utf-8")
