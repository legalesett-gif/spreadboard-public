"""The structural index must be encoded once, not once per writer.

The collector's view worker peaked at 2,162MB inside a 4GiB cgroup that already
carries a ~2.2GB steady base, so base + this child exceeded the limit. Part of
that peak was encoding the same ~130k-row index twice: once for the standalone
live index and once for the generation's own copy. Each encoding is a ~300MB
bytes object.

orjson with sorted keys is deterministic, so a single encoding serves both
writers -- but only if both actually persist the identical bytes, which these
tests pin.
"""

from __future__ import annotations

import inspect

from spreadboard import materialized_views


def _rows() -> dict[str, dict[str, object]]:
    return {
        f"T{i}|Gate|Futures|Mexc|Futures": {
            "route_key": f"T{i}|Gate|Futures|Mexc|Futures",
            "token": f"T{i}",
            "route_kind": "FUTURES",
            "executable_spread_pct": float(i),
        }
        for i in range(50)
    }


def test_encoding_the_same_rows_is_deterministic() -> None:
    """Reuse is only sound if two encodings would have been identical."""

    rows = _rows()
    assert materialized_views._json_bytes(rows) == materialized_views._json_bytes(rows)


def test_the_generation_writer_persists_the_supplied_encoding(tmp_path) -> None:
    store = materialized_views.Store(root=tmp_path / "views")
    writer = materialized_views.GenerationWriter(
        store, required_queries=[], source_signature={}
    )
    rows = _rows()
    encoded = materialized_views._json_bytes(rows)

    writer.write_route_index(rows, encoded=encoded)

    written = (writer.staging / "route-index.json").read_bytes()
    assert written == encoded
    assert writer.route_index_meta["bytes"] == len(encoded)
    assert writer.route_index_meta["sha256"] == materialized_views._sha256(encoded)
    assert writer.route_index_meta["row_count"] == len(rows)


def test_supplying_an_encoding_matches_encoding_internally(tmp_path) -> None:
    """The reused path must produce byte-identical output to the old one."""

    rows = _rows()
    store_a = materialized_views.Store(root=tmp_path / "a")
    store_b = materialized_views.Store(root=tmp_path / "b")
    a = materialized_views.GenerationWriter(store_a, required_queries=[], source_signature={})
    b = materialized_views.GenerationWriter(store_b, required_queries=[], source_signature={})

    a.write_route_index(rows)  # encodes internally, as before
    b.write_route_index(rows, encoded=materialized_views._json_bytes(rows))

    assert (a.staging / "route-index.json").read_bytes() == (
        b.staging / "route-index.json"
    ).read_bytes()
    assert a.route_index_meta["sha256"] == b.route_index_meta["sha256"]


def test_the_live_index_writer_accepts_an_encoding(tmp_path) -> None:
    store = materialized_views.Store(root=tmp_path / "live")
    rows = _rows()
    encoded = materialized_views._json_bytes(rows)

    meta = store.write_live_route_index(
        rows, source_signature={"board_path": "b"}, encoded=encoded
    )

    assert meta["bytes"] == len(encoded)
    assert meta["sha256"] == materialized_views._sha256(encoded)
    assert (store.root / meta["file"]).read_bytes() == encoded


def test_the_worker_encodes_once_and_releases_it() -> None:
    from scripts import materialized_view_worker

    source = inspect.getsource(materialized_view_worker)
    assert source.count("_json_bytes(route_index)") == 1, (
        "the index must be encoded exactly once in the worker"
    )
    assert "encoded=encoded_route_index" in source
    assert "del encoded_route_index" in source, (
        "a ~300MB encoding must not outlive its writers"
    )
