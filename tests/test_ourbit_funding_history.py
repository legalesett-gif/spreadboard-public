"""Ourbit settles funding but has no CCXT adapter.

Every Ourbit leg was classified ``unsupported_venue``, so any board route with
an Ourbit leg showed no funding at all -- 736 legs, and the whole of the
board's missing-funding population in the sample that prompted this.
"""

from __future__ import annotations

import json
import time

from spreadboard import venue_funding_history as vfh

HOUR_MS = 3_600_000


def _page(rows, *, total_page: int = 1):
    return {
        "success": True,
        "code": 0,
        "data": {"pageSize": 100, "totalPage": total_page, "resultList": rows},
    }


def _stub(monkeypatch, pages):
    """Serve ``pages`` in order, recording the URLs asked for."""

    seen: list[str] = []

    def fake_urlopen(request, *_a, **_k):
        seen.append(getattr(request, "full_url", str(request)))
        payload = pages[min(len(seen) - 1, len(pages) - 1)]

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_settled_rows_become_entries(monkeypatch) -> None:
    now = int(time.time() * 1000)
    rows = [
        {"symbol": "ONT_USDT", "fundingRate": 0.0004, "settleTime": now - HOUR_MS},
        {"symbol": "ONT_USDT", "fundingRate": -0.0012, "settleTime": now - 9 * HOUR_MS},
    ]
    _stub(monkeypatch, [_page(rows)])

    outcome = vfh._native_leg_history_outcome("Ourbit", "ONT/USDT:USDT")

    assert outcome["status"] == "ok"
    assert [e["fundingRate"] for e in outcome["entries"]] == [0.0004, -0.0012]
    assert outcome["entries"][0]["timestamp"] == now - HOUR_MS


def test_a_success_code_of_zero_is_not_read_as_failure(monkeypatch) -> None:
    """``code`` is 0 on success.

    Guarding with ``payload.get("code") or -1`` turns every good response into
    an error, which is exactly how this first shipped.
    """

    now = int(time.time() * 1000)
    _stub(
        monkeypatch,
        [_page([{"symbol": "X_USDT", "fundingRate": 0.0001, "settleTime": now - HOUR_MS}])],
    )

    outcome = vfh._native_leg_history_outcome("Ourbit", "X/USDT:USDT")

    assert outcome["status"] == "ok"
    assert len(outcome["entries"]) == 1


def test_the_symbol_is_sent_in_ourbit_underscore_form(monkeypatch) -> None:
    now = int(time.time() * 1000)
    seen = _stub(
        monkeypatch,
        [_page([{"symbol": "X_USDT", "fundingRate": 0.0, "settleTime": now - HOUR_MS}])],
    )

    vfh._native_leg_history_outcome("Ourbit", "XCN/USDT:USDT")

    assert "symbol=XCN_USDT" in seen[0]
    assert "futures.ourbit.com" in seen[0]


def test_paging_stops_once_the_window_is_covered(monkeypatch) -> None:
    """An hourly market needs several pages; an 8-hour one needs a single page.

    Paging to the budget regardless would multiply every refresh's request
    count by ten for no extra window coverage.
    """

    now = int(time.time() * 1000)
    old = [
        {"symbol": "B_USDT", "fundingRate": 0.0001, "settleTime": now - (40 * 24 * HOUR_MS)}
    ]
    seen = _stub(monkeypatch, [_page(old, total_page=99)])

    outcome = vfh._native_leg_history_outcome("Ourbit", "B/USDT:USDT", days=30, max_pages=10)

    assert outcome["status"] == "ok"
    assert len(seen) == 1, f"paged {len(seen)} times past the 30d window"


def test_pages_until_the_window_is_reached(monkeypatch) -> None:
    now = int(time.time() * 1000)
    recent = [{"symbol": "B_USDT", "fundingRate": 0.0001, "settleTime": now - HOUR_MS}]
    older = [{"symbol": "B_USDT", "fundingRate": 0.0002, "settleTime": now - 40 * 24 * HOUR_MS}]
    seen = _stub(monkeypatch, [_page(recent, total_page=99), _page(older, total_page=99)])

    outcome = vfh._native_leg_history_outcome("Ourbit", "B/USDT:USDT", days=30, max_pages=10)

    assert len(seen) == 2
    assert len(outcome["entries"]) == 2


def test_a_provider_error_is_retryable_not_empty_history(monkeypatch) -> None:
    """``no_history_rows`` would mean "this market pays nothing", which is a
    different and much worse claim than "we could not read it"."""

    _stub(monkeypatch, [{"success": False, "code": 500, "message": "nope"}])

    outcome = vfh._native_leg_history_outcome("Ourbit", "X/USDT:USDT")

    assert outcome["status"] == "api_error"
    assert outcome["error_type"] == "OurbitResponseError"


def test_ourbit_is_registered_so_legs_are_no_longer_unsupported() -> None:
    assert "Ourbit" in vfh.NATIVE_HISTORY
    assert vfh._native_leg_history_outcome("Binance", "BTC/USDT:USDT")["status"] == (
        "unsupported_venue"
    )
