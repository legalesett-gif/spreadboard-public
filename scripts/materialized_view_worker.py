#!/usr/bin/env python3
"""Build every principal SpreadBoard screen as one atomic disk generation."""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from scripts import run_spreadboard_service as service
from spreadboard import (
    api_spreads,
    funding_catalog,
    funding_history_demand,
    funding_radar,
    materialized_views,
    server,
    telegram_queries,
)


def _funding_priority_legs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Exact legs behind every route a member can see in a warm Funding view."""

    return funding_history_demand.payload_legs(payload)


def _enqueue_funding_priority(payload: dict[str, Any]) -> None:
    funding_history_demand.enqueue_payload(payload)


def _signature(path: Path | str) -> list[int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


def source_signature(board_path: Path) -> dict[str, Any]:
    return {
        "board_path": str(board_path.resolve()),
        "board": _signature(board_path),
        "discovery": _signature(api_spreads.DEFAULT_API_DISCOVERY_PATH),
        "chart_catalog": _signature(service.RUNTIME_DIR / "chart_market_catalog.json"),
        "metadata": _signature(api_spreads.token_metadata.DEFAULT_CACHE_PATH),
        "rails": _signature(api_spreads.public_rails.DEFAULT_CACHE_PATH),
    }


def _release_memory(*, keep_rows: bool) -> None:
    with server._MARKET_CACHE_LOCK:
        server._MARKET_CACHE.clear()
        server._MARKET_CACHE_INFLIGHT.clear()
    with api_spreads._SNAPSHOT_CACHE_LOCK:
        api_spreads._RESULT_CACHE.clear()
        if not keep_rows:
            api_spreads._ROW_CACHE.clear()
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _cacheable(payload: dict[str, Any]) -> bool:
    return payload.get("status") != "warming" and server._market_payload_cacheable(payload)


def _compact_funding_navigation(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist only the exact-pair preview that normal Funding HTML renders."""

    if not ((payload.get("filters") or {}).get("funding_only")):
        return payload
    compact = dict(payload)
    groups: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for original in payload.get("groups") or []:
        if not isinstance(original, dict):
            continue
        group = dict(original)
        routes = list(group.get("routes") or [])
        preview = routes[: server.FUNDING_PAIR_PREVIEW_LIMIT]
        group["route_count"] = max(len(routes), int(group.get("route_count") or 0))
        group["routes"] = preview
        group["materialized_route_preview"] = True
        groups.append(group)
        rows.extend(preview)
    compact["groups"] = groups
    compact["rows"] = rows
    compact["materialized_route_preview_limit"] = server.FUNDING_PAIR_PREVIEW_LIMIT
    compact["materialized_exact_token_source"] = "complete_catalogue_plus_shared_books"
    return compact


def build(board_path: Path, output_root: Path) -> dict[str, Any]:
    queries = service._materialized_view_queries()
    initial_signature = source_signature(board_path)
    store = materialized_views.Store(output_root)
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=queries,
        source_signature=initial_signature,
    )
    started = time.monotonic()
    dex_archive_routes: list[dict[str, Any]] = []
    try:
        # Charts and arbitrary filters need the full canonical lookup, not only
        # the top 500 tokens. Reuse the independently published fast index when
        # it covers this exact structural source; otherwise build the same
        # direct, ungrouped index and publish it immediately. The former
        # ``load_spreads(limit=None)`` path spent minutes grouping and attaching
        # funding history that an index never reads.
        live_meta = store.live_route_index_status()
        live_source = (
            live_meta.get("source_signature")
            if isinstance(live_meta.get("source_signature"), dict)
            else {}
        )
        shared_keys = (
            "board_path",
            "board",
            "discovery",
            "chart_catalog",
            "metadata",
            "rails",
        )
        route_index = (
            store.live_route_index(board_path=board_path)
            if live_meta.get("ready")
            and all(live_source.get(key) == initial_signature.get(key) for key in shared_keys)
            else None
        )
        source_health: dict[str, Any] = {}
        publish_live_index = route_index is None
        if publish_live_index:
            route_index, source_health = api_spreads.load_public_route_index()
        # A prior artifact or a pinned custom chart can carry retired product
        # permutations even after the broad catalogue was filtered. Enforce
        # the public boundary before resident installation or disk publication.
        route_index = {
            key: row
            for key, row in route_index.items()
            if str(row.get("route_kind") or "").upper()
            not in api_spreads.RETIRED_ROUTE_KINDS
        }
        # Encode the structural index ONCE. Both writers below persist exactly
        # these bytes, and each encoding of ~130k rows is about 300MB; holding
        # two of them beside the decoded index is what pushed this worker to
        # 2,162MB inside a 4GiB cgroup that already carries a ~2.2GB base.
        encoded_route_index = materialized_views._json_bytes(route_index)
        if publish_live_index:
            store.write_live_route_index(
                route_index,
                source_signature={key: initial_signature.get(key) for key in shared_keys},
                encoded=encoded_route_index,
            )
        writer.write_route_index(route_index, encoded=encoded_route_index)
        del encoded_route_index
        template = store.payload_for(
            {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]},
            board_path=board_path,
        ) or {
            "ok": True,
            "source_health": {"canonical_api": source_health},
            "exchange_options": [],
            "route_kind_counts": {},
            "asset_class_counts": {},
            "route_kind_token_counts": {},
            "lane_token_counts": {},
            "top_edges": [],
            "top_funding": [],
        }
        server.warm_query_projection.LIVE_UNIVERSE.install(
            route_index,
            template=template,
        )
        live_status = server.warm_query_projection.LIVE_UNIVERSE.refresh()
        if route_index and not live_status.get("ready"):
            raise RuntimeError(f"live_route_projection_unavailable:{live_status}")
        with server._ROUTE_INDEX_LOCK:
            server._ROUTE_INDEX["signature"] = None
            server._ROUTE_INDEX["rows"] = route_index
        _release_memory(keep_rows=True)

        # Current scanner lanes share one parsed discovery row cache. Write each
        # payload immediately, retain only that row cache, and return every
        # grouped result to the allocator before starting the next lane.
        ordinary = [
            query
            for query in queries
            if not query.get("funding_only")
            or query in service.FUNDING_ARCHIVE_QUERIES
        ]
        funding = [query for query in queries if query not in ordinary]
        for query in ordinary:
            payload = server.api_market_spreads(
                board_path, {**query, "no_cache": ["1"]}
            )
            if not _cacheable(payload):
                raise RuntimeError(f"uncacheable_view:{query}")
            _enqueue_funding_priority(payload)
            if query in service.FUNDING_ARCHIVE_QUERIES:
                dex_archive_routes.extend(
                    route
                    for group in payload.get("groups") or []
                    if isinstance(group, dict)
                    for route in group.get("routes") or []
                    if isinstance(route, dict)
                )
            writer.write_view(query, _compact_funding_navigation(payload))
            del payload
            _release_memory(keep_rows=True)

        # Complete funding is a separate all-market catalogue. Drop the large
        # discovery parse first so the two universes never peak together.
        _release_memory(keep_rows=False)
        funding_catalog.clear_cache()
        restored_funding = funding_catalog.restore_persisted_cache()
        if not restored_funding.get("ready"):
            raise RuntimeError("persisted_complete_funding_catalog_unavailable")
        server.mark_historical_dex_archive_ready()
        for query in funding:
            payload = server.api_market_spreads(
                board_path, {**query, "no_cache": ["1"]}
            )
            if not _cacheable(payload):
                raise RuntimeError(f"uncacheable_view:{query}")
            _enqueue_funding_priority(payload)
            writer.write_view(query, _compact_funding_navigation(payload))
            del payload
            _release_memory(keep_rows=False)

        # Intel is smaller but otherwise has the same restart penalty. Only its
        # default screen is persisted; token-specific searches remain dynamic.
        writer.write_extra("intel-default", server.api_intel(board_path, {"no_cache": ["1"]}))

        final_signature = source_signature(board_path)
        if final_signature != initial_signature:
            raise RuntimeError("source_generation_changed_during_build")
        manifest = writer.publish()
        if dex_archive_routes:
            # The compact navigation payload previews three pairs per token,
            # but exact historical re-ranking needs every eligible OKX DEX
            # direction. Publish the full small DEX universe only after the
            # source-coherent generation succeeds; radar retention then keeps
            # cooled leaders discoverable without a request-owned broad scan.
            funding_radar.refresh(dex_archive_routes)

        # Telegram already has durable snapshots; replace them only after the
        # new website generation is complete so a failed build cannot publish a
        # mixed bot universe.
        spread = store.payload_for(
            {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]}
        )
        if spread:
            telegram_queries.replace_payload(spread)
        funding_payloads = [
            store.payload_for(query)
            for query in service.WARM_QUERIES
            if query.get("funding_only")
            and not query.get("funding_window")
        ]
        if funding_payloads and all(funding_payloads):
            telegram_queries.replace_funding_payloads(
                [payload for payload in funding_payloads if payload]
            )
        return {
            "status": "ok",
            "generation": manifest["generation"],
            "views": len(manifest["views"]),
            "routes": int((manifest.get("route_index") or {}).get("row_count") or 0),
            "seconds": round(time.monotonic() - started, 3),
        }
    except Exception:
        writer.abort()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-path", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=materialized_views.DEFAULT_ROOT)
    args = parser.parse_args()
    try:
        summary = build(args.board_path, args.output_root)
    except Exception as exc:  # noqa: BLE001 - parent retains last complete generation.
        print(
            json.dumps(
                {"status": "failed", "error": type(exc).__name__, "detail": str(exc)[:300]}
            ),
            flush=True,
        )
        return 1
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
