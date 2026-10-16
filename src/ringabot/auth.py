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
    """Equality check that does not branch on length or secret bytes.

    Both sides are hashed to a fixed 32-byte digest first, so a shorter
    guess does not take a faster path than the real token.
    """
    return hmac.compare_digest(hashlib.sha256(left).digest(), hashlib.sha256(right).digest())


class Authenticator(Protocol):
    def authenticate(self, authorization_header: str | None) -> str | None:
        """Return the tenant id, or None when the request is rejected."""


class StaticBearerAuth:
    """Single-tenant bearer token. The tenant id is always ``local``."""

    def __init__(self, token: str, tenant_id: str = "local") -> None:
        self._token = token.encode("utf-8")
        self._digest = hashlib.sha256(self._token).digest()
        self._tenant_id = tenant_id

    def authenticate(self, authorization_header: str | None) -> str | None:
        presented = _bearer_bytes(authorization_header)
        # Always hash something so a missing header is not a faster reject.
        digest = hashlib.sha256(presented if presented is not None else b"").digest()
        if not self._token or presented is None:
            hmac.compare_digest(digest, self._digest)
            return None
        if hmac.compare_digest(digest, self._digest):
            return self._tenant_id
        return None

    def check_token(self, presented: str) -> bool:
        return ct_eq(presented.encode("utf-8"), self._token)


_MAX_BEARER = 512


def _bearer_bytes(header: str | None) -> bytes | None:
    if not header:
        return None
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer" or not rest.strip():
        return None
    token = rest.strip().encode("utf-8")
    if len(token) > _MAX_BEARER:
        return None
    return token
