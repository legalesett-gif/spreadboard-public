"""An owner alert the provider never accepted must not be silently lost.

``notify_transition`` persisted the new fault state and then attempted the
Pushover send. Because the next poll compared only ``active``, it saw no
transition and never retried, and every caller discarded the returned
``delivered``/``errors`` fields. Production could therefore prove which
transitions it attempted but not whether the owner was ever told -- exactly the
gap that let app restart counts climb 6 -> 7 -> 8 on 2026-08-28.

No real push is sent from these tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spreadboard import operator_alerts


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "test-token-not-real")
    monkeypatch.setattr(
        operator_alerts.accounts,
        "list_users",
        lambda **_kw: [{"id": 1, "is_admin": True}],
    )
    monkeypatch.setattr(
        operator_alerts.accounts, "list_pushover_user_ids", lambda **_kw: [1]
    )
    monkeypatch.setattr(
        operator_alerts.accounts,
        "notification_delivery",
        lambda _uid, **_kw: {"user_key": "owner-key", "device": "", "sound": "pushover"},
    )


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "state_path": tmp_path / "operator_alert_state.json",
        "ledger_path": tmp_path / "operator_alert_ledger.jsonl",
    }


def _ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_rejected_push_is_retried_on_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends: list[dict] = []
    outcomes = [{"ok": False, "error": "HTTP 500 provider error"}, {"ok": True}]

    def _send(**kwargs):
        sends.append(kwargs)
        return outcomes[min(len(sends) - 1, len(outcomes) - 1)]

    monkeypatch.setattr(operator_alerts.alerts, "send_pushover_message", _send)
    paths = _paths(tmp_path)

    first = operator_alerts.notify_transition(
        "funding-nav", active=True, title="Funding navigation", message="exit=-9", **paths
    )
    assert first["delivered"] == 0
    assert first["changed"] is True

    # Same fault state on the next poll: the old code returned changed=False
    # and never sent anything again.
    monkeypatch.setattr(operator_alerts.time, "time", lambda: 9_999_999_999.0)
    second = operator_alerts.notify_transition(
        "funding-nav", active=True, title="Funding navigation", message="exit=-9", **paths
    )

    assert second["delivered"] == 1, "the undelivered transition must be retried"
    assert second["retry"] is True
    assert len(sends) == 2
    events = [row["event"] for row in _ledger(paths["ledger_path"])]
    assert events == ["delivery_failed", "delivered"]


def test_a_delivered_transition_is_never_sent_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends: list[dict] = []
    monkeypatch.setattr(
        operator_alerts.alerts,
        "send_pushover_message",
        lambda **kw: (sends.append(kw), {"ok": True})[1],
    )
    paths = _paths(tmp_path)

    operator_alerts.notify_transition(
        "recon", active=True, title="Reconciliation", message="recall=82.2%", **paths
    )
    for _ in range(5):
        operator_alerts.notify_transition(
            "recon", active=True, title="Reconciliation", message="recall=82.2%", **paths
        )

    assert len(sends) == 1, "a delivered fault must not re-notify while it stays active"


def test_a_delivery_failure_does_not_clear_the_underlying_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        operator_alerts.alerts,
        "send_pushover_message",
        lambda **_kw: {"ok": False, "error": "connection reset"},
    )
    paths = _paths(tmp_path)

    operator_alerts.notify_transition(
        "collector-oom", active=True, title="Collector", message="oom_kill", **paths
    )
    state = json.loads(paths["state_path"].read_text())
    event = state["events"]["collector-oom"]

    assert event["active"] is True, "the fault must survive a failed delivery"
    assert event["delivery"]["pending"] is True
    assert event["delivery"]["last_error"] == "connection", "error class only, never the raw text"


def test_a_fault_and_its_recovery_share_one_incident_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        operator_alerts.alerts, "send_pushover_message", lambda **_kw: {"ok": True}
    )
    paths = _paths(tmp_path)

    opened = operator_alerts.notify_transition(
        "nav", active=True, title="Funding navigation", message="exit=-9", **paths
    )
    recovered = operator_alerts.notify_transition(
        "nav", active=False, title="Funding navigation", message="exit=-9", **paths
    )
    reopened = operator_alerts.notify_transition(
        "nav", active=True, title="Funding navigation", message="exit=-9 again", **paths
    )

    assert opened["incident_id"] == recovered["incident_id"], (
        "a recovery must correlate with the fault it closes"
    )
    assert reopened["incident_id"] != opened["incident_id"], (
        "a new OOM after a recovery must open a new incident, not be suppressed"
    )


def test_a_corrupt_state_file_does_not_stop_an_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends: list[dict] = []
    monkeypatch.setattr(
        operator_alerts.alerts,
        "send_pushover_message",
        lambda **kw: (sends.append(kw), {"ok": True})[1],
    )
    paths = _paths(tmp_path)
    paths["state_path"].write_text("{ this is not json")

    result = operator_alerts.notify_transition(
        "nav", active=True, title="Funding navigation", message="exit=-9", **paths
    )

    assert result["delivered"] == 1
    assert len(sends) == 1


def test_a_retry_budget_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider outage must not become an unbounded delivery storm."""

    sends: list[dict] = []
    monkeypatch.setattr(
        operator_alerts.alerts,
        "send_pushover_message",
        lambda **kw: (sends.append(kw), {"ok": False, "error": "timeout"})[1],
    )
    paths = _paths(tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(operator_alerts.time, "time", lambda: clock["now"])

    operator_alerts.notify_transition(
        "nav", active=True, title="Funding navigation", message="exit=-9", **paths
    )
    for _ in range(30):
        clock["now"] += 5_000.0
        operator_alerts.notify_transition(
            "nav", active=True, title="Funding navigation", message="exit=-9", **paths
        )

    assert len(sends) == operator_alerts.MAX_DELIVERY_ATTEMPTS, (
        f"expected a bounded attempt budget, made {len(sends)} sends"
    )
