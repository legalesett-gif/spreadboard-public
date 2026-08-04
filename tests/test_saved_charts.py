"""A member pins the pairs they actually hold.

Any route may be saved, including one whose spread is negative -- a pair that
never converges can still be worth watching, and the operator holds exactly
such a position (SKHY against SKHX on Hyperliquid, which are the same asset at
a fixed 10:1 and so only read correctly once one side is scaled).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spreadboard import accounts


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def test_a_route_can_be_pinned_and_listed(db: Path) -> None:
    accounts.add_saved_chart(
        1, {"route_key": "SKHY|Hyperliquid|Futures|Hyperliquid|Futures", "label": "SKHY vs SKHX"},
        db_path=db,
    )

    saved = accounts.list_saved_charts(1, db_path=db)

    assert len(saved) == 1
    assert saved[0]["label"] == "SKHY vs SKHX"
    assert saved[0]["ratio"] == 1.0


def test_a_fixed_ratio_pair_keeps_its_ratio(db: Path) -> None:
    """SKHY and SKHX are the same asset at 10:1."""
    accounts.add_saved_chart(
        1, {"route_key": "SKHY|A|Futures|B|Futures", "ratio": 10.0}, db_path=db
    )

    assert accounts.list_saved_charts(1, db_path=db)[0]["ratio"] == 10.0


def test_saving_the_same_route_twice_updates_rather_than_duplicates(db: Path) -> None:
    accounts.add_saved_chart(1, {"route_key": "X|A|Futures|B|Futures", "label": "first"}, db_path=db)
    accounts.add_saved_chart(1, {"route_key": "X|A|Futures|B|Futures", "label": "second"}, db_path=db)

    saved = accounts.list_saved_charts(1, db_path=db)
    assert len(saved) == 1
    assert saved[0]["label"] == "second"


def test_one_member_cannot_see_another_members_charts(db: Path) -> None:
    accounts.add_saved_chart(1, {"route_key": "X|A|Futures|B|Futures"}, db_path=db)

    assert accounts.list_saved_charts(2, db_path=db) == []


def test_a_chart_can_be_removed(db: Path) -> None:
    accounts.add_saved_chart(1, {"route_key": "X|A|Futures|B|Futures"}, db_path=db)

    assert accounts.delete_saved_chart(1, "X|A|Futures|B|Futures", db_path=db) is True
    assert accounts.list_saved_charts(1, db_path=db) == []


def test_removing_another_members_chart_does_nothing(db: Path) -> None:
    accounts.add_saved_chart(1, {"route_key": "X|A|Futures|B|Futures"}, db_path=db)

    assert accounts.delete_saved_chart(2, "X|A|Futures|B|Futures", db_path=db) is False
    assert len(accounts.list_saved_charts(1, db_path=db)) == 1


def test_a_route_key_is_required(db: Path) -> None:
    with pytest.raises(ValueError):
        accounts.add_saved_chart(1, {"label": "no route"}, db_path=db)
