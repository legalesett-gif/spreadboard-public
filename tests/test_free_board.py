"""The board without an account.

A visitor sees a real, live, deliberately small slice: complete routes with
venues, ticking off the same feed the full board runs on. What they must not
see is the rest of the board, and what they must not be able to do is widen
the free stream into it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from spreadboard import accounts, server


@pytest.fixture(autouse=True)
def _signed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    accounts.set_current_user(None)


def _stub_board(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr(server, "api_market_spreads", lambda path, query: payload)


def _group(token: str, *, edge: float, funding: float) -> dict:
    route = {
        "route_key": f"{token}|Binance Futures|Gate Futures",
        "long_venue": "Binance",
        "long_market_type": "Futures",
        "short_venue": "Gate",
        "short_market_type": "Futures",
    }
    return {
        "token": token,
        "token_name": f"{token} Token",
        "best_route": route,
        "best_funding_route": route,
        "best_edge_pct": edge,
        "best_funding_24h_pct": funding,
    }


PAYLOAD = {
    "ok": True,
    "summary": {"matching_tokens": 1089, "matching_rows": 15609},
    "source_health": {"canonical_api": {"status": "fresh", "age_min": 0.4}},
    "exchange_options": [f"venue{index}" for index in range(21)],
    "top_edges": [_group(f"EDGE{i}", edge=20.0 - i, funding=0.1) for i in range(8)],
    "top_funding": [_group(f"CARRY{i}", edge=1.0, funding=0.9 - i / 10) for i in range(8)],
}


def test_the_free_page_shows_complete_routes_with_their_venues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A number with the venues stripped out is an unverifiable claim."""
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert "EDGE0" in html
    assert "Binance Futures" in html
    assert "Gate Futures" in html


def test_the_free_page_is_capped_well_below_the_full_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    shown = [f"EDGE{index}" for index in range(8) if f">EDGE{index}<" in html]
    assert len(shown) == server.FREE_TOKEN_LIMIT
    assert "EDGE0" in shown and f"EDGE{server.FREE_TOKEN_LIMIT}" not in shown


def test_the_free_page_carries_the_live_hooks_it_needs_to_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendered numbers are patched by route key; without both it never moves."""
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    rows = server.FREE_TOKEN_LIMIT * 2
    assert html.count('<article class="free-row" data-route-key=') == rows
    assert html.count("<strong data-live-spread>") == rows
    assert html.count("<strong data-live-funding>") == rows
    assert "/api/stream/free" in html


def test_the_free_stream_endpoint_is_the_one_the_page_subscribes_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert 'EventSource("/api/stream/free")' in html
    assert "/api/stream/board" not in html


def test_the_free_query_is_pinned_and_ignores_whatever_the_visitor_sends() -> None:
    """`/free?limit=100000` must not hand over the whole board."""
    assert server.FREE_BOARD_QUERY == {}
    script = server.render_board_stream_script(
        dict(server.FREE_BOARD_QUERY), endpoint="/api/stream/free"
    )
    # No query string at all: nothing from the request reaches the subscription.
    assert 'EventSource("/api/stream/free")' in script


def test_the_free_page_and_its_stream_are_reachable_without_an_account() -> None:
    source = server.SpreadBoardHandler._authorize.__doc__ or ""
    del source
    import inspect

    gate = inspect.getsource(server.SpreadBoardHandler._authorize)
    assert '"/free"' in gate
    assert '"/api/stream/free"' in gate


def test_a_signed_out_visitor_lands_on_the_board_not_a_login_form() -> None:
    import inspect

    gate = inspect.getsource(server.SpreadBoardHandler._authorize)
    assert 'self._redirect("/free")' in gate


def test_the_visitor_nav_only_offers_pages_a_visitor_can_open() -> None:
    """Every member link bounced to /login: seven dead ends, no way to the board."""
    nav = server.render_primary_nav("free", signed_in=False)

    public = {
        "/login",
        "/register",
        "/pricing",
        "/guide",
        "/terms",
        "/privacy",
        "/refunds",
        "/free",
    }
    hrefs = {
        part.split('"', 1)[0]
        for part in nav.split('href="')[1:]
    }
    assert hrefs
    assert hrefs <= public
    assert "/free" in hrefs


def test_the_member_nav_is_unchanged() -> None:
    nav = server.render_primary_nav("markets", signed_in=True)

    for href in ("/funding", "/charts", "/intel", "/watchlist", "/account", "/pricing"):
        assert f'href="{href}"' in nav
    assert urlparse("/").path == "/"
    assert 'href="/"' in nav


def test_warming_yields_between_builds_so_the_server_can_answer() -> None:
    """The warm holds the GIL; without a yield the site is unreachable, not slow."""
    import inspect

    from scripts import run_spreadboard_service as service

    warm = inspect.getsource(service._warm_board_cache)
    assert "_yield_to_requests()" in warm
    assert service.WARM_YIELD_SECONDS > 0


def test_the_readiness_probes_own_query_is_warmed() -> None:
    """/api/health builds the board at limit=0 -- a key nothing else warms."""
    import inspect

    from scripts import run_spreadboard_service as service

    warm = inspect.getsource(service._warm_board_cache)
    assert "api_source_health" in warm


def _production_setting(name: str) -> str:
    import re

    text = Path("compose.production.yml").read_text(encoding="utf-8")
    match = re.search(rf'^\s+{name}:\s*"?([^"\n]*?)"?\s*$', text, re.MULTILINE)
    assert match, f"{name} is not set in compose.production.yml"
    return match.group(1).strip()


def test_the_warm_set_fits_the_board_cache_without_evicting_itself() -> None:
    """Measured, not assumed: the thirteen warmed views cost ~471MB in total.

    Most lane queries add almost nothing on top of the first build because they
    share the parsed snapshot; only the first (+210MB) and the charts view at
    limit=500 (+83MB) are substantial. So the bound exists to stop ad-hoc
    traffic evicting warm entries, and to keep an unbounded tail from growing --
    not because each entry is individually huge.
    """
    from scripts.run_spreadboard_service import WARM_QUERIES

    entries = int(_production_setting("SPREADBOARD_MARKET_CACHE_ENTRIES"))

    # The warm pass must not evict its own earlier entries.
    assert entries > len(WARM_QUERIES)
    # ...and the tail stays bounded. The bound is applied twice, to
    # _MARKET_CACHE and to the stale-while-revalidate copy behind it.
    assert entries <= 24


def test_the_alert_worker_builds_nothing_when_nobody_has_a_rule() -> None:
    """It runs every ten seconds and limit=None is the largest payload there is."""
    import inspect

    from spreadboard import alerts

    source = inspect.getsource(alerts.UserMarketAlertWorker.check_once)
    before_build = source.split("load_spreads", 1)[0]
    assert "list_market_alert_user_ids" in before_build
    assert "if not user_ids:" in before_build


def test_freed_memory_is_returned_to_the_kernel_after_each_warm() -> None:
    """gc.collect() frees the objects; glibc keeps the arenas unless asked."""
    import inspect

    from scripts import run_spreadboard_service as service

    assert "malloc_trim" in inspect.getsource(service._return_freed_memory)
    assert "_return_freed_memory()" in inspect.getsource(service._warm_board_cache)
    # Must not raise anywhere -- it is a no-op off glibc.
    service._return_freed_memory()


def test_the_snapshot_pipeline_runs_outside_the_web_server() -> None:
    """Parsing a 40MB snapshot costs ~1GB, and it was done up to three times.

    In the server process that reached 4.31GB five minutes after every start,
    inside a 6GB cgroup -- so the kernel killed it and lost the scan with it.
    """
    import inspect

    from scripts.run_spreadboard_service import RefreshLoop

    source = inspect.getsource(RefreshLoop.refresh_once)
    assert "_finalize_snapshot" in source
    # No snapshot may be parsed in the server process any more.
    assert "json.loads(REFRESH_SNAPSHOT_PATH" not in source
    assert "json.loads(SNAPSHOT_PATH" not in source
    assert Path("scripts/snapshot_finalize_worker.py").exists()


def test_the_publish_stage_still_runs_under_both_locks() -> None:
    """The merge and the write must stay mutually exclusive with quote cycles."""
    import inspect

    from scripts.run_spreadboard_service import RefreshLoop

    source = inspect.getsource(RefreshLoop.refresh_once)
    guarded = source.split("with self.quote_cycle_lock, self.snapshot_lock:", 1)
    assert len(guarded) == 2, "publish is not inside the lock block"
    assert '_finalize_snapshot("publish")' in guarded[1].split("\n\n", 1)[0]


def test_nothing_that_loads_a_venue_or_a_snapshot_runs_in_the_server() -> None:
    """Everything heavy is a process that exits, so its memory comes back.

    The server reached 2.2GB within a minute of starting and 4.3GB by five,
    inside a 6GB cgroup. What it holds now is its caches.
    """
    import inspect

    from scripts.run_spreadboard_service import RefreshLoop, BulkQuoteLoop

    catalog = inspect.getsource(RefreshLoop.run_chart_catalog)
    assert "chart_catalog.refresh" not in catalog
    assert "artifact_worker" in catalog

    identity = inspect.getsource(RefreshLoop._refresh_verified_identity_registry)
    assert "build_verified_identity_registry" not in identity
    assert "artifact_worker" in identity

    assert "bulk_quote_worker" in inspect.getsource(BulkQuoteLoop)
    for name in ("artifact_worker", "bulk_quote_worker", "snapshot_finalize_worker"):
        assert Path(f"scripts/{name}.py").exists()


def test_the_fast_quote_cycle_does_not_parse_the_snapshot_in_the_server() -> None:
    """Once a minute, ~1GB of parsed JSON. This was the actual cause.

    The service reached 1.8GB after one minute and 4.3GB by five, then the
    kernel killed the container -- losing the discovery scan every time.
    """
    import inspect

    from scripts.run_spreadboard_service import RefreshLoop

    source = inspect.getsource(RefreshLoop.run_fast_quotes)
    assert "json.loads(SNAPSHOT_PATH" not in source
    assert "market_history.record_snapshot" not in source
    assert '_finalize_snapshot("record")' in source


def test_no_worker_output_is_buffered_in_the_server() -> None:
    """capture_output=True holds everything a child says until it exits.

    The fast-quote worker hits its 240s deadline writing venue errors the whole
    time; the parent went 0.51GB -> 4.50GB inside one call, once a minute.
    """
    source = Path("scripts/run_spreadboard_service.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    # Only the docstring that explains the hazard may mention it.
    assert code.count("capture_output=True") <= 1
    assert "WORKER_OUTPUT_TAIL_BYTES" in code
