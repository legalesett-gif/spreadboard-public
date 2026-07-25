"""Bitget API DNS fallback for local resolver failures."""

from __future__ import annotations

import os
import socket
from threading import Lock
from typing import Any

BITGET_API_HOST = "api.bitget.com"
DEFAULT_BITGET_FALLBACK_HOST = "api.bitget.com.cdn.cloudflare.net"

_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_LOCK = Lock()
_INSTALLED = False


def install_bitget_dns_fallback() -> None:
    """Resolve Bitget's official API host through its CNAME in this process.

    The request URL, Host header, and TLS SNI remain `api.bitget.com`. Only the
    local address lookup is redirected, which works around macOS resolver paths
    that time out on the official hostname while its CNAME resolves cleanly.
    """

    if os.getenv("SPREADARB_DISABLE_BITGET_DNS_FALLBACK", "").lower() in {"1", "true", "yes"}:
        return
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        fallback_host = os.getenv("SPREADARB_BITGET_DNS_FALLBACK_HOST", DEFAULT_BITGET_FALLBACK_HOST)

        def getaddrinfo(
            host: str,
            port: str | int | None,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> Any:
            if host == BITGET_API_HOST:
                return _ORIGINAL_GETADDRINFO(fallback_host, port, family, type, proto, flags)
            return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

        socket.getaddrinfo = getaddrinfo
        _INSTALLED = True
