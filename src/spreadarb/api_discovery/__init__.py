"""Read-only API discovery for Telegram opportunity visibility.

The package is intentionally separate from the website scanner, risk gate,
paper engine, and live executors. It writes a snapshot for Telegram and an
append-only archive; it does not mutate runtime trading state.
"""

from __future__ import annotations

from spreadarb.api_discovery.models import (
    API_DISCOVERY_SCHEMA,
    DiscoveryCandidate,
    MarketQuote,
    SourceResult,
    SourceStatus,
    build_snapshot,
)

__all__ = [
    "API_DISCOVERY_SCHEMA",
    "DiscoveryCandidate",
    "MarketQuote",
    "SourceResult",
    "SourceStatus",
    "build_snapshot",
]
