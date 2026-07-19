#!/usr/bin/env python3
"""SSRF guards for the acquisition transport.

Two layers of defense, both fail-closed:

* :func:`is_safe_locator` / :func:`locator_reason` — a pure, offline check on a
  URL string. It enforces http(s), rejects embedded credentials, and rejects any
  host that is a *literal* private/loopback/link-local/multicast/reserved/
  unspecified address (in dotted, decimal, hex, octal, or IPv6 form, including
  IPv4-mapped IPv6) or a dangerous name (localhost, ``*.local``, ``*.localhost``,
  or a known cloud-metadata name). Applied to every hop the executor sees —
  initial locator, each redirect, and the final URL.

* :func:`resolve_public_ips` / :func:`assert_safe_resolved_ip` — the live layer.
  Before a socket is opened, the hostname is resolved with ``getaddrinfo`` and
  every resolved address must be global unicast. The connection is then pinned
  to a validated address so DNS cannot rebind between check and connect.

A name that is not obviously dangerous passes the offline check but is still
resolved-and-validated before any bytes are fetched, so a hostname resolving to
a private address is rejected at connect time.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_SHARED_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# Names that must never be fetched regardless of what they resolve to.
_METADATA_NAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


class SSRFError(ValueError):
    """Raised when a target host is not a safe public destination."""


def _unsafe_ip_reason(ip: ipaddress._BaseAddress) -> str | None:
    """Return a reason string if this address is not safe to fetch, else None."""
    # IPv4-mapped / 6to4 / teredo IPv6 → validate the embedded IPv4 too.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or getattr(ip, "sixtofour", None) or getattr(ip, "teredo", None)
        if isinstance(mapped, tuple):  # teredo returns (server, client)
            mapped = mapped[1]
        if mapped is not None:
            embedded = _unsafe_ip_reason(mapped)
            if embedded:
                return embedded
    if isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_CGNAT_NETWORK:
        return f"shared CGNAT address {ip} is not fetchable"
    if ip.is_loopback:
        return f"loopback address {ip} is not fetchable"
    if ip.is_private:
        return f"private address {ip} is not fetchable"
    if ip.is_link_local:
        return f"link-local address {ip} is not fetchable"
    if ip.is_multicast:
        return f"multicast address {ip} is not fetchable"
    if ip.is_reserved:
        return f"reserved address {ip} is not fetchable"
    if ip.is_unspecified:
        return f"unspecified address {ip} is not fetchable"
    if getattr(ip, "is_site_local", False):
        return f"site-local address {ip} is not fetchable"
    # Fail closed by construction: reject anything that is not global unicast,
    # even when it matches none of the named categories above. The named checks
    # run first so they keep giving precise reasons and so is_global-True-but-
    # unsafe cases (multicast/anycast) stay rejected; this final gate is the
    # allowlist backstop that does not depend on the stdlib's private/reserved
    # enumeration staying complete (e.g. a special-use range a future registry
    # or Python version has not yet flagged, of which RFC 6598 100.64.0.0/10 was
    # historically one). A missing ``is_global`` attribute also fails closed.
    if not getattr(ip, "is_global", False):
        return f"non-global address {ip} is not fetchable"
    return None


def _parse_literal_ip(hostname: str) -> ipaddress._BaseAddress | None:
    """Interpret a hostname as a literal IP in any form socket would accept.

    Handles standard dotted IPv4 and IPv6 plus the decimal/hex/octal and
    short-dotted IPv4 encodings that ``socket.inet_aton`` (and therefore the
    resolver) accepts but :func:`ipaddress.ip_address` does not. Returns ``None``
    for genuine DNS names.
    """
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    # IPv6 literal (already had brackets stripped by urlsplit).
    if ":" in hostname:
        try:
            return ipaddress.IPv6Address(socket.inet_pton(socket.AF_INET6, hostname))
        except (OSError, ValueError):
            return None
    # Alternate IPv4 encodings (decimal / hex / octal / short-dotted).
    try:
        packed = socket.inet_aton(hostname)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def _dangerous_name(hostname: str) -> str | None:
    name = hostname.rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return f"localhost name {hostname!r} is not fetchable"
    if name.endswith(".local"):
        return f"mDNS/.local name {hostname!r} is not fetchable"
    if name in _METADATA_NAMES:
        return f"cloud-metadata name {hostname!r} is not fetchable"
    return None


def locator_reason(url: str | None) -> str | None:
    """Return an explicit reason a URL is unsafe, or ``None`` if it is safe."""
    if not isinstance(url, str) or not url.strip():
        return "locator must be a non-empty URL"
    parsed = urlsplit(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"scheme {parsed.scheme!r} is not http(s)"
    if parsed.username or parsed.password:
        return "credentials are not permitted in a locator"
    hostname = parsed.hostname
    if not hostname:
        return "locator has no host"
    hostname = hostname.strip().lower()
    literal = _parse_literal_ip(hostname)
    if literal is not None:
        return _unsafe_ip_reason(literal)
    return _dangerous_name(hostname)


def is_safe_locator(url: str | None) -> bool:
    """True only for an http(s) URL with a safe public host and no credentials."""
    return locator_reason(url) is None


def assert_safe_resolved_ip(ip_str: str) -> None:
    """Raise :class:`SSRFError` unless ``ip_str`` is a global-unicast address."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as exc:
        raise SSRFError(f"not an IP address: {ip_str!r}") from exc
    reason = _unsafe_ip_reason(ip)
    if reason:
        raise SSRFError(reason)


def resolve_public_ips(hostname: str, port: int) -> list[str]:
    """Resolve ``hostname`` and return validated public addresses.

    Every resolved address must be global unicast, otherwise :class:`SSRFError`
    is raised (a single private answer poisons the whole name, defeating
    DNS-rebinding and split-horizon tricks). Returns the concrete addresses so
    the caller can pin the socket to a validated peer.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SSRFError(f"could not resolve host {hostname!r}: {exc}") from exc
    addresses: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        assert_safe_resolved_ip(ip_str)
        if ip_str not in addresses:
            addresses.append(ip_str)
    if not addresses:
        raise SSRFError(f"no addresses resolved for host {hostname!r}")
    return addresses
