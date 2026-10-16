"""Shared outbound HTTP client with send-time SSRF checks.

Webhook and xAI key probes reuse one keep-alive client. Every send resolves
the host immediately before connect, refuses non-public addresses, and pins
the TCP connection to an address that passed the check so a DNS change cannot
retarget the request. Redirects are not followed. Response bodies are discarded
after a fixed cap so a large reply cannot fill the 1 GB host.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

MAX_RESPONSE_BYTES = 65536
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata.internal"}

Resolver = Callable[[str, int], Awaitable[list[str]]]


class SsrfError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OutboundTarget:
    url: str
    host: str
    port: int
    ip: str
    scheme: str

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        if self.port != default:
            return f"{self.host}:{self.port}"
        return self.host

    def pinned_url(self) -> httpx.URL:
        return httpx.URL(self.url).copy_with(host=self.ip)


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """One pooled client. Callers on the request path must not build another."""
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(5.0, connect=2.0, read=5.0, write=2.0, pool=1.0),
        limits=httpx.Limits(
            max_connections=8,
            max_keepalive_connections=4,
            keepalive_expiry=15.0,
        ),
        follow_redirects=False,
    )


def blocked_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return blocked_ip(str(ip.ipv4_mapped))
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
    )


def parse_outbound_url(url: str) -> tuple[str, str, int, str]:
    if not url or len(url) > 2048:
        raise SsrfError("bad_url")
    try:
        parsed = httpx.URL(url.strip())
    except Exception as exc:
        raise SsrfError("bad_url") from exc
    if parsed.scheme not in {"http", "https"}:
        raise SsrfError("bad_scheme")
    if parsed.userinfo:
        raise SsrfError("bad_url")
    host = parsed.host
    if not host or len(host) > 253:
        raise SsrfError("bad_url")
    lowered = host.lower().rstrip(".")
    if (
        lowered in _BLOCKED_HOSTS
        or lowered.endswith(".localhost")
        or lowered.endswith(".local")
        or lowered.endswith(".internal")
        or lowered.isdigit()
    ):
        raise SsrfError("blocked_host")
    literal = _literal_ip(lowered)
    if literal is not None and blocked_ip(literal):
        raise SsrfError("blocked_address")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise SsrfError("bad_url")
    return parsed.scheme, lowered, port, str(parsed)


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=2,
        )
    except TimeoutError as exc:
        raise SsrfError("dns_timeout") from exc
    except OSError as exc:
        raise SsrfError("dns_failed") from exc
    found: list[str] = []
    for *_, sockaddr in infos:
        found.append(str(sockaddr[0]))
    return found


async def resolve_target(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> OutboundTarget:
    scheme, host, port, canonical = parse_outbound_url(url)
    literal = _literal_ip(host)
    if literal is not None:
        addresses = [literal]
    else:
        lookup = resolver or default_resolver
        addresses = await lookup(host, port)
    if not addresses:
        raise SsrfError("no_address")
    for address in addresses:
        if blocked_ip(address):
            raise SsrfError("blocked_address")
    return OutboundTarget(url=canonical, host=host, port=port, ip=addresses[0], scheme=scheme)


async def request_checked(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 2.0,
    resolver: Resolver | None = None,
) -> tuple[int, bool]:
    """Send one request. Returns status and whether the body stayed under the cap.

    The TCP peer is the address checked above, not a second DNS answer.
    """
    target = await resolve_target(url, resolver=resolver)
    outbound_headers = dict(headers or {})
    outbound_headers["Host"] = target.host_header
    extensions: dict = {}
    if target.scheme == "https":
        extensions["sni_hostname"] = target.host
    return await stream_request(
        client,
        method,
        target.pinned_url(),
        content=content,
        headers=outbound_headers,
        timeout=timeout,
        extensions=extensions or None,
    )


async def stream_request(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL | str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 2.0,
    extensions: dict | None = None,
) -> tuple[int, bool]:
    try:
        async with client.stream(
            method,
            url,
            content=content,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
            extensions=extensions,
        ) as response:
            return response.status_code, await _discard(response, MAX_RESPONSE_BYTES)
    except httpx.TimeoutException as exc:
        raise TimeoutError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise ConnectionError(str(exc)) from exc


async def _discard(response: httpx.Response, limit: int) -> bool:
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            return False
    return True


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None
