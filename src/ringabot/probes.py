"""Cheap reachability checks. Callers inject transports in tests."""

from __future__ import annotations

import httpx

from ringabot.outbound import (
    SsrfError,
    make_client,
    parse_outbound_url,
    request_checked,
    stream_request,
)


async def validate_xai_key(
    api_base: str,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = 3.0,
    resolver=None,
) -> tuple[bool, str]:
    if not api_key:
        return False, "missing"
    url = api_base.rstrip("/") + "/v1/models"
    try:
        parse_outbound_url(url)
    except SsrfError:
        return False, "blocked_url"
    own_client = client is None
    http = client or make_client(transport)
    try:
        if transport is not None and resolver is None:
            status, within_cap = await stream_request(
                http,
                "GET",
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
        else:
            status, within_cap = await request_checked(
                http,
                "GET",
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
                resolver=resolver,
            )
    except SsrfError:
        return False, "blocked_url"
    except TimeoutError:
        return False, "timeout"
    except (ConnectionError, OSError):
        return False, "network"
    finally:
        if own_client:
            await http.aclose()
    if not within_cap:
        return False, "response_too_large"
    if status == 200:
        return True, "ok"
    return False, "rejected"
