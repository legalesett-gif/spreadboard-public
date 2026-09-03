"""Pricing 11 positions parsed every order book in the cache.

`live_book_cache.load_all()` reads and JSON-decodes every book fresh enough to
price with -- hundreds of legs, each into full bid/ask level lists -- so that
roughly 22 keys could be read for the account's positions. The alert worker did
that on every pass, and the profiler caught the thread inside `load_all` while
requests waited.

The keys are fully derivable from the positions themselves: venue, market type
(and its canonical spelling) and symbol, per leg.
"""

from __future__ import annotations

import time

from spreadboard import live_book_cache, portfolio


def _position(**kw):
    base = {
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "long_symbol": "TKN/USDT",
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_symbol": "TKN/USDT:USDT",
    }
    base.update(kw)
    return base


def test_keys_cover_both_legs_of_every_position() -> None:
    keys = portfolio.position_book_keys([_position(), _position(long_venue="Mexc")])

    assert live_book_cache.cache_key("Gate", "Spot", "TKN/USDT") in keys
    assert live_book_cache.cache_key("Aster", "Futures", "TKN/USDT:USDT") in keys
    assert live_book_cache.cache_key("Mexc", "Spot", "TKN/USDT") in keys


def test_the_canonical_market_spelling_is_included() -> None:
    """The lookup tries both the recorded type and its canonical form, so a
    keyed load that fetched only one would miss the book the reader wants."""

    keys = portfolio.position_book_keys([_position(short_market_type="PERP")])

    # Aster/PERP canonicalises to Futures. BOTH must be asked for: the reader
    # tries the recorded spelling and the canonical one, so fetching only the
    # recorded one leaves it looking for a book that was never loaded.
    assert live_book_cache.cache_key("Aster", "PERP", "TKN/USDT:USDT") in keys
    assert live_book_cache.cache_key("Aster", "Futures", "TKN/USDT:USDT") in keys


def test_a_position_missing_a_leg_contributes_nothing_for_it() -> None:
    keys = portfolio.position_book_keys([_position(short_venue="", short_symbol="")])

    assert any(k.startswith("Gate|") for k in keys)
    assert not any(k.startswith("|") for k in keys), "built a key from an empty leg"


def test_no_positions_asks_for_no_books() -> None:
    assert portfolio.position_book_keys([]) == set()


def test_the_store_loads_only_the_keys_asked_for(tmp_path) -> None:
    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    try:
        for i in range(6):
            store.put(
                "Gate", "Spot", f"T{i}/USDT",
                bids=[[1.0, 5.0]], asks=[[1.1, 5.0]],
                quote_ts_us=int(time.time() * 1_000_000),
            )
        wanted = {live_book_cache.cache_key("Gate", "Spot", "T2/USDT")}

        got = store.load_keys(wanted, max_age_seconds=600.0)

        assert set(got) == wanted, f"asked for 1 key, received {len(got)}"
    finally:
        store.close()


def test_an_empty_key_set_does_not_read_the_table(tmp_path) -> None:
    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    try:
        store.put(
            "Gate", "Spot", "T/USDT", bids=[[1.0, 5.0]], asks=[[1.1, 5.0]],
            quote_ts_us=int(time.time() * 1_000_000),
        )
        assert store.load_keys(set(), max_age_seconds=600.0) == {}
    finally:
        store.close()


def test_the_alert_worker_asks_only_for_its_positions_books(monkeypatch, tmp_path) -> None:
    """Drives check_once, not the helper.

    The helper test passes against the mutant that puts `_live_books()` back,
    which is the whole cost this change exists to remove.
    """

    asked: list[set[str]] = []

    def fake_for(positions):
        asked.append(portfolio.position_book_keys(positions))
        return {}

    monkeypatch.setattr(portfolio, "_live_books_for", fake_for)
    monkeypatch.setattr(
        portfolio, "_live_books", lambda: (_ for _ in ()).throw(AssertionError("load_all used"))
    )
    monkeypatch.setattr(portfolio.bulk_quotes, "load_funding", dict)
    monkeypatch.setattr(portfolio.chart_catalog, "load", lambda: {"markets": []})
    monkeypatch.setattr(portfolio.portfolio_funding, "load", dict)
    monkeypatch.setattr(portfolio.accounts, "list_alert_user_ids", lambda **_k: [1])
    monkeypatch.setattr(
        portfolio.accounts,
        "list_positions",
        lambda _uid, **_k: [
            dict(_position(), status="open", alert_rules=[{"enabled": True}])
        ],
    )
    monkeypatch.setattr(portfolio.accounts, "get_user_object", lambda _uid, **_k: None)

    worker = portfolio.PositionAlertWorker.__new__(portfolio.PositionAlertWorker)
    worker.accounts_path = tmp_path / "a.sqlite3"
    worker.quote_scheduler = None
    worker.check_once()

    assert asked, "check_once did not load books through the keyed path"
    assert asked[0] == {
        live_book_cache.cache_key("Gate", "Spot", "TKN/USDT"),
        live_book_cache.cache_key("Aster", "Futures", "TKN/USDT:USDT"),
    }
