from __future__ import annotations

from pathlib import Path

from scripts import funding_navigation_worker
from spreadboard import funding_catalog, funding_navigation, server


def _route(token: str, route_kind: str = "FUTURES") -> dict:
    return {
        "token": token,
        "route_key": f"{token}|Mexc|Futures|Gate|Futures",
        "route_kind": route_kind,
        "long_venue": "Mexc",
        "long_market_type": "Futures",
        "long_market_symbol": f"{token}/USDT:USDT",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "deliverable": True,
    }


def test_navigation_build_ranks_now_and_exact_windows_in_one_generation(monkeypatch) -> None:
    fast = _route("FAST")
    persistent = _route("PERSIST")
    monkeypatch.setattr(funding_catalog, "_complete_payloads", lambda: {"X": {}})
    monkeypatch.setattr(
        funding_catalog,
        "_all_routes",
        lambda **kwargs: [fast, persistent]
        if kwargs.get("route_kind") == "FUTURES"
        else [],
    )
    monkeypatch.setattr(
        funding_catalog.bulk_quotes,
        "load_funding",
        lambda: {
            "Mexc|FAST/USDT:USDT": {"rate_pct": 0.0, "interval_hours": 8, "age_seconds": 10},
            "Gate|FAST/USDT:USDT": {"rate_pct": 0.2, "interval_hours": 8, "age_seconds": 10},
            "Mexc|PERSIST/USDT:USDT": {"rate_pct": 0.0, "interval_hours": 8, "age_seconds": 10},
            "Gate|PERSIST/USDT:USDT": {"rate_pct": 0.1, "interval_hours": 8, "age_seconds": 10},
        },
    )
    monkeypatch.setattr(funding_catalog.venue_funding_history, "load", lambda: {"exact": {}})
    history = {
        "FAST": {"1d": 0.1, "7d": 0.2, "30d": None},
        "PERSIST": {"1d": 0.2, "7d": 1.4, "30d": 5.0},
    }
    monkeypatch.setattr(
        funding_catalog,
        "_window_value",
        lambda route, label, **_kwargs: history[route["token"]][label],
    )

    pages = funding_catalog.build_navigation_pages(limit=500, preview_limit=3)

    assert len(pages) == 12
    assert pages[("FUTURES", "now")]["groups"][0]["token"] == "FAST"
    assert pages[("FUTURES", "7d")]["groups"][0]["token"] == "PERSIST"
    assert pages[("FUTURES", "30d")]["groups"][0]["token"] == "PERSIST"
    assert pages[("FUTURES", "30d")]["window_token_counts"]["30d"] == 1
    assert pages[("FUTURES", "30d")]["groups"][0]["routes"][0][
        "funding_navigation_windows"
    ] == history["PERSIST"]


def test_principal_funding_request_uses_persisted_ranking_before_dynamic_catalog(
    monkeypatch, tmp_path: Path
) -> None:
    query = {
        "funding_only": ["1"],
        "kind": ["FUTURES"],
        "sort": ["funding"],
        "direction": ["desc"],
        "limit": ["25"],
        "offset": ["0"],
    }
    payload = {
        "ok": True,
        "filters": {"funding_only": True},
        "groups": [{"token": "READY", "routes": [_route("READY")]}],
        "rows": [_route("READY")],
        "summary": {},
        "pagination": {"limit": 500, "offset": 0},
        "source_health": {"canonical_api": {"status": "fresh"}},
    }

    class Store:
        def payload_for(self, _query, **_kwargs):
            return payload

    monkeypatch.setattr(server, "_FUNDING_NAVIGATION_STORE", Store())
    monkeypatch.setattr(
        server.funding_navigation,
        "status",
        lambda: {"generation": "ready", "built_at_unix": 1.0},
    )
    monkeypatch.setattr(
        server,
        "_expand_complete_funding_groups",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP request must not rank the complete catalogue")
        ),
    )
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda value: value)

    result = server.api_market_spreads(tmp_path / "board.jsonl", query)

    assert result["groups"][0]["token"] == "READY"
    assert result["funding_navigation"]["request_owned_exchange_work"] is False


def test_export_and_exact_token_keep_complete_dynamic_route_expansion() -> None:
    base = {
        "funding_only": ["1"],
        "kind": ["FUTURES"],
        "sort": ["funding"],
        "direction": ["desc"],
    }

    assert server._can_use_persisted_funding_navigation(
        base, limit=25, offset=0
    )
    assert not server._can_use_persisted_funding_navigation(
        {**base, "export": ["1"]}, limit=25, offset=0
    )
    assert not server._can_use_persisted_funding_navigation(
        {**base, "q": ["GUA"]}, limit=25, offset=0
    )


def test_navigation_query_matrix_is_complete() -> None:
    identities = {
        tuple((key, tuple(value)) for key, value in sorted(query.items()))
        for query in funding_navigation.QUERIES
    }

    assert len(identities) == 12


def test_worker_publishes_loaded_snapshot_when_live_source_advances(
    monkeypatch, tmp_path: Path
) -> None:
    signatures = iter(({"funding": [1, 1]}, {"funding": [2, 2]}))
    monkeypatch.setattr(
        funding_navigation_worker,
        "source_signature",
        lambda _path: next(signatures),
    )
    monkeypatch.setattr(
        funding_navigation_worker.funding_catalog,
        "restore_persisted_cache",
        lambda: {"ready": True},
    )
    pages = {
        (
            str((query.get("kind") or [""])[0]),
            str((query.get("funding_window") or ["now"])[0]),
        ): {"groups": [], "rows": []}
        for query in funding_navigation.QUERIES
    }
    monkeypatch.setattr(
        funding_navigation_worker.funding_catalog,
        "build_navigation_pages",
        lambda **_kwargs: pages,
    )
    monkeypatch.setattr(
        funding_navigation_worker.server,
        "_funding_catalog_seed_payload",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        funding_navigation_worker.server,
        "_merge_complete_funding_page",
        lambda shell, *_args, **_kwargs: dict(shell),
    )

    class Writer:
        def __init__(self, *_args, **_kwargs):
            self.views = []

        def write_route_index(self, _routes):
            return None

        def write_view(self, query, _payload):
            self.views.append(query)

        def publish(self):
            return {"generation": "g1", "views": self.views}

        def abort(self):
            return None

    monkeypatch.setattr(
        funding_navigation_worker.materialized_views,
        "GenerationWriter",
        Writer,
    )

    result = funding_navigation_worker.build(
        tmp_path / "board.jsonl", tmp_path / "navigation"
    )

    assert result["views"] == 12
    assert result["source_advanced_during_build"] is True
