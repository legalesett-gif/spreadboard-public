"""Recent timestamps must not hide obsolete market-definition semantics."""
import json
import os

import pytest

from scripts import run_spreadboard_service as service
from spreadboard import chart_catalog


@pytest.mark.parametrize('revision,refresh_first', [(None, True), (1, True), (2, True), (3, True), (4, False)])
def test_actual_catalog_loop_refreshes_old_definitions_before_delay(tmp_path, monkeypatch, revision, refresh_first):
    path = tmp_path / 'chart_market_catalog.json'
    path.write_text(json.dumps({'definition_revision': revision, 'markets': []}))
    os.utime(path, (1000, 1000))
    monkeypatch.setattr(service, 'RUNTIME_DIR', tmp_path)
    monkeypatch.setattr(service.time, 'time', lambda: 1060)
    monkeypatch.setenv('SPREADBOARD_CHART_CATALOG_SECONDS', '3600')
    calls = []
    monkeypatch.setattr(service, '_artifact_worker', lambda *args: calls.append(args))

    class StopAfterWait:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            calls.append(seconds)
            self.stopped = True
            return True

    loop = service.RefreshLoop(7200)
    loop.stop_event = StopAfterWait()
    loop.run_chart_catalog()
    assert calls == ([('chart-catalog', '--workers', '4'), 3600.0] if refresh_first else [3540.0])


@pytest.mark.parametrize('failed', [None, 'Hyperliquid|Futures', 'BitMart|Spot', 'BitMart|Futures', 'WhiteBIT|Futures', 'WhiteBIT|Spot'])
def test_refresh_marks_new_definitions_only_when_required_sources_succeeded(tmp_path, monkeypatch, failed):
    path = tmp_path / 'chart_market_catalog.json'
    path.write_text(json.dumps({'markets': [], 'definition_revision': 1}))
    venues = {'Binance': 'binance', 'Bitget': 'bitget', 'Bybit': 'bybit', 'Hyperliquid': 'hyperliquid', 'BitMart': 'bitmart', 'WhiteBIT': 'whitebit'}
    monkeypatch.setattr(chart_catalog, 'VENUE_IDS', venues)
    monkeypatch.setattr(chart_catalog, 'NATIVE_FUTURES_VENUES', set(venues))
    monkeypatch.setattr(chart_catalog, 'NATIVE_SPOT_VENUES', {'BitMart', 'WhiteBIT'})
    monkeypatch.setattr(chart_catalog, 'CATALOGUE_ONLY_SPOT_VENUES', set())
    monkeypatch.setattr(chart_catalog, 'dex_market_entries', list)
    def job(venue, market_type):
        if failed == f'{venue}|{market_type}':
            raise RuntimeError('provider unavailable')
        return [{'token': 'AAA', 'venue': venue, 'market_type': market_type, 'symbol': 'AAA/USDT:USDT'}]
    monkeypatch.setattr(chart_catalog, '_load_job_subprocess', job)
    result = chart_catalog.refresh(path)
    assert result.get('definition_revision') == (1 if failed else 4)
