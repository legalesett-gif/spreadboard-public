from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts import live_route_index_worker as worker
from spreadboard import materialized_views, streaming_index


def route(key='old', token='X'):
    return {'route_key': key, 'token': token, 'route_kind': 'FUTURES',
            'long_venue': 'Gate', 'short_venue': 'Mexc',
            'long_market_type': 'Futures', 'short_market_type': 'Futures',
            'long_market_symbol': f'{token}/USDT:USDT', 'short_market_symbol': f'{token}/USDT:USDT'}


@pytest.mark.parametrize('duplicate_clear', [False, True])
def test_compact_safety_matches_full_previous_generation(monkeypatch, duplicate_clear):
    rich = {**route(), 'price': 123, 'notes': {'unused': ['large'] * 100},
            'blockers': ['depth_unverified', 'identity_unresolved'], 'mirage_guarded': True,
            'identity_warning': True, 'identity_mismatch': True, 'thin_book': True,
            'deliverable': False, 'tokenized_guard': {'rankable': False, 'reasons': ['oracle_missing']}}
    previous = {'old': rich}
    if duplicate_clear:
        previous['later'] = route('later')
    compact = {key: worker._prior_rebuild_evidence(row) for key, row in previous.items()}
    assert 'price' not in compact['old'] and 'notes' not in compact['old']
    fresh = {**route('fresh'), 'price': 456, 'depth_weighted_spread_pct': 1.0,
             'matched_size_notional_usd': worker.api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD}
    monkeypatch.setattr(worker.api_spreads.token_metadata, 'load_token_metadata', dict)
    monkeypatch.setattr(worker.api_spreads, '_complete_current_catalogue_rows',
                        lambda *a, **kw: ([copy.deepcopy(fresh)], {}))
    monkeypatch.setattr(worker, '_current_dex_rows', lambda *a, **kw: [])
    actual, _ = worker._current_generation_rows(compact)
    expected, _ = worker._current_generation_rows(previous)
    assert actual == expected
    row = next(iter(actual.values()))
    assert row['price'] == 456
    assert 'depth_unverified' not in (row.get('blockers') or [])
    if duplicate_clear:
        assert row.get('mirage_guarded') is not True
    else:
        assert row['tokenized_guard']['rankable'] is False
        assert row['deliverable'] is False


def test_missing_rows_keep_full_payload_and_current_identity_wins(tmp_path, monkeypatch):
    store = materialized_views.Store(tmp_path)
    board = tmp_path / 'board'
    old = route()
    missing = {**route('missing', 'Y'), 'notes': {'proof': ['full evidence']}, 'price': 12}
    removed = route('removed', 'Z')
    store.write_live_route_index({'old': old, 'missing': missing, 'removed': removed},
                                 source_signature={'board_path': str(board)})
    generation = store.live_route_index_status()
    markets = [{'venue': r[f'{side}_venue'], 'market_type': 'Futures', 'symbol': r[f'{side}_market_symbol']}
               for r in (old, missing) for side in ('long', 'short')]
    monkeypatch.setattr(worker.chart_catalog, 'load', lambda: {'markets': markets})
    current = {'new-key': {**old, 'route_key': 'new-key', 'price': 55}}
    retained = worker._missing_previous_rows(store, board, generation, current, same_structural_generation=True)
    assert retained == {'missing': missing}
    assert worker._merge_by_economic_identity(retained, current) == {**retained, **current}


def test_pinned_selection_does_not_follow_new_pointer_and_validates_skipped_bytes(tmp_path):
    store = materialized_views.Store(tmp_path)
    first = {'keep': route(), 'skip': route('skip', 'Y')}
    store.write_live_route_index(first, source_signature={})
    pinned = store.live_route_index_status()
    store.write_live_route_index({'replacement': route('replacement')}, source_signature={})
    select = lambda row: row if row['token'] == 'X' else None
    assert store.live_route_index(generation=pinned, select_row=select) == {'keep': first['keep']}
    path = tmp_path / pinned['file']
    raw = path.read_bytes()
    # Same byte length, altered skipped row: selector cannot hide corruption.
    path.write_bytes(raw.replace(b'Y/USDT', b'Z/USDT'))
    assert store.live_route_index(generation=pinned, select_row=select) is None


def test_selected_reader_still_rejects_invalid_numeric_data(tmp_path):
    raw = json.dumps({'skipped': {'overflow': 2**80}}).encode()
    path = tmp_path / 'index.json'
    path.write_bytes(raw)
    assert streaming_index.read_index(path, expected_bytes=len(raw), expected_sha256=hashlib.sha256(raw).hexdigest(),
                                      select_row=lambda _: None) is None


def test_second_pass_failure_does_not_publish_candidate(tmp_path, monkeypatch):
    store = materialized_views.Store(tmp_path)
    board = tmp_path / 'board'
    signature = {'board_path': str(board)}
    store.write_live_route_index({'old': route()}, source_signature=signature)
    pointer = store.live_route_pointer_path.read_bytes()
    generation = store.live_route_index_status()
    monkeypatch.setattr(worker, 'source_signature', lambda _: signature)
    monkeypatch.setattr(worker.chart_catalog, 'load', lambda: {'markets': []})
    def fresh(_previous):
        (tmp_path / generation['file']).write_text('corrupt')
        return {'fresh': route('fresh')}, {}
    monkeypatch.setattr(worker, '_current_generation_rows', fresh)
    with pytest.raises(RuntimeError, match='previous_index_changed'):
        worker.build(board, tmp_path)
    assert store.live_route_pointer_path.read_bytes() == pointer


@pytest.mark.parametrize('same_generation', [False, True])
def test_missing_dex_identity_bridges_only_same_structural_generation(tmp_path, monkeypatch, same_generation):
    store = materialized_views.Store(tmp_path)
    dex = {**route('dex'), 'route_kind': 'DEX-FUTURES', 'long_venue': 'OKX DEX',
           'long_market_type': 'Spot', 'dex_chain': '1', 'dex_contract': '0xabc',
           'notes': {'identity': {'verified': True}}, 'quote_ts_us': 123}
    store.write_live_route_index({'dex': dex}, source_signature={})
    monkeypatch.setattr(worker.chart_catalog, 'load', lambda: {'markets': []})
    retained = worker._missing_previous_rows(store, tmp_path/'board', store.live_route_index_status(), {},
                                            same_structural_generation=same_generation)
    assert retained == ({'dex': dex} if same_generation else {})


def test_filtered_duplicate_json_key_obeys_last_entry(tmp_path):
    raw = b'{"same":{"token":"X"},"same":{"token":"Y"}}'
    path = tmp_path/'index.json'
    path.write_bytes(raw)
    result = streaming_index.read_index(path, expected_bytes=len(raw), expected_sha256=hashlib.sha256(raw).hexdigest(),
                                        select_row=lambda row: row if row['token']=='X' else None)
    assert result == {}


def test_build_does_not_carry_old_economics_into_current_generation(tmp_path, monkeypatch):
    store = materialized_views.Store(tmp_path)
    board = tmp_path/'board'
    signature = {'board_path': str(board)}
    old = {**route(), 'price': 12, 'notes': {'unneeded_old_payload': ['large'] * 100},
           'identity_warning': True}
    store.write_live_route_index({'old': old}, source_signature=signature)
    monkeypatch.setattr(worker, 'source_signature', lambda _: signature)
    monkeypatch.setattr(worker.chart_catalog, 'load', lambda: {'markets': []})
    def fresh(previous):
        assert 'price' not in previous['old'] and 'notes' not in previous['old']
        assert previous['old']['identity_warning'] is True
        return {'new': {**route('new'), 'price': 30, 'identity_warning': True}}, {}
    monkeypatch.setattr(worker, '_current_generation_rows', fresh)
    worker.build(board, tmp_path)
    assert store.live_route_index()['new']['price'] == 30
