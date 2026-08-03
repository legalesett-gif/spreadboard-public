

def test_rule_value_reads_fields_the_board_actually_produces() -> None:
    """A spread rule must find a value on a real board row.

    _rule_value looked for open_spread_pct/entry_spread_pct/spread_pct; the
    board emits displayed_open_spread_pct, executable_spread_pct and
    depth_weighted_spread_pct. Every spread alert read None and never fired,
    so a member could set a threshold, watch the board cross it, and hear
    nothing.
    """
    from spreadboard.alerts import _rule_value

    row = {
        "route_key": "VANRY|Gate|Spot|Binance|Spot",
        "displayed_open_spread_pct": 94.30,
        "executable_spread_pct": 94.30,
        "depth_weighted_spread_pct": 94.13,
        "funding_24h_pct": 0.052,
    }

    assert _rule_value(row, "open_spread_pct") == 94.30
    assert _rule_value(row, "funding_24h_pct") == 0.052


def test_rule_value_prefers_the_number_the_member_saw() -> None:
    from spreadboard.alerts import _rule_value

    row = {
        "displayed_open_spread_pct": 3.0,
        "executable_spread_pct": 2.0,
        "depth_weighted_spread_pct": 1.0,
    }

    assert _rule_value(row, "open_spread_pct") == 3.0


def test_rule_value_still_reads_rules_stored_under_the_old_names() -> None:
    from spreadboard.alerts import _rule_value

    assert _rule_value({"open_spread_pct": 5.0}, "open_spread_pct") == 5.0
    assert _rule_value({"funding_net_24h_pct": 0.1}, "funding_24h_pct") == 0.1
    assert _rule_value({}, "open_spread_pct") is None
