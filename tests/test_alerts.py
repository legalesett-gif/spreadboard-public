

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
        "spread_quote_current": True,
    }

    assert _rule_value(row, "open_spread_pct") == 94.30
    assert _rule_value(row, "funding_24h_pct") == 0.052


def test_rule_value_prefers_the_number_the_member_saw() -> None:
    from spreadboard.alerts import _rule_value

    row = {
        "displayed_open_spread_pct": 3.0,
        "executable_spread_pct": 2.0,
        "depth_weighted_spread_pct": 1.0,
        "spread_quote_current": True,
    }

    assert _rule_value(row, "open_spread_pct") == 3.0


def test_rule_value_still_reads_rules_stored_under_the_old_names() -> None:
    from spreadboard.alerts import _rule_value

    assert _rule_value(
        {"open_spread_pct": 5.0, "spread_quote_current": True}, "open_spread_pct"
    ) == 5.0
    assert _rule_value({"open_spread_pct": 5.0, "age_min": 10.0}, "open_spread_pct") is None
    assert _rule_value({"funding_net_24h_pct": 0.1}, "funding_24h_pct") == 0.1
    assert _rule_value({}, "open_spread_pct") is None


def test_emergency_pushover_payload_repeats_with_siren_until_acknowledged(
    monkeypatch,
) -> None:
    import urllib.parse

    from spreadboard import alerts

    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status":1,"receipt":"receipt-id"}'

    def fake_urlopen(request, timeout):
        del timeout
        captured.update(urllib.parse.parse_qs(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    result = alerts.send_pushover_message(
        app_token="app",
        user_key="user",
        title="OPENAI exit",
        message="Close spread reached",
        sound="siren",
        priority=2,
        retry_seconds=216,
        expire_seconds=10_800,
    )

    assert result["ok"] is True
    assert captured["priority"] == ["2"]
    assert captured["sound"] == ["siren"]
    assert captured["retry"] == ["216"]
    assert captured["expire"] == ["10800"]
