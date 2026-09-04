"""The owner must be able to stop the site-status pushes without a deploy dance.

These are SpreadBoard health transitions -- route reconciliation and funding
truth -- and the owner asked for them off. Both call sites go through
`notify_transition`, so one switch covers them, and it is the only Pushover path
in this repo: the trading system's alerts use a different token entirely and are
untouched.

Off means off at the door. The state file is still updated so a later re-enable
does not replay a backlog of transitions that happened while it was quiet, and
nothing is queued as pending for retry.
"""

from __future__ import annotations

from spreadboard import operator_alerts


def _notify(tmp_path, monkeypatch, *, active: bool = True):
    sent: list[tuple] = []
    monkeypatch.setattr(
        operator_alerts,
        "_send",
        lambda **kwargs: sent.append(kwargs) or (1, []),
    )
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "token-not-used-by-tests")
    result = operator_alerts.notify_transition(
        "site_status",
        active=active,
        title="SpreadBoard",
        message="something happened",
        db_path=tmp_path / "accounts.sqlite3",
        state_path=tmp_path / "state.json",
        ledger_path=tmp_path / "ledger.jsonl",
    )
    return result, sent


def test_alerts_are_delivered_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SPREADBOARD_OPERATOR_ALERTS", raising=False)

    result, sent = _notify(tmp_path, monkeypatch)

    assert sent, "default behaviour must be unchanged"
    assert result["delivered"] == 1


def test_the_switch_stops_delivery(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_OPERATOR_ALERTS", "off")

    result, sent = _notify(tmp_path, monkeypatch)

    assert sent == []
    assert result["delivery"] == "operator_alerts_disabled"
    assert result["delivered"] == 0


def test_a_disabled_transition_is_not_queued_for_retry(tmp_path, monkeypatch) -> None:
    """Otherwise re-enabling replays every fault that happened while quiet."""

    monkeypatch.setenv("SPREADBOARD_OPERATOR_ALERTS", "off")
    _notify(tmp_path, monkeypatch, active=True)

    monkeypatch.delenv("SPREADBOARD_OPERATOR_ALERTS")
    result, sent = _notify(tmp_path, monkeypatch, active=True)

    assert sent == [], "the fault state is unchanged, so there is nothing to send"
    assert result["delivered"] == 0


def test_the_state_still_tracks_while_disabled(tmp_path, monkeypatch) -> None:
    """Recovery must not be announced for a fault that was never announced."""

    monkeypatch.setenv("SPREADBOARD_OPERATOR_ALERTS", "off")
    first, _ = _notify(tmp_path, monkeypatch, active=True)
    second, _ = _notify(tmp_path, monkeypatch, active=True)

    assert first["changed"] is True
    assert second["changed"] is False
