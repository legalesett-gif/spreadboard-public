"""Ourbit's spot side must be in the catalogue, not just its futures side.

`NATIVE_SPOT_VENUES` answers "does this venue have a native order-book
fetcher". The catalogue was reusing it to answer a different question --
"should we list this venue's spot markets" -- so Ourbit, which has no native
fetcher but IS swept by `ourbit_quotes.fetch_spot`, had its entire spot side
missing. The external comparator reported the consequence as
`VELVET ourbit Spot -> gate Futures: missing_long_catalog_market`.

Loading it also had to be fixed: `_load_venue` special-cased Ourbit only for
Futures, so a Spot request fell through to ccxt, which has no Ourbit class and
raises KeyError.
"""

from __future__ import annotations

from spreadboard import chart_catalog, fast_quotes


def test_ourbit_spot_is_enumerated_for_the_catalogue() -> None:
    assert "Ourbit" in chart_catalog.CATALOGUE_ONLY_SPOT_VENUES


def test_the_two_questions_stay_separate() -> None:
    """Listing a venue's markets is not the same as having a native fetcher.

    Ourbit must NOT be claimed as a native spot book venue: its books arrive
    from the bulk sweep, and saying otherwise would route requests to a fetcher
    that cannot serve it.
    """

    assert "Ourbit" not in fast_quotes.NATIVE_SPOT_VENUES
    assert not fast_quotes.supports_native_order_book("Ourbit", "Spot")


def test_a_spot_request_uses_the_ourbit_adapter_not_ccxt(monkeypatch) -> None:
    """The ccxt path raises KeyError for Ourbit; the venue needs its own client."""

    from spreadarb.api_discovery import sources

    seen: dict[str, object] = {}

    class _Client:
        def load_markets(self):
            return {
                "VELVET/USDT": {
                    "base": "VELVET",
                    "quote": "USDT",
                    "symbol": "VELVET/USDT",
                    "id": "VELVETUSDT",
                    "spot": True,
                    "swap": False,
                    "active": True,
                }
            }

        def close(self):
            return None

    def _build(params):
        seen["options"] = dict(params.get("options") or {})
        return _Client()

    monkeypatch.setattr(sources, "_build_ourbit_exchange", _build)

    rows = chart_catalog._load_venue("Ourbit", "Spot")

    assert seen.get("options", {}).get("defaultType") == "spot", (
        "the adapter must be pointed at the requested market type"
    )
    assert [r["token"] for r in rows] == ["VELVET"]
    assert rows[0]["venue"] == "Ourbit" and rows[0]["market_type"] == "Spot"


def test_ourbit_futures_is_still_catalogued() -> None:
    assert "Ourbit" in fast_quotes.NATIVE_FUTURES_VENUES
