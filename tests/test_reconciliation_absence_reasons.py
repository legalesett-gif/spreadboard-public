"""Every comparator absence must carry the deepest proven reason.

The first scheduled UACryptoInvest run recorded eight absences behind two
coarse labels. First-party catalogue evidence on 2026-08-28 showed both were
misleading:

* SAMSUNGSTOCK / SKHYNIXSTOCK / SHEINSTOCK were recorded as
  "missing_long_catalog_market", but the exchanges list those markets as
  SAMSUNG (28 rows), SKHYNIX (28 rows) and SHEIN (6 rows). The market is
  catalogued; only the comparator's display label failed to resolve.
* SKR, PIPPIN, XMR and CHZ were recorded as "route_not_generated", but all
  four are catalogued with many legs -- XMR carries 16 legs, enough for 120
  directed pairs -- and every one of them holds exactly 28 generated rows.
  Production caps each token at 28 rows, so the comparator's pair was budgeted
  out rather than being impossible.

Naming those two cases separately is the difference between "we cannot make
this route" and "we chose not to, and here is the knob".
"""

from __future__ import annotations

from spreadboard import coverage_reconciliation as cr


def _catalog(*markets):
    return {
        "markets": [
            {"token": t, "venue": v, "market_type": m} for t, v, m in markets
        ]
    }


def _sample(token, lv, lt, sv, st):
    return {
        "rows": [
            {
                "token": token,
                "long_venue": lv,
                "long_market_type": lt,
                "short_venue": sv,
                "short_market_type": st,
                "reference_spread_pct": 1.0,
            }
        ]
    }


def _reason(reference, routes, catalog):
    result = cr.reconcile(reference, routes=routes, catalog=catalog)
    return result["rows"][0]["reason_code"]


def test_a_display_suffix_is_not_reported_as_a_missing_market() -> None:
    """SAMSUNGSTOCK is catalogued by the venues as SAMSUNG."""

    reason = _reason(
        _sample("SAMSUNGSTOCK", "WhiteBIT", "Futures", "Mexc", "Futures"),
        routes=[],
        catalog=_catalog(
            ("SAMSUNG", "WhiteBIT", "Futures"), ("SAMSUNG", "Mexc", "Futures")
        ),
    )

    assert reason == "comparator_display_alias_unmatched", (
        "the market exists under its official ticker; do not call it missing"
    )


def test_a_budgeted_out_pair_is_named_as_such() -> None:
    """XMR carries 16 legs but is capped at 28 generated rows."""

    routes = [
        {
            "token": "XMR",
            "long_venue": "XT",
            "long_market_type": "Spot",
            "short_venue": f"V{i}",
            "short_market_type": "Futures",
            "long_market_symbol": "XMR/USDT",
            "short_market_symbol": "XMR/USDT:USDT",
        }
        for i in range(28)
    ]
    reason = _reason(
        _sample("XMR", "Mexc", "Spot", "Hyperliquid", "Futures"),
        routes=routes,
        catalog=_catalog(
            ("XMR", "Mexc", "Spot"), ("XMR", "Hyperliquid", "Futures")
        ),
    )

    assert reason == "token_route_budget_exhausted", (
        "both legs are catalogued and the token is at its route cap"
    )


def test_a_genuinely_ungenerated_route_keeps_its_original_reason() -> None:
    """A token below the cap must not be excused by the budget label."""

    routes = [
        {
            "token": "OTHER",
            "long_venue": "Gate",
            "long_market_type": "Futures",
            "short_venue": "Mexc",
            "short_market_type": "Futures",
            "long_market_symbol": "OTHER/USDT:USDT",
            "short_market_symbol": "OTHER/USDT:USDT",
        }
        for _ in range(28)
    ]
    reason = _reason(
        _sample("SKR", "Mexc", "Futures", "Gate", "Futures"),
        routes=routes,
        catalog=_catalog(("SKR", "Mexc", "Futures"), ("SKR", "Gate", "Futures")),
    )

    assert reason == "route_not_generated", (
        "SKR has no generated rows at all here, so the cap does not explain it"
    )


def test_an_uncatalogued_leg_is_still_reported_as_missing() -> None:
    """Ourbit Spot is genuinely unsupported: Ourbit is Futures-only."""

    reason = _reason(
        _sample("CHZ", "Ourbit", "Spot", "WhiteBIT", "Futures"),
        routes=[],
        catalog=_catalog(("CHZ", "WhiteBIT", "Futures")),
    )

    assert reason == "missing_long_catalog_market"


def test_alias_detection_never_invents_a_market() -> None:
    """A stripped label that is NOT catalogued must not become an alias hit."""

    reason = _reason(
        _sample("SHEINSTOCK", "Bitget", "Futures", "Mexc", "Futures"),
        routes=[],
        catalog=_catalog(("UNRELATED", "Bitget", "Futures")),
    )

    assert reason == "missing_both_catalog_markets", (
        "no SHEIN market exists here, so no alias may be claimed"
    )
