"""Actual store/restore reads remain bounded and reject partial generations."""
import hashlib
import json
import math
import time
from pathlib import Path

import orjson
import pytest

from spreadboard import materialized_views, server, streaming_index, warm_query_projection


def published(tmp_path, raw):
    store = materialized_views.Store(tmp_path)
    path = tmp_path / "live-route-index-test.json"
    path.write_bytes(raw)
    meta = {"schema": materialized_views.LIVE_ROUTE_SCHEMA, "file": path.name,
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "built_at_unix": 1, "source_signature": {"board_path": str(tmp_path / "board")}}
    store.live_route_pointer_path.write_text(json.dumps(meta))
    return store, path, meta


def test_actual_store_streams_multichunk_unicode_without_reading_whole_artifact(tmp_path, monkeypatch):
    rows = {"龍蝦|one": {"token": "龍蝦", "text": "escaped \\\" {} \\n 龍" * 10000,
                         "nested": {"price": 1.2345678901234567, "list": [None, True, -0.0]},
                         "quote_ts_us": 1788580860369770, "uint64": 2**64-1,
                         "signed_min": -(2**63)},
            "two": {"token": "OPENAI", "nested": {"price": 2.0}}}
    store, path, _ = published(tmp_path, b" \n\t" + orjson.dumps(rows))
    original_open, original_read_bytes = Path.open, Path.read_bytes
    sizes = []

    class Guarded:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, size=-1):
            assert 0 <= size <= 65536, "unbounded artifact read"
            sizes.append(size)
            return self.source.read(size)

        def seek(self, offset):
            return self.source.seek(offset)

    def open_file(candidate, *args, **kwargs):
        source = original_open(candidate, *args, **kwargs)
        return Guarded(source) if candidate == path else source

    def read_bytes(candidate):
        assert candidate != path, "whole artifact allocation at the store call site"
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    loaded = store.live_route_index(board_path=tmp_path / "board")
    assert loaded == rows
    assert type(loaded["龍蝦|one"]["uint64"]) is int
    assert type(loaded["two"]["nested"]["price"]) is float
    assert math.copysign(1, loaded["龍蝦|one"]["nested"]["list"][-1]) == -1
    assert sizes.count(65536) >= 2
    assert type(loaded["two"]) is dict
    first_key = next(k for k in loaded["龍蝦|one"] if k == "nested")
    second_key = next(k for k in loaded["two"] if k == "nested")
    assert first_key is second_key, "repeated field names were retained separately"


@pytest.mark.parametrize("raw", [b'[]', b'[{"a":{}}]', b'null', b'"scalar"', b'3',
    b'{"a":[]}', b'{"a":5}', b'{"a":{}} {"b":{}}', b'{"a":{}} trailing',
    b'{"a":{"n":1e400}}', b'{"a":{"n":18446744073709551616}}',
    b'{"a":{"n":-9223372036854775809}}', b'{"a":{"n":01}}',
    b'{"a":{"n":NaN}}', b'{"a":{"n":"\xff"}}', b'{"a":{', b''])
def test_actual_store_rejects_invalid_roots_numbers_and_incomplete_streams(tmp_path, raw):
    store, _, _ = published(tmp_path, raw)
    assert store.live_route_index() is None


@pytest.mark.parametrize("damage", ["checksum", "size", "board"])
def test_failed_actual_restore_keeps_previous_live_generation(tmp_path, monkeypatch, damage):
    old = {"old": {"route_key": "old", "token": "OLD", "route_kind": "FUTURES",
                   "executable_spread_pct": 1.0, "quote_ts_us": int(time.time()*1_000_000)}}
    store, _, meta = published(tmp_path, orjson.dumps({"new": {"token": "NEW"}}))
    if damage == "checksum":
        meta["sha256"] = "0" * 64
    elif damage == "size":
        meta["bytes"] += 1
    else:
        meta["source_signature"]["board_path"] = str(tmp_path / "other")
    store.live_route_pointer_path.write_text(json.dumps(meta))
    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    monkeypatch.setattr(server, "_ROUTE_INDEX", {"signature": None, "rows": old})
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install(old)
    assert universe.target_rows(route_keys=["old"])[0][0]["token"] == "OLD"
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    assert server.restore_materialized_route_index(tmp_path / "board") == 0
    assert server._ROUTE_INDEX["rows"] is old
    assert universe.target_rows(route_keys=["old"])[0][0]["token"] == "OLD"


def test_actual_store_bounds_shared_fields_without_dropping_unique_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(streaming_index, "SHARED_KEY_LIMIT", 2)
    rows = {"route": {f"field{i}": {"nested": [i]} for i in range(10)}}
    store, _, _ = published(tmp_path, orjson.dumps(rows))
    original = streaming_index._shared_fields
    pools = []

    def checked(value, keys):
        result = original(value, keys)
        assert len(keys) <= 2
        pools.append(keys)
        return result

    monkeypatch.setattr(streaming_index, "_shared_fields", checked)
    assert store.live_route_index() == rows
    assert len(pools[0]) == 2
    first_pool = pools[0]
    pools.clear()
    assert store.live_route_index() == rows
    assert pools[0] is not first_pool, "field pool persisted across generations"


def test_actual_store_checks_bytes_consumed_after_preflight(tmp_path, monkeypatch):
    store, path, _ = published(tmp_path, b'{"a":{}}')
    status = store.live_route_index_status()
    path.write_bytes(b'{"a":{}} ')
    status["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(store, "live_route_index_status", lambda: status)
    assert store.live_route_index() is None


def test_actual_store_shares_repeated_strings_without_sharing_mutable_rows(tmp_path):
    venue = 'Repeated exchange name 龍蝦 ' * 4
    symbol = 'A-LONG-EXACT-MARKET-SYMBOL/USDT:USDT'
    rows = {key: {'route_key':key, 'long_venue':venue, 'short_venue':venue,
                  'long_market_symbol':symbol, 'nested':{'value':1}}
            for key in ('first-route','second-route')}
    store, _, _ = published(tmp_path, orjson.dumps(rows))
    loaded = store.live_route_index()
    assert loaded == rows
    first, second = loaded.values()
    assert first['long_venue'] is second['long_venue']
    assert first['long_market_symbol'] is second['long_market_symbol']
    first['nested']['value'] = 2
    first['long_venue'] = 'changed'
    assert second['nested']['value'] == 1
    assert second['long_venue'] == venue


def test_actual_store_bounds_and_releases_value_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(streaming_index, 'SHARED_VALUE_LIMIT', 2)
    rows = {f'route-{i}': {'token':f'token-{i}', 'long_venue':'shared venue'} for i in range(20)}
    store, _, _ = published(tmp_path, orjson.dumps(rows))
    pools = []
    original = streaming_index._shared_route_values
    def checked(row, values):
        result = original(row, values)
        assert len(values) <= 2
        pools.append(values)
        return result
    monkeypatch.setattr(streaming_index, '_shared_route_values', checked)
    assert store.live_route_index() == rows
    first = pools[0]
    assert len(first) == 2
    pools.clear()
    assert store.live_route_index() == rows
    assert pools[0] is not first
