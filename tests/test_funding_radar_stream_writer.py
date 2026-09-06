"""Atomic radar writes preserve records and avoid whole-payload encoding."""
import json

import pytest

from spreadboard import funding_radar


def test_streamed_radar_bytes_match_existing_format(tmp_path):
    payload = {'schema': funding_radar.SCHEMA, 'updated_at': '2026-09-06T08:29:15+00:00',
               'retention_days': 30, 'records': {
                   'CUSTOM:abc': {'route': {'token': '龙虾', 'rate': 1e-18}, 'windows': {'1d': None}},
                   'quoted"key': {'route': {'token': 'GPRO', 'rate': -.1}, 'windows': {'7d': 1.5}},
               }}
    path = tmp_path / 'radar.json'
    funding_radar._write_atomic(path, payload)
    assert path.read_bytes() == json.dumps(payload, separators=(',', ':')).encode()


def test_encoding_failure_keeps_previous_generation_and_cleans_partial_file(tmp_path):
    path = tmp_path / 'radar.json'
    path.write_bytes(b'previous-complete-generation')
    payload = {'schema': funding_radar.SCHEMA, 'records': {'valid': {'x': 1}, 'invalid': {1, 2}}}
    with pytest.raises(TypeError):
        funding_radar._write_atomic(path, payload)
    assert path.read_bytes() == b'previous-complete-generation'
    assert not path.with_suffix('.tmp').exists()


def test_actual_refresh_never_encodes_whole_radar(monkeypatch, tmp_path):
    original = json.dumps
    def bounded(value, *args, **kwargs):
        assert not (isinstance(value, dict) and 'records' in value), 'whole-radar encoding'
        return original(value, *args, **kwargs)
    monkeypatch.setattr(funding_radar.json, 'dumps', bounded)
    monkeypatch.setattr(funding_radar, '_window_snapshot', lambda route: {'1d': 1., '7d': None, '30d': None})
    path = tmp_path / 'radar.json'
    rows = [{'token': 'GPRO', 'route_key': f'route-{i}', 'long_venue': 'Bybit',
             'long_market_type': 'Futures', 'long_market_symbol': f'GPRO{i}/USDT:USDT',
             'short_venue': 'Hyperliquid', 'short_market_type': 'Futures',
             'short_market_symbol': 'IO-GPRO/USDC:USDC'} for i in range(10)]
    assert funding_radar.refresh(rows, cache_path=path, now=1_000_000) == 10
    assert len(json.loads(path.read_bytes())['records']) == 10


def test_empty_radar_is_valid_json(tmp_path):
    path = tmp_path / "radar.json"
    funding_radar._write_atomic(path, {"records": {}})
    assert path.read_bytes() == b'{"records":{}}'
