"""Where a contract trades away from its own venue's fair price.

The reference product alerts on this, and their notification is the fixture:

    Fair Price Alert ARWRSTOCK 7.34% Long
    Last Price: 84.31    Fair Price: 90.99
    Volume: $145.2k      Limit: $46370.5     Leverage: 20x
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spreadboard import fair_price


def test_it_reproduces_the_reference_alert() -> None:
    """Last 84.31 against fair 90.99 is 7.34% cheap, so the side is Long."""
    row = fair_price.deviation(
        "Mexc",
        "ARWRSTOCK/USDT:USDT",
        {"last": 84.31, "quoteVolume": 145_200.0, "info": {"fairPrice": 90.99}},
    )

    assert row is not None
    assert row["deviation_pct"] == pytest.approx(7.34, abs=0.01)
    assert row["side"] == "Long"
    assert row["last_price"] == 84.31
    assert row["fair_price"] == 90.99
    assert row["fair_basis"] == "fairPrice"


def test_a_contract_above_its_fair_price_is_a_short() -> None:
    row = fair_price.deviation(
        "Mexc",
        "AAA/USDT:USDT",
        {"last": 110.0, "quoteVolume": 500_000.0, "info": {"fairPrice": 100.0}},
    )

    assert row is not None
    assert row["deviation_pct"] < 0
    assert row["side"] == "Short"


def test_noise_and_untradeable_contracts_are_left_out(monkeypatch) -> None:
    """Below the floor it is fees and tick size; below the volume nobody can take it."""
    monkeypatch.setattr(fair_price, "MIN_DEVIATION_PCT", 1.0)
    monkeypatch.setattr(fair_price, "MIN_VOLUME_USD", 25_000.0)

    tight = fair_price.deviation(
        "Mexc", "AAA/USDT:USDT",
        {"last": 100.2, "quoteVolume": 900_000.0, "info": {"fairPrice": 100.0}},
    )
    thin = fair_price.deviation(
        "Mexc", "BBB/USDT:USDT",
        {"last": 80.0, "quoteVolume": 100.0, "info": {"fairPrice": 100.0}},
    )

    assert tight is None
    assert thin is None


def test_a_venue_without_a_fair_price_is_skipped() -> None:
    assert fair_price.deviation("Kraken", "AAA/USD", {"last": 100.0}) is None
    assert fair_price.deviation("Kraken", "AAA/USD", {"info": {"fairPrice": 100.0}}) is None


def test_an_index_price_stands_in_when_there_is_no_mark() -> None:
    row = fair_price.deviation(
        "Bybit",
        "AAA/USDT:USDT",
        {"last": 90.0, "quoteVolume": 500_000.0, "info": {"indexPrice": 100.0}},
    )

    assert row is not None
    assert row["fair_basis"] == "indexPrice"


def test_the_widest_deviation_is_published_first(tmp_path: Path) -> None:
    path = tmp_path / "fair.json"
    written = fair_price.write(
        [
            {"symbol": "SMALL", "deviation_pct": 1.5},
            {"symbol": "BIG", "deviation_pct": -12.0},
            {"symbol": "MID", "deviation_pct": 6.0},
        ],
        cache_path=path,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert written == 3
    # Ranked on distance, whichever side it is on.
    assert [row["symbol"] for row in payload["rows"]] == ["BIG", "MID", "SMALL"]


def test_load_survives_a_missing_file(tmp_path: Path) -> None:
    assert fair_price.load(cache_path=tmp_path / "absent.json") == {"rows": []}


def test_the_page_renders_the_flagged_contracts(monkeypatch) -> None:
    from spreadboard import accounts, server

    accounts.set_current_user(None)
    monkeypatch.setattr(
        server.fair_price,
        "load",
        lambda **_kwargs: {
            "updated_at": "2026-08-04T22:00:00+00:00",
            "rows": [
                {
                    "venue": "Mexc",
                    "symbol": "ARWRSTOCK/USDT:USDT",
                    "last_price": 84.31,
                    "fair_price": 90.99,
                    "deviation_pct": 7.34,
                    "side": "Long",
                    "volume_24h_usd": 145_200.0,
                }
            ],
        },
    )

    html = server.render_fair_price_page()

    assert "ARWRSTOCK" in html
    assert "+7.34%" in html
    assert "Long" in html
    assert "Mexc" in html
    assert 'class="fair-page" data-refresh="120" data-refresh-silent="1"' in html


def test_the_page_says_so_when_nothing_is_flagged(monkeypatch) -> None:
    from spreadboard import accounts, server

    accounts.set_current_user(None)
    monkeypatch.setattr(server.fair_price, "load", lambda **_kwargs: {"rows": []})

    html = server.render_fair_price_page()

    assert "No contract is far enough" in html


# --------------------------------------------------------------------------
# Venues that quote volume in the base asset
# --------------------------------------------------------------------------
#
# Kraken Futures reports ``baseVolume`` and leaves ``quoteVolume`` empty. Reading
# only ``quoteVolume`` made volume unknown for every one of its contracts, and
# the floor was written as "reject when we know it is thin" -- so an unknown
# volume sailed straight through. 50 of the 66 rows on the live page were
# Kraken Futures admitted this way, including the widest gap on the board:
# DEGEN at +13.18% on $71 of 24h volume, against a $25,000 floor.
#
# That is precisely the market the page tells you to reject -- one small trade
# distorts Last, and the gap is the distortion, not an opportunity.


def test_volume_is_read_from_base_volume_when_the_venue_quotes_it_that_way() -> None:
    """79,873 contracts at $0.0008938 is $71 of turnover, not an unknown."""
    row = fair_price.deviation(
        "Kraken Futures",
        "DEGEN/USD:USD",
        {"last": 0.0008938, "baseVolume": 79_873.0, "info": {"markPrice": 0.0010289}},
    )

    assert row is None, "a $71 market must not reach the board"


def test_a_base_volume_venue_still_qualifies_when_it_is_genuinely_liquid() -> None:
    """The fix must not simply exclude Kraken Futures wholesale."""
    row = fair_price.deviation(
        "Kraken Futures",
        "BIG/USD:USD",
        {"last": 2.0, "baseVolume": 500_000.0, "info": {"markPrice": 2.1}},
    )

    assert row is not None
    assert row["volume_24h_usd"] == pytest.approx(1_000_000.0)


def test_quote_volume_still_wins_when_the_venue_reports_both() -> None:
    row = fair_price.deviation(
        "Mexc",
        "AAA/USDT:USDT",
        {
            "last": 100.0,
            "quoteVolume": 900_000.0,
            "baseVolume": 3.0,
            "info": {"fairPrice": 110.0},
        },
    )

    assert row is not None
    assert row["volume_24h_usd"] == pytest.approx(900_000.0)


def test_an_unmeasurable_volume_is_rejected_rather_than_admitted(monkeypatch) -> None:
    """The floor is a proof requirement, not a courtesy.

    Letting an unknown through put the thinnest markets on the board at rank 1,
    which is the exact failure the floor exists to prevent.
    """
    monkeypatch.setattr(fair_price, "MIN_VOLUME_USD", 25_000.0)

    row = fair_price.deviation(
        "Kraken Futures",
        "AAA/USD:USD",
        {"last": 100.0, "info": {"markPrice": 110.0}},
    )

    assert row is None
