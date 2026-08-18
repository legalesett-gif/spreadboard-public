"""Charts must show the market, not the measurement artifacts.

Production history carries samples that jump 11, 48 percentage points between
consecutive readings -- 30-45% of all consecutive pairs move more than a full
point. A spread does not do that; a crossed order book does, and the board was
recording those readings as observations.

The crossed-book guard stops new ones, but 7.9 million rows were already
written, so the rendered series has to survive them. Two defences:

* a bucket keeps the MEDIAN of the samples inside it rather than whichever
  happened to land last, so one bad reading cannot become the whole bucket;
* points absurdly far from the series' own centre are dropped, measured in
  median absolute deviations so a route that genuinely lives at 96% is judged
  against its own scale rather than an absolute threshold.

Both pick real recorded points. Nothing here invents a value that was never
observed.
"""

from __future__ import annotations

from spreadboard import server


def _point(ts: int, value: float, *, exact: bool = True) -> dict:
    return {
        "quote_ts_us": ts,
        "executable_spread_pct": value,
        "sample_source": "exact_book" if exact else "historical_ohlcv_close_proxy",
    }


def test_a_bucket_keeps_its_median_not_its_last_sample() -> None:
    """One bad reading landing last must not become the bucket's value."""
    rows = [
        _point(1_000_000, 2.0), _point(1_500_000, 2.1), _point(1_900_000, 30.0),
    ]

    out = server._merge_history_rows(
        [], rows, since_us=0, max_points=100, bucket_seconds=2
    )

    assert len(out) == 1
    assert out[0]["executable_spread_pct"] == 2.1


def test_the_surviving_point_is_a_real_recorded_sample() -> None:
    """No averaging: every other field on the row must stay truthful."""
    rows = [_point(1_000_000, 2.0), _point(1_500_000, 9.0), _point(1_900_000, 4.0)]

    out = server._merge_history_rows(
        [], rows, since_us=0, max_points=100, bucket_seconds=2
    )

    assert out[0] in rows


def test_a_lone_wild_reading_is_dropped_from_the_series() -> None:
    """The 30% ANSEM print among 1.2% neighbours."""
    rows = [_point(i * 10_000_000, 1.2 + (i % 3) * 0.05) for i in range(40)]
    rows.append(_point(41 * 10_000_000, 30.0))

    out = server._merge_history_rows(
        [], rows, since_us=0, max_points=500, bucket_seconds=0
    )

    assert max(row["executable_spread_pct"] for row in out) < 5.0


def test_a_route_that_genuinely_lives_high_is_not_flattened() -> None:
    """VANRY sits near 96%. Its own points are not outliers."""
    rows = [_point(i * 10_000_000, 96.0 + (i % 5)) for i in range(40)]

    out = server._merge_history_rows(
        [], rows, since_us=0, max_points=500, bucket_seconds=0
    )

    assert len(out) == len(rows)
    assert min(row["executable_spread_pct"] for row in out) >= 96.0


def test_a_genuine_trend_is_never_treated_as_noise() -> None:
    """A spread walking from 2% to 8% is the whole point of the chart."""
    rows = [_point(i * 10_000_000, 2.0 + i * 0.15) for i in range(40)]

    out = server._merge_history_rows(
        [], rows, since_us=0, max_points=500, bucket_seconds=0
    )

    assert len(out) == len(rows)


def test_a_short_series_is_left_alone() -> None:
    """Too few points to know what the centre is, so nothing is judged."""
    rows = [_point(1_000_000, 1.0), _point(2_000_000, 40.0)]

    out = server._merge_history_rows(
        [], rows, since_us=0, max_points=100, bucket_seconds=0
    )

    assert len(out) == 2


def test_proxy_points_still_fill_gaps_where_there_is_no_exact_sample() -> None:
    exact = [_point(5_000_000, 2.0)]
    proxy = [_point(1_000_000, 2.4, exact=False)]

    out = server._merge_history_rows(
        proxy, exact, since_us=0, max_points=100, bucket_seconds=0
    )

    assert len(out) == 2
