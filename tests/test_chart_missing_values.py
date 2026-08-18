"""Missing is not zero, least of all on a spread.

The chart footer read "$500 VWAP +0.000%", "Long fund +0.000%", "Short fund
+0.000%" on a route where none of those had been measured. Its formatter did

    Number.isFinite(Number(value))

and `Number(null)` is 0, which is finite -- so every absent value rendered as a
confident +0.000%.

For a spread that is the worst possible wrong answer: 0.000% is what a fully
converged trade looks like, so the board was telling a member the edge had gone
when in fact it had never been measured. The series itself was always correct;
only the numbers printed beside it lied.
"""

from __future__ import annotations

import re

from spreadboard import server


def _chart_script() -> str:
    import inspect

    return inspect.getsource(server.render_live_spread_chart)


def test_the_chart_formatter_rejects_null_before_testing_finiteness() -> None:
    """`Number(null)` is 0. Any check that converts first is broken."""
    source = _chart_script()
    assert "Number.isFinite(Number(value))\n" not in source
    # The guard has to look at the raw value, not its numeric coercion.
    assert re.search(r"value === null \|\| value === undefined", source)


def test_the_chart_has_a_dash_for_absent_readings() -> None:
    source = _chart_script()
    assert "'—'" in source or '"—"' in source


def test_the_live_board_formatter_was_already_correct() -> None:
    """It checks the raw value first; the chart one did not. Keep it that way."""
    import inspect

    board = inspect.getsource(server.render_board_stream_script)
    assert 'value === null || value === undefined || value === ""' in board
