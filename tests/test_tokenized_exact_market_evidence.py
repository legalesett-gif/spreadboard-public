"""Reviewed stock evidence must authorize only the exact public market pair."""
import json

import pytest

from spreadboard import tokenized_assets


def _case(tmp_path):
    common = {
        'underlying_symbol': 'GPRO',
        'instrument_type': 'equity_perpetual',
        'oracle_source': 'Fixture published index rules',
        'trading_hours': '24/7',
        'source_url': 'https://example.com/reviewed-specification',
    }
    markets = [
        {**common, 'venue': 'Bybit', 'market_type': 'Futures', 'market_symbol': 'GPRO/USDT:USDT'},
        {**common, 'venue': 'Hyperliquid', 'market_type': 'Futures', 'market_symbol': 'IO-GPRO/USDC:USDC'},
    ]
    entry = {**common, 'venues': ['Bybit', 'Hyperliquid'], 'markets': markets}
    route = {'token': 'GPRO', 'asset_class': 'tokenized'}
    for side, market in zip(('long', 'short'), markets):
        route.update({f'{side}_{key}': market[key] for key in ('venue', 'market_type', 'market_symbol')})
    path = tmp_path / 'registry.json'
    return path, entry, route


def _classify(path, entry, route):
    path.write_text(json.dumps({'assets': {'GPRO': entry}}))
    tokenized_assets._CACHE.update(stamp=None, assets={})
    return tokenized_assets.classify(route, path=path)


def test_exact_reviewed_pair_and_reverse_verified_research_only(tmp_path):
    path, entry, route = _case(tmp_path)
    for candidate in (route, {**route, **{
        f'{side}_{key}': route[f'{other}_{key}']
        for side, other in [('long', 'short'), ('short', 'long')]
        for key in ('venue', 'market_type', 'market_symbol')
    }}):
        guard = _classify(path, entry, candidate)
        assert guard['status'] == 'verified'
        assert guard['reasons'] == []
        assert guard['execution_policy'] == 'research_only'


@pytest.mark.parametrize('changes', [
    {'short_market_symbol': 'XYZ-GPRO/USDC:USDC'},
    {'long_market_type': 'Spot', 'long_market_symbol': 'GPRO/USDT'},
    {'short_market_symbol': ''},
    {'short_venue': 'Gate'},
])
def test_label_and_venue_evidence_cannot_certify_different_market(tmp_path, changes):
    path, entry, route = _case(tmp_path)
    assert _classify(path, entry, {**route, **changes})['status'] == 'blocked'


@pytest.mark.parametrize('field', ['oracle_source', 'trading_hours', 'source_url', 'instrument_type'])
def test_each_leg_requires_its_own_evidence(tmp_path, field):
    path, entry, route = _case(tmp_path)
    entry['markets'][1][field] = ''
    guard = _classify(path, entry, route)
    assert guard['status'] == 'blocked'
    assert 'short_market_evidence_unresolved' in guard['reasons']


def test_conflicting_underlying_and_duplicate_mapping_fail_closed(tmp_path):
    path, entry, route = _case(tmp_path)
    entry['markets'][1]['underlying_symbol'] = 'UNRELATED'
    assert _classify(path, entry, route)['status'] == 'blocked'
    entry['markets'][1]['underlying_symbol'] = 'GPRO'
    entry['markets'].append(dict(entry['markets'][1]))
    assert _classify(path, entry, route)['status'] == 'blocked'
