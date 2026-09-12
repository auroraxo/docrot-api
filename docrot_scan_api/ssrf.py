"""SSRF defenses for URL checking.

Before any connection to a checked URL we:
  1. resolve the hostname ourselves,
  2. reject if ANY resolved address is private / loopback / link-local /
     reserved / unspecified / ULA / IPv4-mapped IPv6 / documentation ranges,
  3. pin the resolved IP for the actual connection so DNS cannot be
     re-resolved to something else between check and connect.

Redirects are followed manually so every hop is re-vetted.
"""

import ipaddress
import socket
from urllib.parse import urlsplit


class SSRFBlocked(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _reject(ip: ipaddress._BaseAddress, host: str):
    kind = None
    if ip.is_loopback:
        kind = "loopback"
    elif ip.is_private:
        kind = "private"
    elif ip.is_link_local:
        kind = "link_local"
    elif ip.is_reserved:
        kind = "reserved"
    elif ip.is_multicast:
        kind = "multicast"
    elif ip.is_unspecified:
        kind = "unspecified"
    elif getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped:
        kind = "ipv4_mapped"
    elif getattr(ip, "sixtofour", None) and ip.sixtofour:
        kind = "6to4"
    elif getattr(ip, "teredo", None) and ip.teredo:
        kind = "teredo"
    if kind:
        raise SSRFBlocked(
            f"host {host!r} resolves to a {kind} address ({ip}); blocked"
        )


def vet_host(host: str, *, timeout: float = 5.0) -> str:
    """Resolve `host` and return a vetted IP to connect to.

    Raises SSRFBlocked if the host resolves to any forbidden address or
    cannot be resolved.
    """
    host = (host or "").strip().rstrip(".")
    if not host:
        raise SSRFBlocked("empty host")
    # literal IPs are vetted directly; hostnames via getaddrinfo
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        _reject(addr, host)
        return str(addr)

    infos = None
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFBlocked(f"DNS resolution failed for {host!r}") from exc
    if not infos:
        raise SSRFBlocked(f"no addresses for {host!r}")
    vetted = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        _reject(ip, host)
        if vetted is None and ip.version == 6:
            vetted = str(ip)
        if vetted is None:
            vetted = str(ip)
    return vetted


def vet_url(url: str, *, timeout: float = 5.0) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SSRFBlocked(f"unsupported scheme {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise SSRFBlocked("URL has no host")
    if parts.port not in (None, 80, 443):
        raise SSRFBlocked(f"non-standard port {parts.port} blocked")
    return vet_host(host, timeout=timeout)
