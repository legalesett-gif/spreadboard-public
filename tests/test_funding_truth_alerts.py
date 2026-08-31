"""Funding stopped being true and no alert fired.

`catalog_coverage_pct` read 100.0 while 736 Ourbit legs held no figure at all,
because it counts legs we have *classified*, not legs we can actually show.
Roughly seven board cells in ten were blank and the owner found it before any
monitor did.

Both checks here are rules, not tuned thresholds: a venue with no reader names
itself, and an artifact that stopped being republished is stalled by
definition.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts import run_spreadboard_service as service


class _Recorder:
    """The alert surface, reduced to what was raised."""

    def __init__(self) -> None:
        self.calls: dict[str, dict[str, object]] = {}

    def __call__(self, key, *, active, title, message):
        self.calls[key] = {"active": active, "title": title, "message": message}


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    cache = tmp_path / "venue_funding_history.json"
    monkeypatch.setattr(
        service.venue_funding_history, "DEFAULT_CACHE_PATH", cache, raising=False
    )

    instance = service.SharedArtifactWatcher.__new__(service.SharedArtifactWatcher)
    instance.funding_truth_signature = None
    instance.funding_truth_payload = None
    instance._emit_operator_alert = _Recorder()

    def write(payload):
        cache.write_text(json.dumps(payload), encoding="utf-8")
        instance.funding_truth_signature = None
        instance.funding_truth_payload = None
        monkeypatch.setattr(
            service.venue_funding_history,
            "_load_raw",
            lambda **_k: payload,
            raising=False,
        )

    instance.write = write
    return instance


def _stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def test_a_venue_whose_every_leg_is_unsupported_is_named(watcher) -> None:
    watcher.write(
        {
            "updated_at": _stamp(0.1),
            "leg_status": {
                "Ourbit|BTC/USDT:USDT": {"status": "unsupported_venue"},
                "Ourbit|ETH/USDT:USDT": {"status": "unsupported_venue"},
                "Gate|BTC/USDT:USDT": {"status": "ok"},
            },
        }
    )

    watcher._notify_funding_truth()

    alert = watcher._emit_operator_alert.calls["funding_venue_without_reader"]
    assert alert["active"] is True
    assert "Ourbit (2)" in alert["message"]
    assert "Gate" not in alert["message"]


def test_a_venue_that_mostly_works_is_not_named(watcher) -> None:
    """One paused market is not a missing reader.

    Alerting on any unsupported leg would fire constantly and be ignored, which
    is worse than not alerting at all.
    """

    watcher.write(
        {
            "updated_at": _stamp(0.1),
            "leg_status": {
                "Gate|A/USDT:USDT": {"status": "ok"},
                "Gate|B/USDT:USDT": {"status": "unsupported_venue"},
            },
        }
    )

    watcher._notify_funding_truth()

    assert watcher._emit_operator_alert.calls["funding_venue_without_reader"]["active"] is False


def test_a_stalled_sweep_is_reported(watcher) -> None:
    watcher.write({"updated_at": _stamp(9), "leg_status": {"Gate|A/USDT:USDT": {"status": "ok"}}})

    watcher._notify_funding_truth()

    alert = watcher._emit_operator_alert.calls["funding_sweep_stalled"]
    assert alert["active"] is True
    assert "540 minutes" in alert["message"]


def test_a_fresh_sweep_is_not_reported(watcher) -> None:
    watcher.write({"updated_at": _stamp(0.2), "leg_status": {"Gate|A/USDT:USDT": {"status": "ok"}}})

    watcher._notify_funding_truth()

    assert watcher._emit_operator_alert.calls["funding_sweep_stalled"]["active"] is False


def test_an_unreadable_timestamp_is_not_called_stalled(watcher) -> None:
    """"We cannot tell" and "the sweep died" are different findings."""

    watcher.write({"updated_at": "whenever", "leg_status": {"Gate|A/USDT:USDT": {"status": "ok"}}})

    watcher._notify_funding_truth()

    assert watcher._emit_operator_alert.calls["funding_sweep_stalled"]["active"] is False


def test_an_empty_artifact_raises_nothing(watcher) -> None:
    watcher.write({})

    watcher._notify_funding_truth()

    assert watcher._emit_operator_alert.calls == {}


def test_the_poll_actually_runs_the_check(monkeypatch) -> None:
    """A check nothing calls is a check that does not exist.

    Tests that exercise only the helper have passed against this exact mutant
    before on this codebase.
    """

    instance = service.SharedArtifactWatcher.__new__(service.SharedArtifactWatcher)
    instance.book_coverage_health_signature = None
    instance.book_coverage_health = {}
    instance.funding_navigation_health_signature = None
    instance.funding_navigation_health = {}
    instance._emit_operator_alert = _Recorder()

    ran: list[str] = []
    monkeypatch.setattr(
        service.SharedArtifactWatcher,
        "_notify_funding_truth",
        lambda self: ran.append("funding_truth"),
    )
    monkeypatch.setattr(
        service.SharedArtifactWatcher, "_notify_container_health", lambda self: None
    )
    monkeypatch.setattr(service, "_artifact_signature", lambda _p: None)
    monkeypatch.setattr(
        service.coverage_reconciliation, "load_json", lambda _p: {}, raising=False
    )

    instance._notify_operational_health_if_changed()

    assert ran == ["funding_truth"]
