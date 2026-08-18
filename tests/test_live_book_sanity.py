"""A book whose bid sits above its ask is not a spread, it is bad data.

Live on production: MEXC's websocket books for ANSEM read

    Mexc Futures  bid=0.319     ask=0.2475
    Mexc Spot     bid=0.31008   ask=0.24539

while the exchange itself was quoting 0.2573/0.2579 and 0.25338/0.25416. Every
other venue's book was ordered correctly, so this is one adapter handing back
levels the code then trusted positionally: `_book_side` takes `levels[0]`, and
if the venue did not sort them, that is an arbitrary level rather than the top
of book.

The board turned that into "ANSEM, Mexc Spot to Mexc Futures, +30%" -- an
opportunity that never existed, presented as fresh and depth-verified. On a
paid board that is worse than showing nothing.

Two invariants, both enforced where every consumer reads: levels are sorted so
the best price really is first, and a book that is still crossed after sorting
is discarded rather than priced.
"""

from __future__ import annotations

from spreadboard import live_book_cache


def test_bids_are_sorted_best_first() -> None:
    levels = live_book_cache._levels([[0.24, 5], [0.26, 5], [0.25, 5]], side="bid")

    assert [row[0] for row in levels] == [0.26, 0.25, 0.24]


def test_asks_are_sorted_best_first() -> None:
    levels = live_book_cache._levels([[0.26, 5], [0.24, 5], [0.25, 5]], side="ask")

    assert [row[0] for row in levels] == [0.24, 0.25, 0.26]


def test_an_unsorted_venue_cannot_fake_a_top_of_book() -> None:
    """The MEXC shape: the real best bid is far from position zero."""
    bids = live_book_cache._levels([[0.319, 1], [0.2573, 900]], side="bid")

    # Sorting alone still puts 0.319 first -- it is genuinely the highest bid in
    # the payload -- which is why the cross check below is the real guard.
    assert bids[0][0] == 0.319


def test_a_crossed_book_is_rejected() -> None:
    """bid 0.319 against ask 0.2475 cannot happen in a real market."""
    assert live_book_cache.book_is_sane(
        [[0.319, 1]], [[0.2475, 1]]
    ) is False


def test_a_normal_book_is_kept() -> None:
    assert live_book_cache.book_is_sane([[0.25338, 10]], [[0.25416, 10]]) is True


def test_a_touching_book_is_kept() -> None:
    """bid == ask is a locked market: unusual, but it does happen."""
    assert live_book_cache.book_is_sane([[0.25, 10]], [[0.25, 10]]) is True


def test_a_one_sided_book_is_kept() -> None:
    """Only one side quoted is thin, not wrong, and still prices that side."""
    assert live_book_cache.book_is_sane([[0.25, 10]], []) is True
    assert live_book_cache.book_is_sane([], [[0.25, 10]]) is True


def test_the_ansem_book_that_shipped_a_thirty_percent_lie() -> None:
    """The exact production values, as a regression."""
    assert live_book_cache.book_is_sane([[0.31008, 100]], [[0.24539, 100]]) is False
