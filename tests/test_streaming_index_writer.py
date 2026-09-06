"""Bounded index encoding preserves exact bytes and atomic publication."""
import hashlib

import pytest

from spreadboard import materialized_views as m


@pytest.mark.parametrize('rows', [{}, {'龙虾': {'z': [None, 1.25, 'é'], 'a': True}, 'A': {'quote': '"\\\n'}}])
def test_index_writer_matches_original_bytes_and_roundtrips(tmp_path, rows):
    store = m.Store(tmp_path)
    expected = m._json_bytes(rows)
    meta = store.write_live_route_index(rows, source_signature={})
    assert (tmp_path / meta['file']).read_bytes() == expected
    assert meta['bytes'] == len(expected)
    assert meta['sha256'] == hashlib.sha256(expected).hexdigest()
    assert store.live_route_index() == rows


def test_actual_publisher_never_serializes_the_whole_route_dictionary(tmp_path, monkeypatch):
    rows = {str(i): {'token':str(i), 'value':'x'*1000} for i in range(100)}
    original = m._json_bytes
    def encode(value):
        assert value is not rows, 'whole-index encoding overlaps all route objects'
        return original(value)
    monkeypatch.setattr(m, '_json_bytes', encode)
    store = m.Store(tmp_path)
    store.write_live_route_index(rows, source_signature={})
    assert store.live_route_index() == rows


def test_failed_row_serialization_keeps_previous_pointer_readable(tmp_path, monkeypatch):
    store = m.Store(tmp_path)
    previous = {'old': {'token':'OLD'}}
    store.write_live_route_index(previous, source_signature={})
    pointer = store.live_route_pointer_path.read_bytes()
    broken = {'token':'BROKEN'}
    original = m._json_bytes
    def encode(value):
        if value is broken:
            raise RuntimeError('serialization failed')
        return original(value)
    monkeypatch.setattr(m, '_json_bytes', encode)
    with pytest.raises(RuntimeError, match='serialization failed'):
        store.write_live_route_index({'a':{'token':'OK'}, 'b':broken}, source_signature={})
    assert store.live_route_pointer_path.read_bytes() == pointer
    assert store.live_route_index() == previous


def test_supplied_encoding_remains_supported_without_reencoding(tmp_path, monkeypatch):
    rows = {'A':{'token':'A'}}
    raw = m._json_bytes(rows)
    monkeypatch.setattr(m, '_write_index_rows', lambda *a: pytest.fail('already encoded'))
    store = m.Store(tmp_path)
    meta = store.write_live_route_index(rows, source_signature={}, encoded=raw)
    assert (tmp_path / meta['file']).read_bytes() == raw
    assert store.live_route_index() == rows
