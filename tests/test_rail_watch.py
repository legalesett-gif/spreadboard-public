"""A shut rail is why a fat spread survives; the reopen is the tradeable moment."""

from __future__ import annotations

import json

from spreadboard import rail_watch


def _rails(deposit=None, withdraw=None, venue="Kucoin", token="SIREN"):
    return {venue: {token: {"deposit": deposit, "withdraw": withdraw}}}


def test_shut_to_open_is_a_reopen() -> None:
    reopens = rail_watch.detect_reopens(_rails(deposit=False), _rails(deposit=True))
    assert reopens == [{"venue": "Kucoin", "token": "SIREN", "direction": "deposit"}]


def test_unknown_is_not_a_reopen() -> None:
    """A venue whose API was briefly unavailable last cycle has not reopened
    anything; treating None as shut would fire on every such blip."""
    assert rail_watch.detect_reopens(_rails(deposit=None), _rails(deposit=True)) == []
    assert rail_watch.detect_reopens({}, _rails(deposit=True)) == []


def test_still_shut_or_still_open_is_silent() -> None:
    assert rail_watch.detect_reopens(_rails(deposit=False), _rails(deposit=False)) == []
    assert rail_watch.detect_reopens(_rails(deposit=True), _rails(deposit=True)) == []
    assert rail_watch.detect_reopens(_rails(deposit=True), _rails(deposit=False)) == []


def _market(**overrides):
    row = {
        "token": "SIREN", "long_venue": "OKX DEX 1", "short_venue": "Kucoin",
        "executable_spread_pct": 98.0, "deliverable": True,
    }
    row.update(overrides)
    return {"rows": [row]}


def test_a_reopen_with_a_surviving_edge_is_alertable() -> None:
    reopens = [{"venue": "Kucoin", "token": "SIREN", "direction": "deposit"}]
    alerts = rail_watch.alertable_reopens(reopens, _market())
    assert len(alerts) == 1
    assert alerts[0]["edge_pct"] == 98.0
    assert "SIREN" in rail_watch.format_alert(alerts[0])


def test_a_reopen_whose_edge_has_gone_is_not_worth_waking_anyone() -> None:
    reopens = [{"venue": "Kucoin", "token": "SIREN", "direction": "deposit"}]
    assert rail_watch.alertable_reopens(reopens, _market(executable_spread_pct=0.4)) == []


def test_a_route_still_blocked_elsewhere_is_not_alertable() -> None:
    """A reopened withdrawal does not help while the other side's deposits are shut."""
    reopens = [{"venue": "Kucoin", "token": "SIREN", "direction": "deposit"}]
    assert rail_watch.alertable_reopens(reopens, _market(deliverable=False)) == []


def test_the_reopen_must_be_on_the_side_that_needs_it() -> None:
    """You withdraw from the leg you buy and deposit into the leg you sell."""
    deposit_side = [{"venue": "OKX DEX 1", "token": "SIREN", "direction": "deposit"}]
    assert rail_watch.alertable_reopens(deposit_side, _market()) == []
    withdraw_side = [{"venue": "OKX DEX 1", "token": "SIREN", "direction": "withdraw"}]
    assert len(rail_watch.alertable_reopens(withdraw_side, _market())) == 1


def test_first_run_records_a_baseline_without_shouting(tmp_path, monkeypatch) -> None:
    """Otherwise every already-open rail on the board reads as a fresh reopening."""
    monkeypatch.setattr(rail_watch.public_rails, "load_public_rails", lambda *a, **k: _rails(deposit=True))
    sent: list[str] = []
    watcher = rail_watch.RailReopenWatcher(
        state_path=tmp_path / "state.json", notify=sent.append
    )
    assert watcher.check_once()["status"] == "baseline_recorded"
    assert sent == []
    assert json.loads((tmp_path / "state.json").read_text())["rails"]


def test_watcher_announces_a_reopen_that_still_pays(tmp_path, monkeypatch) -> None:
    (tmp_path / "state.json").write_text(json.dumps({"rails": _rails(deposit=False)}))
    monkeypatch.setattr(rail_watch.public_rails, "load_public_rails", lambda *a, **k: _rails(deposit=True))
    monkeypatch.setattr(rail_watch.api_spreads, "load_spreads", lambda **k: _market())
    sent: list[str] = []
    watcher = rail_watch.RailReopenWatcher(state_path=tmp_path / "state.json", notify=sent.append)
    summary = watcher.check_once()
    assert summary == {"status": "ok", "reopened": 1, "alerted": 1, "tokens": ["SIREN"]}
    assert "RAIL REOPENED" in sent[0] and "Kucoin" in sent[0]


def test_the_same_reopen_is_not_announced_twice(tmp_path, monkeypatch) -> None:
    (tmp_path / "state.json").write_text(json.dumps({"rails": _rails(deposit=False)}))
    monkeypatch.setattr(rail_watch.public_rails, "load_public_rails", lambda *a, **k: _rails(deposit=True))
    monkeypatch.setattr(rail_watch.api_spreads, "load_spreads", lambda **k: _market())
    sent: list[str] = []
    watcher = rail_watch.RailReopenWatcher(state_path=tmp_path / "state.json", notify=sent.append)
    watcher.check_once()
    watcher.check_once()
    assert len(sent) == 1, "state must advance so a steady-open rail stays quiet"


def test_the_watcher_is_actually_wired_into_the_service() -> None:
    """A watcher nobody starts is a watcher that never fires."""
    import inspect
    from scripts import run_spreadboard_service

    source = inspect.getsource(run_spreadboard_service.main)
    assert "rail_watch.RailReopenWatcher" in source
    assert "rail_reopen_worker.start()" in source
    assert "rail_reopen_worker.stop()" in source
