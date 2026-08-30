"""The global row limit must not delete a token because it sorted late.

`--row-limit` bounds the whole snapshot, and the rows arrive grouped by source
and token. The trim was `rows[:limit]`, so once the limit binds every token
past the cut lost ALL of its spreads -- not its weakest ones, all of them --
regardless of quality, and nothing reported it. Production hit that limit
exactly (30,000 rows) after quote unification widened the candidate set.
"""

from __future__ import annotations

from typing import Any

from spreadarb.api_discovery import runner


def _rows(token: str, count: int) -> list[dict[str, Any]]:
    return [{"token": token, "route_key": f"{token}-{i}"} for i in range(count)]


def test_a_token_at_the_end_of_the_list_keeps_a_spread() -> None:
    """The exact production shape: early tokens carry many routes each."""

    rows = [*_rows("AAA", 28), *_rows("BBB", 28), *_rows("LATE", 4)]

    kept = runner._token_fair_trim(rows, 30)

    tokens = {row["token"] for row in kept}
    assert "LATE" in tokens, (
        "a token was deleted entirely because it sorted last, not because its "
        "spreads were worse"
    )
    assert len(kept) == 30


def test_every_token_gets_its_best_route_before_any_gets_a_second() -> None:
    rows = [*_rows("AAA", 10), *_rows("BBB", 10), *_rows("CCC", 10)]

    kept = runner._token_fair_trim(rows, 3)

    assert {row["token"] for row in kept} == {"AAA", "BBB", "CCC"}
    assert {row["route_key"] for row in kept} == {"AAA-0", "BBB-0", "CCC-0"}, (
        "each token's strongest route comes first"
    )


def test_a_tokens_internal_order_is_preserved() -> None:
    """Incoming order within a token is the per-token cap's strength order."""

    kept = runner._token_fair_trim([*_rows("AAA", 5), *_rows("BBB", 1)], 4)
    aaa = [row["route_key"] for row in kept if row["token"] == "AAA"]
    assert aaa == sorted(aaa, key=lambda k: int(k.split("-")[1]))


def test_a_list_under_the_limit_is_untouched() -> None:
    rows = [*_rows("AAA", 3), *_rows("BBB", 2)]
    assert runner._token_fair_trim(rows, 99) == rows


def test_the_limit_is_respected_exactly() -> None:
    rows = [*_rows("AAA", 50), *_rows("BBB", 50)]
    assert len(runner._token_fair_trim(rows, 37)) == 37
