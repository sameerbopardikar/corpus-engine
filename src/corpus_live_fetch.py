#!/usr/bin/env python3
"""Stdlib live HTTP fetch for the acquisition executor — no third-party deps.

The default transport the deployed cycle uses. It is built on the standard
library only (``http.client`` / ``ssl`` / ``socket``) so ``requests`` is not a
runtime dependency, and it enforces every safety invariant the injected fetch
contract promises:

* **SSRF, every hop.** The initial URL and every redirect target are checked
  offline (:func:`corpus_ssrf.is_safe_locator`) and then resolved with DNS; the
  socket is pinned to a validated public address so the peer that is validated
  is the peer that is connected (no DNS-rebinding window).
* **Bounded transfer, before the body.** An oversized declared ``Content-Length``
  is rejected before the body is read, and the streaming read stops the moment
  the cumulative size crosses ``max_bytes`` — the full oversized body is never
  consumed and nothing is buffered to disk.

The returned :class:`FetchResult` matches the executor's fetch contract, so the
executor re-validates every hop and re-applies its own byte cap defensively.
"""
from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from corpus_acquisition_executor import DEFAULT_MAX_BYTES, FetchResult
from corpus_ssrf import SSRFError, is_safe_locator, locator_reason, resolve_public_ips

_READ_CHUNK = 65536
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_MAX_REDIRECTS = 5


class LiveFetchError(RuntimeError):
    """Raised for a live-fetch failure that is not specifically an SSRF block."""


class SizeCapExceeded(LiveFetchError):
    """Raised when a response exceeds the byte cap (declared or streamed)."""


def enforce_declared_length(content_length: str | None, max_bytes: int) -> None:
    """Reject an oversized *declared* Content-Length before reading any body."""
    if content_length is None:
        return
    try:
        declared = int(str(content_length).strip())
    except (TypeError, ValueError):
        return
    if declared > max_bytes:
        raise SizeCapExceeded(f"declared Content-Length {declared} exceeds cap {max_bytes}")


def read_capped(reader, max_bytes: int) -> bytes:
    """Stream ``reader`` into memory, stopping as soon as the cap is crossed.

    Reads in bounded chunks and raises :class:`SizeCapExceeded` the moment the
    cumulative size exceeds ``max_bytes`` — it never drains the remainder of an
    oversized body.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SizeCapExceeded(f"response body exceeds cap {max_bytes}")
        chunks.append(chunk)
    return b"".join(chunks)


def _open_pinned(scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    """Open a connection pinned to a DNS-validated public address for ``host``.

    Every resolved address is validated (a single private answer poisons the
    name); the socket is then pinned to a validated address while TLS SNI and
    certificate validation still use the real hostname.
    """
    pinned = resolve_public_ips(host, port)[0]
    raw = socket.create_connection((pinned, port), timeout=timeout)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    if scheme == "https":
        context = ssl.create_default_context()
        conn.sock = context.wrap_socket(raw, server_hostname=host)
    else:
        conn.sock = raw
    return conn


def live_fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
) -> FetchResult:
    """Fetch ``url`` with SSRF pinning, manual redirects, and a hard byte cap."""
    current = url
    redirect_chain: list[str] = []
    for _ in range(max_redirects + 1):
        parsed = urlsplit(current)
        if parsed.scheme not in ("http", "https"):
            raise LiveFetchError(f"scheme {parsed.scheme!r} is not http(s)")
        if not parsed.hostname:
            raise LiveFetchError("URL has no host")
        if not is_safe_locator(current):
            raise SSRFError(locator_reason(current) or f"unsafe target: {current!r}")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        conn = _open_pinned(parsed.scheme, host, port, timeout)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            conn.request("GET", path, headers={"User-Agent": "GBrainCorpusEngine/1.0", "Host": host})
            response = conn.getresponse()
            status = response.status
            if status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    raise LiveFetchError(f"redirect {status} without Location")
                # Do NOT drain the redirect body: a malicious redirect could
                # stream an unbounded body. Close the connection (in the finally
                # below) to free it and follow the validated Location instead.
                redirect_chain.append(current)
                current = urljoin(current, location)
                continue
            if status >= 400:
                raise LiveFetchError(f"HTTP {status} for {current!r}")
            enforce_declared_length(response.getheader("Content-Length"), max_bytes)
            body = read_capped(response, max_bytes)
            content_type = response.getheader("Content-Type", "")
            return FetchResult(
                body=body,
                final_url=current,
                content_type=content_type,
                redirect_chain=tuple(redirect_chain),
            )
        finally:
            conn.close()
    raise LiveFetchError(f"too many redirects (> {max_redirects})")
