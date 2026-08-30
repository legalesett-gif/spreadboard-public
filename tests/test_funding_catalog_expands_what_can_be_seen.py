"""The funding catalogue expands what can reach a page, not the whole universe.

Measured on production 2026-08-30: `complete_funding_catalog.json` held 5,331
token payloads in a 523MB file, loaded resident -- on the order of 1.5-2.5GB of
Python objects, on a host whose two cgroups already commit 7,680MB of 7,941MB.
3,643 of those payloads contained no routes at all.

The module expands everything deliberately: its docstring says ranking a
bounded index before expanding "biases both the current and settled Funding
lanes". So the pruning must not reintroduce that bias, and the bound below is
what makes it safe -- a token's net carry cannot exceed the spread between its
own best and worst leg rates, and that spread is read from the UNBOUNDED
per-leg file rather than the per-token-bounded snapshot.
"""

from __future__ import annotations

import pytest

from spreadboard import bulk_quotes, funding_catalog


def _funding(**legs: tuple[float, float]):
    return {
        key: {"rate_pct": rate, "interval_hours": interval}
        for key, (rate, interval) in legs.items()
    }


def test_the_bound_normalises_to_a_day_before_comparing(monkeypatch) -> None:
    """0.01% every 4 hours is six times the carry of 0.01% every 24."""

    monkeypatch.setattr(
        bulk_quotes,
        "load_funding",
        lambda: _funding(**{
            "A|FAST/USDT:USDT": (0.01, 4.0),
            "B|FAST/USDT:USDT": (0.0, 4.0),
            "A|SLOW/USDT:USDT": (0.01, 24.0),
            "B|SLOW/USDT:USDT": (0.0, 24.0),
        }),
    )

    bound = funding_catalog._funding_reach_bound()

    assert bound["FAST"] == pytest.approx(0.06)
    assert bound["SLOW"] == pytest.approx(0.01)


def test_a_token_that_cannot_pay_loses_to_one_that_can(monkeypatch) -> None:
    """The production shape: a major spanning many venues, arbitraged flat.

    BTC spans 35 legs and 4.4MB and ranks 483rd by reachable carry; ZKC ranks
    first on a fraction of the bytes.
    """

    monkeypatch.setattr(funding_catalog, "CATALOG_TOKEN_BUDGET", 1)
    monkeypatch.setattr(
        bulk_quotes,
        "load_funding",
        lambda: _funding(**{
            "A|BTC/USDT:USDT": (0.0100, 8.0),
            "B|BTC/USDT:USDT": (0.0101, 8.0),
            "A|ZKC/USDT:USDT": (0.5000, 8.0),
            "B|ZKC/USDT:USDT": (-0.5000, 8.0),
        }),
    )

    kept = funding_catalog._tokens_worth_expanding(["BTC", "ZKC"])

    assert kept == ["ZKC"], "the payer must win the budget, not the big payload"


def test_a_futures_token_with_no_published_rate_is_kept(monkeypatch) -> None:
    """A missing rate on a real futures market is unknown, not zero."""

    monkeypatch.setattr(funding_catalog, "CATALOG_TOKEN_BUDGET", 2)
    monkeypatch.setattr(
        bulk_quotes,
        "load_funding",
        lambda: _funding(**{
            "A|KNOWN/USDT:USDT": (0.5, 8.0),
            "B|KNOWN/USDT:USDT": (-0.5, 8.0),
        }),
    )

    kept = funding_catalog._tokens_worth_expanding(
        ["KNOWN", "QUIET"], futures_tokens={"KNOWN", "QUIET"}
    )

    assert "QUIET" in kept, "a real futures market with no rate yet is unknown"


def test_a_spot_only_token_never_outranks_a_payer(monkeypatch) -> None:
    """The inversion this function must not have.

    3,126 of production's 5,331 tokens have no futures market at all. They
    cannot pay funding, so their bound is zero -- not unknown. Treating them as
    unknown spends the whole budget on tokens that produce no funding route and
    drops ZKC, the best payer on the board.
    """

    monkeypatch.setattr(funding_catalog, "CATALOG_TOKEN_BUDGET", 1)
    monkeypatch.setattr(
        bulk_quotes,
        "load_funding",
        lambda: _funding(**{
            "A|ZKC/USDT:USDT": (0.5, 8.0),
            "B|ZKC/USDT:USDT": (-0.5, 8.0),
        }),
    )

    kept = funding_catalog._tokens_worth_expanding(
        ["SPOTONLY", "ZKC"], futures_tokens={"ZKC"}
    )

    assert kept == ["ZKC"], f"spot-only token took the budget: {kept}"


def test_an_unreadable_funding_file_expands_everything(monkeypatch) -> None:
    """No bound means no safe pruning, so fall back to the old behaviour."""

    def _boom():
        raise RuntimeError("no funding file")

    monkeypatch.setattr(funding_catalog, "CATALOG_TOKEN_BUDGET", 1)
    monkeypatch.setattr(bulk_quotes, "load_funding", _boom)

    assert funding_catalog._tokens_worth_expanding(["A", "B", "C"]) == ["A", "B", "C"]


def test_a_budget_of_zero_disables_the_pruning(monkeypatch) -> None:
    monkeypatch.setattr(funding_catalog, "CATALOG_TOKEN_BUDGET", 0)
    assert funding_catalog._tokens_worth_expanding(["A", "B"]) == ["A", "B"]


def test_opening_a_pruned_token_still_answers(monkeypatch) -> None:
    """A token nobody could rank into a page is absent -- until it is asked for."""

    from spreadboard import catalog_pairs

    monkeypatch.setattr(
        catalog_pairs,
        "for_tokens",
        lambda tokens, **_kw: {tokens[0]: {"token": tokens[0], "routes": [], "ok": True}},
    )

    payload = funding_catalog._expand_one_token("PRUNED")

    assert payload is not None and payload["token"] == "PRUNED"


def test_a_venue_failure_on_demand_does_not_raise(monkeypatch) -> None:
    from spreadboard import catalog_pairs

    def _boom(_tokens, **_kw):
        raise RuntimeError("venue down")

    monkeypatch.setattr(catalog_pairs, "for_tokens", _boom)

    assert funding_catalog._expand_one_token("ANY") is None


def test_the_build_actually_applies_the_budget() -> None:
    """Guard the wiring, not just the helper.

    The helpers above pass whether or not anything calls them, so a build that
    quietly went back to expanding every token would keep this file green. This
    asserts the call site itself.
    """

    import inspect

    source = inspect.getsource(funding_catalog._complete_payloads)
    assert "_tokens_worth_expanding(" in source, (
        "the all-token build must pass its token list through the budget"
    )
    assert source.index("_tokens_worth_expanding") < source.index(
        "catalog_pairs.for_tokens"
    ), "the budget must be applied BEFORE the expensive expansion"
