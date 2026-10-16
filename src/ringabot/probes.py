"""Cheap reachability checks. Callers inject transports in tests."""

from __future__ import annotations

import httpx


async def validate_xai_key(
    api_base: str,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    if not api_key:
        return False, "missing"
    url = api_base.rstrip("/") + "/v1/models"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.TimeoutException:
        return False, "timeout"
    except httpx.HTTPError:
        return False, "network"
    if response.status_code == 200:
        return True, "ok"
    return False, "rejected"
