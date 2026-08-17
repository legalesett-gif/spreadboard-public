"""What the new answers must actually say to be worth typing.

A group answer competes with opening the website. It earns its place only if it
resolves the question in one glance: what is worth looking at, can I get size
in, and what would this pay on my capital.

The sizing view exists because the arithmetic is the exact one that was wrong
on the portfolio page for a whole session: at 1x both legs are funded, so
capital is roughly TWICE the per-leg notional. A member asking "$5,000 into
GUA" means $2,500 a side, and answering with $5,000 a side would double their
real exposure.
"""

from __future__ import annotations

from spreadboard.telegram_queries import Query

from spreadboard import telegram_queries


def _payload() -> dict:
    """Two tokens: one wide but unproven, one narrower with real depth."""
    return {
        "groups": [
            {
                "token": "WIDE",
                "token_name": "Wide Token",
                "best_edge_pct": 12.5,
                "best_funding_24h_pct": 0.02,
                "best_funding_apr_pct": 7.3,
                "route_count": 3,
                "routes": [
                    {
                        "token": "WIDE",
                        "route_key": "WIDE|A|Spot|B|Futures",
                        "long_venue": "Mexc", "long_market_type": "Spot",
                        "short_venue": "Gate", "short_market_type": "Futures",
                        "executable_spread_pct": 12.5,
                        "depth_weighted_spread_pct": None,
                        "depth_usd": None,
                        "funding_daily_pct": 0.02,
                        "funding_apr_pct": 7.3,
                        "age_min": 0.5,
                        "requires_transfer": True,
                    }
                ],
            },
            {
                "token": "DEEP",
                "token_name": "Deep Token",
                "best_edge_pct": 3.1,
                "best_funding_24h_pct": 0.35,
                "best_funding_apr_pct": 127.8,
                "route_count": 2,
                "routes": [
                    {
                        "token": "DEEP",
                        "route_key": "DEEP|A|Futures|B|Futures",
                        "long_venue": "Bybit", "long_market_type": "Futures",
                        "short_venue": "Bitget", "short_market_type": "Futures",
                        "executable_spread_pct": 3.4,
                        "depth_weighted_spread_pct": 3.1,
                        "depth_usd": 500.0,
                        "funding_daily_pct": 0.35,
                        "funding_apr_pct": 127.8,
                        "age_min": 0.2,
                        "requires_transfer": False,
                    }
                ],
            },
        ]
    }


def _install(monkeypatch) -> None:
    payload = _payload()
    monkeypatch.setattr(telegram_queries, "_warm_payload", lambda *_a, **_k: payload)
    monkeypatch.setattr(telegram_queries, "client_visible_payload", lambda: payload)


def _render(kind: str, symbol: str = "", arg: str = "") -> str:
    return telegram_queries.render(
        Query(kind=kind, symbol=symbol, arg=arg),
        board_path="",
        public_url="https://spreadarbitrage.ink",
    )


# --------------------------------------------------------------------------
# "What is worth looking at right now?"
# --------------------------------------------------------------------------


def test_top_ranks_the_widest_spreads_and_names_them(monkeypatch) -> None:
    _install(monkeypatch)

    body = _render("top")

    assert "WIDE" in body
    assert "12.5" in body


def test_top_says_which_rows_could_not_prove_depth(monkeypatch) -> None:
    """A wide unproven number is a lead, not a fill. Saying so is the point."""
    _install(monkeypatch)

    body = _render("top")

    assert "unproven" in body.casefold() or "not proven" in body.casefold()


def test_deep_only_lists_routes_that_proved_the_probe(monkeypatch) -> None:
    """The whole value of /deep: everything in it can actually be entered."""
    _install(monkeypatch)

    body = _render("deep")

    assert "DEEP" in body
    assert "WIDE" not in body


def test_carry_ranks_by_funding_not_by_spread(monkeypatch) -> None:
    _install(monkeypatch)

    body = _render("carry")

    # DEEP pays 0.35%/day against WIDE's 0.02%, so it must lead.
    assert body.index("DEEP") < body.index("WIDE")


# --------------------------------------------------------------------------
# "Can I get size in?"
# --------------------------------------------------------------------------


def test_depth_reports_the_probe_size_the_board_actually_proved(monkeypatch) -> None:
    _install(monkeypatch)

    body = _render("depth", "DEEP")

    assert "500" in body


def test_depth_is_honest_when_nothing_was_proven(monkeypatch) -> None:
    _install(monkeypatch)

    body = _render("depth", "WIDE")

    lowered = body.casefold()
    assert "no route proved" in lowered
    # It must still hand back the usable number rather than just refusing.
    assert "12.50" in body
    assert "top-of-book" in lowered


# --------------------------------------------------------------------------
# "What would this pay on my money?"
# --------------------------------------------------------------------------


def test_sizing_splits_capital_across_both_legs(monkeypatch) -> None:
    """$5,000 of capital is $2,500 a side, because both legs are funded."""
    _install(monkeypatch)

    body = _render("calc", "DEEP", "5000")

    assert "2,500" in body


def test_sizing_quotes_the_carry_against_capital_not_one_leg(monkeypatch) -> None:
    """0.35%/day on $2,500 a side is $8.75/day, not $17.50."""
    _install(monkeypatch)

    body = _render("calc", "DEEP", "5000")

    assert "8.75" in body


def test_sizing_accepts_the_way_people_write_money(monkeypatch) -> None:
    _install(monkeypatch)

    for text in ("$5,000", "5k", "5000"):
        body = _render("calc", "DEEP", text)
        assert "2,500" in body, f"failed for {text!r}"


def test_sizing_without_an_amount_explains_itself(monkeypatch) -> None:
    _install(monkeypatch)

    body = _render("calc", "DEEP", "")

    assert "calc" in body.casefold()


# --------------------------------------------------------------------------
# Discoverability
# --------------------------------------------------------------------------


def test_help_shows_the_token_first_syntax(monkeypatch) -> None:
    _install(monkeypatch)

    body = _render("help")

    assert "/" in body
    assert "funding" in body.casefold()


def test_help_for_a_token_uses_that_token_in_its_examples(monkeypatch) -> None:
    """Generic help makes you translate; this does not."""
    _install(monkeypatch)

    body = _render("help", "GUA")

    assert "GUA/" in body


def test_status_reports_snapshot_freshness(monkeypatch) -> None:
    _install(monkeypatch)

    body = _render("status")

    assert "token" in body.casefold()


def test_a_leaderboard_shows_each_token_once(monkeypatch) -> None:
    """Eight rows is the whole answer, so one token must not eat four of them.

    Live /top returned BULLA three times and ANSEM four times: the same asset
    via near-identical routes. That is one opportunity wearing seven rows while
    the rest of the board goes unseen.
    """
    payload = {
        "groups": [
            {
                "token": "SAME",
                "routes": [
                    {"token": "SAME", "long_venue": "A", "short_venue": "B",
                     "long_market_type": "Spot", "short_market_type": "Futures",
                     "executable_spread_pct": 9.0, "depth_weighted_spread_pct": None,
                     "funding_daily_pct": 0.1},
                    {"token": "SAME", "long_venue": "A", "short_venue": "C",
                     "long_market_type": "Spot", "short_market_type": "Futures",
                     "executable_spread_pct": 8.0, "depth_weighted_spread_pct": None,
                     "funding_daily_pct": 0.09},
                ],
            },
            {
                "token": "OTHER",
                "routes": [
                    {"token": "OTHER", "long_venue": "D", "short_venue": "E",
                     "long_market_type": "Spot", "short_market_type": "Futures",
                     "executable_spread_pct": 5.0, "depth_weighted_spread_pct": None,
                     "funding_daily_pct": 0.05},
                ],
            },
        ]
    }
    monkeypatch.setattr(telegram_queries, "client_visible_payload", lambda: payload)

    body = _render("top")

    assert body.count("SAME") == 1, "the same token took more than one row"
    assert "OTHER" in body
    # It must keep the best of the duplicates, not an arbitrary one.
    assert "+9.00%" in body
    assert "+8.00%" not in body
