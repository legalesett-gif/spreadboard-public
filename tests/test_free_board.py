"""The board without an account.

A visitor sees a real, live, deliberately small slice: complete routes with
venues, ticking off the same feed the full board runs on. What they must not
see is the rest of the board, and what they must not be able to do is widen
the free stream into it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
        "age_min": 0.1,
        "executable_spread_pct": edge,
        "depth_weighted_spread_pct": edge,
        "depth_usd": 500.0,
        "target_notional_usd": 500.0,
        "funding_projected_24h_pct": funding,
    }
    return {
        "token": token,
        "token_name": f"{token} Token",
        "best_route": route,
        "best_funding_route": route,
        "best_edge_pct": edge,
        "best_funding_24h_pct": funding,
        "best_funding_24h_basis": "projected_current_rate",
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
    """The visible rows use the member renderer, so they tick like the paid ones."""
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert html.count("data-live-spread") >= server.FREE_TOKEN_LIMIT
    assert html.count("data-live-funding") >= server.FREE_TOKEN_LIMIT
    assert "/api/stream/free" in html


def test_a_cold_free_shell_recovers_in_place_and_reconnects_its_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warming visit used to remain an empty 0-token page forever.

    The price stream cannot create missing route markup, and the public stream's
    allow-list is frozen when the connection opens.  The cold shell therefore
    needs the same in-place structural recovery as Markets, followed by a new
    EventSource connection for the recovered visible route set.
    """
    _stub_board(
        monkeypatch,
        {
            "ok": False,
            "status": "warming",
            "summary": {},
            "source_health": {"canonical_api": {"status": "warming"}},
            "top_edges": [],
            "top_funding": [],
        },
    )

    html = server.render_free_page(Path("board.json"))
    refresh = server.render_auto_refresh_script()
    stream = server.render_board_stream_script({}, endpoint="/api/stream/free")

    assert 'class="free-page" data-refresh="5" data-refresh-silent="1"' in html
    assert 'document.dispatchEvent(new CustomEvent("spreadboard:structure-refreshed"))' in refresh
    assert 'document.addEventListener("spreadboard:structure-refreshed", connect)' in stream
    assert "source.close()" in stream


def test_a_live_free_shell_uses_the_slow_structural_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert 'class="free-page" data-refresh="300" data-refresh-silent="1"' in html


def test_the_visible_rows_are_the_same_component_a_member_sees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a cut-down copy: a visitor should see exactly what they are buying."""
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert 'class="token-route-group"' in html
    assert "Best pair" in html
    assert f"{server.PROBE_LABEL} VWAP" in html
    assert "Best-route funding" in html


def test_only_the_top_tokens_are_shown_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert server.FREE_TOKEN_LIMIT == 2
    assert html.count('class="token-route-group"') == server.FREE_TOKEN_LIMIT * 2


def test_a_teaser_row_shows_the_numbers_and_hides_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What converts is a judgeable opportunity you cannot act on.

    The spread, funding, lane and route count are all real. What is withheld is
    what you would need to place the trade: which asset, and where to buy it.
    """
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))
    teasers = html.split('class="teaser-list"')[1].split("</section>")[0]

    # Numbers are present and real.
    assert "data-live-spread" in teasers
    assert "data-live-funding" in teasers
    assert "%" in teasers

    # The token and the buy leg never reach the markup.
    for index in range(server.FREE_TOKEN_LIMIT, 8):
        assert f"EDGE{index}" not in teasers
    assert "Binance" not in teasers, "the buy venue leaked"
    # ...while the sell leg is shown, so the shape of the route is legible.
    assert "Gate" in teasers


def test_a_funding_teaser_shows_the_ranked_value_and_its_evidence_basis() -> None:
    """The public funding lane must not rank on one number and print another."""
    group = _group("CARRY", edge=1.0, funding=0.8)
    route = group["best_funding_route"]
    route["funding_24h_pct"] = 0.1
    route["funding_projected_24h_pct"] = 0.8
    group["best_funding_24h_pct"] = 0.8
    group["best_funding_24h_basis"] = "projected_current_rate"

    html = server.render_teaser_row(group, metric="funding")

    assert "+0.800%" in html
    assert "24h at current rate" in html
    assert "+0.100%" not in html


def test_a_funding_teaser_uses_the_funding_routes_own_spread() -> None:
    group = _group("CARRY", edge=8.0, funding=0.8)
    funding_route = dict(group["best_funding_route"])
    funding_route.update(
        {
            "route_key": "CARRY|Mexc|Spot|Bybit|Futures",
            "depth_weighted_spread_pct": 0.35,
            "executable_spread_pct": 0.4,
        }
    )
    group["best_funding_route"] = funding_route
    group["best_funding_24h_pct"] = 0.8
    group["best_funding_24h_basis"] = "projected_current_rate"

    html = server.render_teaser_row(group, metric="funding")

    assert ">0.3%</strong>" in html
    assert ">8.0%</strong>" not in html


def test_a_teaser_does_not_label_legacy_depth_as_current_matched_spread() -> None:
    group = _group("LEGACY", edge=3.0, funding=0.2)
    route = group["best_route"]
    route.pop("target_notional_usd", None)
    route["depth_usd"] = 50.0
    route["executable_spread_pct"] = 0.5

    html = server.render_teaser_row(group, metric="spread")

    assert "Spread evidence" in html
    assert ">0.5%</strong>" in html
    assert "target depth unavailable" in html
    assert ">3.0%</strong>" not in html


def test_a_full_group_has_distinct_live_keys_for_spread_and_funding() -> None:
    group = _group("CARRY", edge=1.0, funding=0.8)
    funding_route = dict(group["best_funding_route"])
    funding_route.update(
        {
            "route_key": "CARRY|Mexc|Spot|Bybit|Futures",
            "long_venue": "Mexc",
            "long_market_type": "Spot",
            "short_venue": "Bybit",
            "short_market_type": "Futures",
            "funding_projected_24h_pct": 0.8,
        }
    )
    group["best_funding_route"] = funding_route
    group["best_funding_24h_pct"] = 0.8
    group["best_funding_24h_basis"] = "projected_current_rate"

    html = server.render_market_token_group(group)

    assert 'data-route-key="CARRY|Binance Futures|Gate Futures"' in html
    assert 'data-funding-route-key="CARRY|Mexc|Spot|Bybit|Futures"' in html
    assert "Mexc → Bybit" in html


def test_now_funding_is_not_overwritten_by_an_older_settled_window() -> None:
    group = _group("CARRY", edge=1.0, funding=0.8)
    route = group["best_funding_route"]
    route["funding_24h_pct"] = 0.8
    route["funding_24h_source"] = "settled_public_events"
    route["funding_projected_24h_pct"] = 0.2
    route["funding_daily_pct"] = 0.2
    route["funding_rank_basis"] = "projected_current_rate"
    group["best_funding_24h_pct"] = 0.2
    group["best_funding_24h_basis"] = "projected_current_rate"

    group_html = server.render_market_token_group(group)
    route_html = server.render_market_group_route(route)

    assert '<strong data-live-funding>+0.200%</strong>' in group_html
    assert '<strong data-live-funding>+0.200%</strong>' in route_html
    assert "+0.800%" not in group_html
    assert "+0.800%" not in route_html


def test_the_free_stream_allows_both_headline_routes_only_for_full_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = []
    for index in range(3):
        group = _group(f"T{index}", edge=3.0 - index, funding=0.8 - index / 10)
        funding_route = dict(group["best_funding_route"])
        funding_route["route_key"] = f"T{index}|Mexc|Spot|Bybit|Futures"
        group["best_funding_route"] = funding_route
        groups.append(group)
    _stub_board(
        monkeypatch,
        {
            **PAYLOAD,
            "top_edges": groups,
            "top_funding": [],
        },
    )

    mapping = server.free_stream_key_map(Path("board.json"))

    for index in range(server.FREE_TOKEN_LIMIT):
        assert mapping[f"T{index}|Binance Futures|Gate Futures"] == (
            f"T{index}|Binance Futures|Gate Futures"
        )
        assert mapping[f"T{index}|Mexc|Spot|Bybit|Futures"] == (
            f"T{index}|Mexc|Spot|Bybit|Futures"
        )
    assert "T2|Binance Futures|Gate Futures" in mapping
    assert "T2|Mexc|Spot|Bybit|Futures" not in mapping


def test_free_copy_does_not_claim_hidden_depth_future_settlements_or_fixed_dex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert "depth are all open" not in html
    assert "What the position pays over 24h at the settled rate" not in html
    assert "venues and OKX DEX" not in html


def test_the_locked_rows_open_the_membership_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert "data-locked" in html
    assert 'id="unlockDialog"' in html
    assert "showModal" in html
    # Keyboard users get there too.
    assert 'tabindex="0"' in html


def test_the_teaser_says_how_much_is_withheld_without_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spread without its token is unactionable, and it is what sells."""
    _stub_board(monkeypatch, PAYLOAD)

    html = server.render_free_page(Path("board.json"))

    assert "Widest spread held back" in html
    assert "Best funding held back" in html
    # The widest withheld edge belongs to EDGE2, whose name must not appear.
    assert "EDGE2" not in html


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
        "/telegram",
        "/methodology",
        "/guide",
        "/terms",
        "/privacy",
        "/refunds",
        "/free",
        "/status",
    }
    hrefs = {
        part.split('"', 1)[0]
        for part in nav.split('href="')[1:]
    }
    assert hrefs
    assert hrefs <= public
    assert "/free" in hrefs


def test_the_member_nav_still_carries_every_working_surface() -> None:
    """Adding the free board must not disturb the member navigation.

    /intel was deliberately withdrawn once it was found to be an empty shell --
    see test_navigation_surface -- so it is no longer expected here.
    """
    nav = server.render_primary_nav("markets", signed_in=True)

    for href in ("/funding", "/charts", "/watchlist", "/account", "/pricing"):
        assert f'href="{href}"' in nav
    assert 'href="/intel"' not in nav, "an empty surface must not return to the nav"
    assert urlparse("/").path == "/"
    assert 'href="/"' in nav


def test_status_page_compacts_market_timestamp_for_small_cards() -> None:
    page = server.render_status_page(
        {
            "ok": False,
            "checked_at": "2026-08-09T21:59:40.556000+00:00",
            "components": {
                "market_data": {
                    "status": "operational",
                    "row_count": 25967,
                    "updated_at": "2026-08-09T21:56:37Z",
                }
            },
        }
    )

    assert "09 Aug 2026 · 21:56 UTC" in page
    assert "2026-08-09T21:56:37Z" not in page


def test_status_page_does_not_call_an_unbuilt_market_cache_zero_rows() -> None:
    page = server.render_status_page(
        {
            "ok": True,
            "checked_at": "2026-08-14T17:30:00+00:00",
            "components": {
                "market_data": {
                    "status": "operational",
                    "row_count": None,
                    "updated_at": "2026-08-14T17:29:30Z",
                }
            },
        }
    )

    assert "Live feed · updated 14 Aug 2026 · 17:29 UTC" in page
    assert "0 current rows" not in page


def test_status_page_handler_includes_subscription_lifecycle_health() -> None:
    import inspect

    source = inspect.getsource(server.SpreadBoardHandler.do_GET)
    status_branch = source.split('parsed.path == "/status"', 1)[1].split(
        'parsed.path == "/pricing"', 1
    )[0]

    assert "self.server.position_alert_worker" in status_branch
    assert "self.server.subscription_lifecycle_worker" in status_branch


def test_status_discloses_partial_dex_and_every_running_customer_service(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "api_source_health",
        lambda *_args, **_kwargs: {
            "ok": True,
            "canonical_api": {
                "status": "fresh",
                "updated_at": "2026-08-21T13:00:00Z",
                "age_min": 0.5,
                "row_count": 23_696,
                "dex_spot_source": {
                    "status": "partial",
                    "rows": 0,
                    "details": {"provider": "OKX DEX"},
                    "blockers": ["partial_source_errors"],
                    "errors": ["catalogue:1:API key or regions have no access"],
                },
            },
        },
    )
    monkeypatch.setattr(
        server.crypto_billing,
        "status",
        lambda: {"checkout_ready": True, "chain": "Arbitrum One", "tokens": ["USDT"]},
    )
    monkeypatch.setattr(
        server.telegram_bot,
        "status",
        lambda: {"configured": True, "community_configured": True},
    )
    monkeypatch.setattr(server.mailer, "status", lambda: {"configured": True})
    monkeypatch.setattr(
        server,
        "funding_history_health",
        lambda: {"status": "operational", "coverage_pct": 100.0},
    )
    monkeypatch.setattr(
        server,
        "accounting_worker_status",
        lambda: {"configured": True, "running": True, "read_only": True},
    )

    payload = server.api_public_status(
        Path("board.json"),
        {},
        SimpleNamespace(running=True, poll_seconds=30),
        SimpleNamespace(running=True, poll_seconds=900),
    )

    assert payload["ok"] is False
    assert payload["overall_status"] == "degraded"
    assert payload["components"]["market_data"]["status"] == "operational"
    assert payload["components"]["dex_quotes"]["status"] == "degraded"
    assert payload["components"]["subscription_access"]["status"] == "operational"
    assert payload["components"]["funding_history"]["status"] == "operational"
    assert payload["components"]["private_accounting"]["status"] == "operational"

    page = server.render_status_page(payload)
    assert "Core services live · data source degraded" in page
    assert "DEX quotes" in page
    assert "OKX DEX provider access is blocked; CEX market data remains live" in page
    assert "Subscription access" in page
    assert "Funding history" in page
    assert "Read-only accounting" in page
    assert 'href="/api/status"' in page


def test_status_does_not_mislabel_a_transient_dex_partial_as_access_blocked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "api_source_health",
        lambda *_args, **_kwargs: {
            "ok": True,
            "canonical_api": {
                "dex_spot_source": {
                    "status": "partial",
                    "details": {"provider": "Example DEX"},
                    "errors": ["quote timeout"],
                }
            },
        },
    )
    monkeypatch.setattr(server.crypto_billing, "status", lambda: {"checkout_ready": True})
    monkeypatch.setattr(
        server.telegram_bot,
        "status",
        lambda: {"configured": True, "community_configured": False},
    )
    monkeypatch.setattr(server.mailer, "status", lambda: {"configured": True})
    monkeypatch.setattr(server, "funding_history_health", lambda: {"status": "operational"})
    monkeypatch.setattr(
        server,
        "accounting_worker_status",
        lambda: {"configured": True, "running": True, "read_only": True},
    )

    payload = server.api_public_status(
        Path("board.json"),
        {},
        SimpleNamespace(running=True),
        SimpleNamespace(running=True),
    )

    assert payload["components"]["dex_quotes"]["detail"] == (
        "Example DEX quote source is degraded; CEX market data remains live"
    )
    assert payload["components"]["telegram"]["status"] == "setup_needed"


def test_status_headline_names_a_non_data_degradation_honestly() -> None:
    page = server.render_status_page(
        {
            "checked_at": "2026-08-21T13:00:00+00:00",
            "overall_status": "degraded",
            "components": {
                "website": {"status": "operational"},
                "background_alerts": {"status": "degraded"},
            },
        }
    )

    assert "Core services live · a monitored service is degraded" in page
    assert "Core services live · data source degraded" not in page


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
    # the one bounded current-generation _MARKET_CACHE.
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


def test_telegram_snapshot_can_warm_before_the_first_quote_cycle(tmp_path, monkeypatch) -> None:
    from scripts import run_spreadboard_service as service
    from spreadboard import server, telegram_queries

    seen = []
    payload = {"groups": []}
    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda path, query: seen.append((Path(path), query)) or payload,
    )
    monkeypatch.setattr(telegram_queries, "replace_payload", lambda value: value)
    installed_funding = []
    monkeypatch.setattr(
        telegram_queries,
        "replace_funding_payloads",
        lambda values: installed_funding.extend(values) or {"groups": []},
    )

    board_path = tmp_path / "existing-board.json"
    service._warm_telegram_payload_at_startup(board_path)

    funding_queries = [query for query in service.WARM_QUERIES if query.get("funding_only")]
    assert seen == [
        (board_path, {}),
        (board_path, {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]}),
        *((board_path, query) for query in funding_queries),
    ]
    assert installed_funding == [payload] * len(funding_queries)


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

    from scripts.run_spreadboard_service import BulkFundingLoop, BulkQuoteLoop, RefreshLoop

    catalog = inspect.getsource(RefreshLoop.run_chart_catalog)
    assert "chart_catalog.refresh" not in catalog
    assert "artifact_worker" in catalog

    identity = inspect.getsource(RefreshLoop._refresh_verified_identity_registry)
    assert "build_verified_identity_registry" not in identity
    assert "artifact_worker" in identity

    assert "bulk_quote_worker" in inspect.getsource(BulkQuoteLoop)
    assert "bulk_quote_worker" in inspect.getsource(BulkFundingLoop)
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
    assert '_finalize_snapshot("record")' not in source


def test_continuous_price_collectors_yield_cpu_to_member_requests() -> None:
    """Live data stays current without letting collectors starve HTTP."""
    import inspect

    from scripts.run_spreadboard_service import BulkQuoteLoop, RefreshLoop

    assert "*_live_worker_prefix()" in inspect.getsource(RefreshLoop._ensure_websocket_worker)
    assert "*_live_worker_prefix()" in inspect.getsource(BulkQuoteLoop._sweep_once)


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


def test_a_dex_leg_is_labelled_dex_not_spot() -> None:
    """An on-chain swap carries market_type "Spot", so a DEX-Futures route
    rendered as "OKX DEX 56 Spot -> Gate Futures" -- which reads exactly like a
    Spot-Futures route, and is why the DEX farms looked mixed in with the
    Futures-Spot ones."""
    from spreadboard import server

    assert server.leg_market_label("OKX DEX 56", "Spot") == "DEX"
    assert server.leg_market_label("OKX DEX 1", "Spot") == "DEX"
    # Everything else is untouched, including the perp DEXes, which are perps.
    assert server.leg_market_label("Gate", "Futures") == "Futures"
    assert server.leg_market_label("Binance", "Spot") == "Spot"
    assert server.leg_market_label("Hyperliquid", "Futures") == "Futures"
    assert server.leg_market_label("Aster", "Futures") == "Futures"


def test_the_exchange_link_shows_dex_not_spot() -> None:
    """The funding rows render their legs through this helper."""
    from spreadboard import server

    row = {
        "long_venue": "OKX DEX 56", "long_market_type": "Spot",
        "short_venue": "Gate", "short_market_type": "Futures",
    }
    long_html = server.render_exchange_link(row, "long", include_market_type=True)
    short_html = server.render_exchange_link(row, "short", include_market_type=True)

    assert "DEX" in long_html and "Spot" not in long_html
    assert "Futures" in short_html


def test_no_leg_renders_its_raw_market_type() -> None:
    """Every render site must go through the label, or one page disagrees."""
    import re

    source = Path("spreadboard/server.py").read_text(encoding="utf-8")
    raw = re.findall(r"h\((?:row|route)\.get\(['\"](?:long|short)_market_type['\"]\)\)", source)
    assert not raw, f"{len(raw)} render sites still print the raw market type"


def test_the_free_stream_sends_only_the_visible_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning the query was not enough on its own.

    The board stream pushes a route key with every price change, so a visitor
    who opened it received live prices for all 1,097 tokens -- locked ones
    included -- which is the entire product.
    """
    _stub_board(monkeypatch, PAYLOAD)

    mapping = server.free_stream_key_map(Path("board.json"))

    shown = server.FREE_TOKEN_LIMIT + server.FREE_TEASER_ROWS
    assert len(mapping) <= shown * 2
    # The two full rows keep their own key; teasers go out under a hash that
    # names neither the asset nor its venues.
    for index in range(server.FREE_TOKEN_LIMIT):
        real = f"EDGE{index}|Binance Futures|Gate Futures"
        assert mapping[real] == real
    for index in range(server.FREE_TOKEN_LIMIT, 6):
        real = f"EDGE{index}|Binance Futures|Gate Futures"
        emitted = mapping[real]
        assert emitted != real
        assert "EDGE" not in emitted and "Binance" not in emitted and "Gate" not in emitted


def test_the_stream_handler_applies_that_filter() -> None:
    import inspect

    source = inspect.getsource(server.SpreadBoardHandler.do_GET)
    block = source.split('"/api/stream/free"', 1)[1][:900]
    assert "free_stream_key_map" in block

    stream = inspect.getsource(server.SpreadBoardHandler._send_board_stream)
    assert "rename" in stream and "key in rename" in stream
