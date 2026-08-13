"""Fast public order-book refreshes for routes already leading the board."""

from __future__ import annotations

import gc
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spreadarb.api_discovery.models import spread_pct
from spreadarb.api_discovery.orderbook import depth_weighted_price
from spreadboard import live_book_cache, public_rails, token_metadata, tokenized_assets

ROOT = Path(__file__).resolve().parents[1]
LEVERAGED_TOKEN_PATTERN = re.compile(r"^[A-Z0-9]+[2-5][LS]$")

VENUE_IDS = {
    "Aster": "aster",
    "Binance": "binance",
    "Bingx": "bingx",
    "Bitget": "bitget",
    "BitMart": "bitmart",
    "Bybit": "bybit",
    "Coinbase": "coinbaseexchange",
    "Coinbase International": "coinbaseinternational",
    "CoinEx": "coinex",
    "Gate": "gateio",
    "HTX": "htx",
    "Hyperliquid": "hyperliquid",
    "Kraken": "kraken",
    "Kraken Futures": "krakenfutures",
    "Kucoin": "kucoin",
    "Kucoin Futures": "kucoinfutures",
    "Mexc": "mexc",
    "OKX": "okx",
    "Phemex": "phemex",
    "WhiteBIT": "whitebit",
    "XT": "xt",
}

NATIVE_FUTURES_VENUES = {
    "Aster",
    "Binance",
    "Bingx",
    "Bitget",
    "BitMart",
    "Bybit",
    "CoinEx",
    "Coinbase International",
    "Gate",
    "HTX",
    "Hyperliquid",
    "Kraken Futures",
    "Kucoin Futures",
    "Mexc",
    "OKX",
    "Ourbit",
    "Phemex",
    "WhiteBIT",
    "XT",
}
NATIVE_SPOT_VENUES = {
    "Binance",
    "Bingx",
    "Bitget",
    "BitMart",
    "Bybit",
    "CoinEx",
    "Coinbase",
    "Gate",
    "HTX",
    "Kraken",
    "Kucoin",
    "Mexc",
    "OKX",
    "WhiteBIT",
    "XT",
}
FAST_QUOTE_LANES = (
    "FUTURES",
    "FUTURES-SPOT",
    "SPOT",
    "DEX-FUTURES",
    "DEX-SPOT",
)
FAST_QUOTE_LANE_WEIGHTS = {
    "FUTURES": 1,
    "FUTURES-SPOT": 1,
    # Public-book failure rates are measured at the release boundary. The
    # formerly doubled Spot quota left Futures with only 37 attempts while
    # Spot was succeeding on 72/73; equal CEX shares give every lane 50
    # attempts inside the same 220-route ceiling.
    "SPOT": 1,
    "DEX-FUTURES": 1,
    "DEX-SPOT": 1,
}

#: What a perpetual settles on when the venue does not say. Eight hours is
#: the market standard; the venues that differ (Hyperliquid, Kraken) publish
#: their interval, so this only ever fills a genuine gap.
DEFAULT_FUNDING_INTERVAL_HOURS = 8.0


#: Venues whose funding CCXT cannot fetch in bulk. Without these the legs on
#: these venues keep whatever the 20-40 minute discovery scan captured and are
#: never corrected: HTX's ZHIPU sat at -0.6677%/8h while the exchange had long
#: since moved to 0.0, which alone put ZHIPU on the board at 2.43%/day against
#: a real 0.41%. Eight of eighteen futures venues were in this state.
#:
#: `path` walks the response to the list of contracts; `rate` is a fraction
#: unless `divide_by` names a price field to normalise against (Kraken quotes
#: an absolute rate). Native ids are mapped back through ccxt's markets_by_id,
#: so the symbols always match the ones the board stores.
NATIVE_FUNDING_SOURCES: dict[str, dict[str, Any]] = {
    "HTX": {
        "url": "https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate",
        "path": ("data",),
        "symbol": "contract_code",
        "rate": "funding_rate",
        "next_ms": "next_funding_time",
    },
    "Mexc": {
        "url": "https://contract.mexc.com/api/v1/contract/funding_rate?page_num=1&page_size=1000",
        "path": ("data",),
        "symbol": "symbol",
        "rate": "fundingRate",
        "interval": "collectCycle",
        "next_ms": "nextSettleTime",
    },
    "Kucoin Futures": {
        "url": "https://api-futures.kucoin.com/api/v1/contracts/active",
        "path": ("data",),
        "symbol": "symbol",
        "rate": "fundingFeeRate",
        "interval": "fundingRateGranularity",
        # Kucoin reports granularity in milliseconds: 14400000 is four hours,
        # not 14.4 million of them.
        "interval_scale": 1.0 / 3_600_000.0,
    },
    "Phemex": {
        "url": "https://api.phemex.com/md/v3/ticker/24hr/all",
        "path": ("result",),
        "symbol": "symbol",
        "rate": "fundingRateRr",
    },
    "BitMart": {
        "url": "https://api-cloud-v2.bitmart.com/contract/public/details",
        "path": ("data", "symbols"),
        "symbol": "symbol",
        "rate": "funding_rate",
        "interval": "funding_interval_hours",
    },
    "XT": {
        "url": "https://fapi.xt.com/future/market/v1/public/cg/contracts",
        "path": (),
        "symbol": "symbol",
        "symbol_keys": ("symbol", "ticker_id"),
        "rate": "funding_rate",
        "next_ms": "next_funding_rate_timestamp",
    },
    "Ourbit": {
        # An MEXC white-label with no CCXT adapter, so there are no markets to
        # map ids through; its symbols are BASE_QUOTE and convert directly.
        "url": "https://futures.ourbit.com/api/v1/contract/funding_rate",
        "path": ("data",),
        "symbol": "symbol",
        "symbol_format": "underscore_swap",
        "rate": "fundingRate",
        "interval": "collectCycle",
        "next_ms": "nextSettleTime",
    },
    "Kraken Futures": {
        "url": "https://futures.kraken.com/derivatives/api/v4/tickers",
        "path": ("tickers",),
        "symbol": "symbol",
        "rate": "fundingRate",
        # Kraken publishes an absolute rate; the fraction is it over mark price.
        "divide_by": "markPrice",
        "interval_constant": 1.0,
    },
}


class FastQuoteRefresher:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], Any] = {}
        self._client_lock = Lock()
        self._client_request_locks: dict[tuple[str, str], Lock] = {}

    def refresh_all_funding(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Re-quote funding for EVERY futures leg in the snapshot, not just the top routes.

        Order books cost one request per symbol, so they have to be rationed to
        `route_limit` routes. Funding does not: one bulk call per venue returns
        every symbol at once. Rationing it anyway left most legs carrying rates
        from the 20-45 minute discovery scan -- MEXC SWARMS read 0.0352%/4h here
        while the exchange was live at 0.0506%/4h and the reference board showed
        0.05%. The parser was right; the number was simply old.
        """
        # Funding drifts over minutes, not seconds, and a bulk call costs a
        # load_markets() per venue. Running it on every 20s quote cycle put real
        # pressure on the same upstream metadata endpoints the websocket book
        # worker uses, so it gets its own slower cadence.
        interval = max(30.0, float(os.environ.get("SPREADBOARD_FUNDING_REFRESH_SECONDS", "120")))
        previous = payload.get("funding_refresh") or {}
        last = _optional_number(previous.get("epoch"))
        now = time.time()
        if last is not None and now - last < interval:
            return {**previous, "skipped": True}
        legs: dict[str, list[tuple[dict[str, Any], str]]] = {}
        for bucket in ("api_discovered_rows", "dex_discovered_rows"):
            for row in payload.get(bucket) or []:
                if not isinstance(row, dict):
                    continue
                for side in ("long", "short"):
                    if row.get(f"{side}_market_type") != "Futures":
                        continue
                    key = _route_leg_key(row, side)
                    if key is not None:
                        legs.setdefault(key[0], []).append((row, side))
        venues = updated = 0
        # The bulk call itself is one request, but ccxt needs load_markets() first
        # and this worker is a fresh process every cycle, so a venue costs ~15s on
        # the droplet. Rotate: each pass takes the next slice of venues and the
        # cursor persists in the snapshot, so every venue is covered within a few
        # minutes instead of one pass starving the rest. Busiest venues sort first
        # so the rotation order is stable.
        budget = time.monotonic() + max(
            10.0, float(os.environ.get("SPREADBOARD_FUNDING_REFRESH_BUDGET_SECONDS", "45"))
        )
        ordered = [venue for venue, _ in sorted(legs.items(), key=lambda item: -len(item[1]))]
        start = int(_optional_number(previous.get("cursor")) or 0) % max(1, len(ordered))
        rotation = ordered[start:] + ordered[:start]
        cursor = start
        for venue in rotation:
            if time.monotonic() > budget:
                break
            cursor = (ordered.index(venue) + 1) % max(1, len(ordered))
            entries = legs[venue]
            rates = self._bulk_funding_rates(venue)
            if not rates:
                continue
            venues += 1
            for row, side in entries:
                key = _route_leg_key(row, side)
                fields = rates.get(key[2]) if key else None
                if not fields:
                    continue
                notes = row.setdefault("notes", {})
                route_inputs = notes.setdefault("route_inputs", {})
                route_inputs[side] = {**(route_inputs.get(side) or {}), **fields}
                updated += 1
        summary = {
            "venues": venues,
            "legs": updated,
            "candidates": sum(map(len, legs.values())),
            "cursor": cursor,
            "venue_count": len(ordered),
            "epoch": now,
            "updated_at": _utc_now_iso(),
        }
        payload["funding_refresh"] = summary
        return summary

    def _native_bulk_funding_rates(self, venue: str) -> dict[str, dict[str, Any]]:
        """Funding for a venue CCXT cannot bulk-fetch, from its own public API.

        Keyed by the venue's unified CCXT symbol so the result drops straight
        into the same place the CCXT path fills.
        """
        spec = NATIVE_FUNDING_SOURCES.get(venue)
        if spec is None:
            return {}
        try:
            payload = _json_url(str(spec["url"]))
            for step in spec.get("path") or ():
                payload = payload[step]
            if not isinstance(payload, list):
                return {}
            client = None
            if spec.get("symbol_format") != "underscore_swap":
                client = self._client(venue, "Futures")
                if client is None:
                    return {}
            if client is not None and not getattr(client, "markets", None):
                with self._client_request_lock(venue, "Futures"):
                    client.load_markets()
        except Exception:  # noqa: BLE001 - one venue must not stop the cycle.
            return {}

        divide_by = spec.get("divide_by")
        rates: dict[str, dict[str, Any]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            if spec.get("symbol_format") == "underscore_swap":
                native = str(item.get(str(spec["symbol"])) or "")
                base, separator, quote = native.partition("_")
                if not separator or not base:
                    continue
                unified = f"{base}/{quote}:{quote}"
                rate = _optional_number(item.get(str(spec["rate"])))
                if rate is None:
                    continue
                interval = _optional_number(item.get(str(spec.get("interval") or "")))
                fields = _funding_fields(
                    rate,
                    interval_hours=interval or DEFAULT_FUNDING_INTERVAL_HOURS,
                    next_funding_ms=item.get(str(spec.get("next_ms") or "")),
                )
                if fields:
                    rates[unified] = fields
                continue

            market = None
            for name in spec.get("symbol_keys") or (str(spec["symbol"]),):
                native = item.get(name)
                found = client.markets_by_id.get(str(native)) if native else None
                if isinstance(found, list):
                    found = next(
                        (m for m in found if m.get("swap") and m.get("inverse") is not True),
                        None,
                    )
                if isinstance(found, dict) and found.get("swap") and found.get("symbol"):
                    market = found
                    break
            if market is None:
                continue
            rate = _optional_number(item.get(str(spec["rate"])))
            if rate is None:
                continue
            if divide_by:
                reference = _optional_number(item.get(str(divide_by)))
                if not reference:
                    continue
                rate = rate / reference
            interval = spec.get("interval_constant")
            if interval is None:
                interval = _optional_number(item.get(str(spec.get("interval") or "")))
                scale = spec.get("interval_scale")
                if interval is not None and scale:
                    interval = interval * float(scale)
            fields = _funding_fields(
                rate,
                interval_hours=interval if interval else DEFAULT_FUNDING_INTERVAL_HOURS,
                next_funding_ms=item.get(str(spec.get("next_ms") or "")),
            )
            if fields:
                rates[str(market["symbol"])] = fields
        return rates

    def _bulk_funding_rates(self, venue: str) -> dict[str, dict[str, Any]]:
        """One call per venue for every perpetual it lists."""
        try:
            client = self._client(venue, "Futures")
            if client is None or not getattr(client, "has", {}).get("fetchFundingRates"):
                return self._native_bulk_funding_rates(venue)
            with self._client_request_lock(venue, "Futures"):
                payload = client.fetch_funding_rates()
        except Exception:  # noqa: BLE001 - one venue must not stop the cycle.
            return self._native_bulk_funding_rates(venue)
        interval_overrides = self._bulk_funding_interval_overrides(venue)
        items = payload.values() if isinstance(payload, dict) else payload
        rates: dict[str, dict[str, Any]] = {}
        for item in items or []:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            interval = item.get("interval")
            if isinstance(interval, str) and interval.casefold().endswith("h"):
                interval = interval[:-1]
            if not interval and interval_overrides:
                market = (getattr(client, "markets", {}) or {}).get(str(item["symbol"])) or {}
                interval = interval_overrides.get(str(market.get("id") or "").upper())
            fields = _funding_fields(
                item.get("fundingRate"),
                # Never leave a fresh rate sitting on a stale interval. WhiteBIT
                # publishes no interval, so DEXE kept a 1h interval from an old
                # scan against an 8h rate and read 4.27%/day instead of 0.02%.
                interval_hours=interval if interval else DEFAULT_FUNDING_INTERVAL_HOURS,
                next_funding_ms=item.get("fundingTimestamp") or item.get("nextFundingTimestamp"),
            )
            if fields:
                rates[str(item["symbol"])] = fields
        # A venue that answers with nothing is indistinguishable from one that
        # cannot answer at all, and both leave the legs frozen at scan time.
        return rates or self._native_bulk_funding_rates(venue)
    @staticmethod
    def _bulk_funding_interval_overrides(venue: str) -> dict[str, float]:
        """Return venue-published schedules missing from CCXT's bulk payload.

        Aster's bulk funding response includes the live rate but omits its interval.
        The separate public ``fundingInfo`` response publishes the exact schedule
        per contract. Falling back to the market-wide 8h default understated BTW's
        hourly carry by eight times.
        """

        if venue != "Aster":
            return {}
        try:
            payload = _json_url("https://fapi.asterdex.com/fapi/v1/fundingInfo")
        except Exception:  # noqa: BLE001 - the caller retains its explicit fallback.
            return {}
        result: dict[str, float] = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            interval = _optional_number(item.get("fundingIntervalHours"))
            if symbol and interval is not None and interval > 0:
                result[symbol] = interval
        return result

    def refresh(
        self,
        snapshot_path: Path,
        *,
        route_limit: int = 100,
        target_notional_usd: float = 50.0,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        deadline = (
            time.monotonic() + deadline_seconds if deadline_seconds is not None else None
        )
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "unavailable", "updated": 0, "error": type(exc).__name__}
        # The prior delta contains the newest rows from recent rotations.  Use
        # it as the selection baseline and retain its still-live rows below;
        # otherwise every pass forgets the previous one and a rotating warmer
        # can never build more coverage than a single pass.
        _overlay_current_fast_delta(payload, snapshot_path)
        # The dedicated bulk worker already refreshes every venue into the
        # compact live_funding file. Repeating the same 18-venue sweep here made
        # the top-route quote cycle take more than four minutes and delayed the
        # very prices it exists to refresh. Fall back to the inline sweep only
        # when that independent cache is absent or stale.
        funding_summary = (
            {"status": "external_live_funding", "skipped": True, "legs": 0, "venues": 0}
            if _external_funding_is_fresh()
            else self.refresh_all_funding(payload)
        )
        include_leg_funding = funding_summary.get("status") != "external_live_funding"
        rails = public_rails.load_public_rails()
        metadata = token_metadata.load_token_metadata()
        rows_by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in FAST_QUOTE_LANES}
        for bucket in ("api_discovered_rows", "dex_discovered_rows"):
            for row in payload.get(bucket) or []:
                if (
                    not isinstance(row, dict)
                    or _has_permanent_mirage_guard(row)
                    or _cannot_lead_public_lane(row, rails=rails, metadata=metadata)
                ):
                    continue
                lane = _fast_quote_lane(row)
                if lane is None:
                    continue
                spread = _number(row.get("depth_weighted_spread_pct"), -999999.0)
                # A DEX-futures funding farm can have a negative entry basis
                # while still paying exceptional carry.  Spread-only admission
                # starved precisely those routes after their entry basis fell.
                plausible_dex = lane.startswith("DEX-") and abs(spread) <= 90.0
                if plausible_dex or 0.0 <= spread <= 90.0 or row.get("fast_quote_verified_at"):
                    rows_by_lane[lane].append(row)
        selected = _select_fast_quote_rows(rows_by_lane, route_limit=route_limit)
        for lane_rows in rows_by_lane.values():
            for row in lane_rows:
                blockers = [
                    str(item)
                    for item in row.get("blockers") or []
                    if not str(item).startswith("mirage_guard:fast_")
                    and str(item) != "condition:fast_refresh_pending"
                ]
                row["blockers"] = list(dict.fromkeys(blockers))
        leg_jobs: dict[tuple[str, str, str], tuple[dict[str, Any], str]] = {}
        for row in selected:
            for side in ("long", "short"):
                key = _route_leg_key(row, side)
                if key is not None:
                    leg_jobs.setdefault(key, (row, side))
        jobs_by_venue: dict[
            tuple[str, str],
            list[tuple[tuple[str, str, str], dict[str, Any], str]],
        ] = {}
        for key, (row, side) in leg_jobs.items():
            jobs_by_venue.setdefault((key[0], key[1]), []).append((key, row, side))
        leg_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        # Venue groups are independent and almost entirely network-bound. Two
        # workers made a 220-route pass take four to five minutes, leaving the
        # five-minute public freshness window with no scheduling margin. Keep
        # each venue sequential (and therefore inside its own rate limiter),
        # but allow several different venues to make progress concurrently.
        venue_workers = max(
            1,
            min(8, int(os.environ.get("SPREADBOARD_FAST_QUOTE_WORKERS", "4"))),
        )
        dex_batches: list[
            tuple[
                tuple[str, str],
                list[tuple[tuple[str, str, str], dict[str, Any], str]],
            ]
        ] = []
        cex_batches: list[
            tuple[
                tuple[str, str],
                list[tuple[tuple[str, str, str], dict[str, Any], str]],
            ]
        ] = []
        dex_chunk_size = max(
            4,
            min(24, int(os.environ.get("SPREADBOARD_DEX_QUOTE_CHUNK_SIZE", "8"))),
        )
        for venue_key, jobs in jobs_by_venue.items():
            if "okx dex" in venue_key[0].casefold():
                # OKX DEX quotes are stateless HTTP requests. Keeping dozens of
                # them in one venue batch made that single task dominate the
                # entire cycle even after CEX venues were parallelized.
                dex_batches.extend(
                    (venue_key, jobs[offset : offset + dex_chunk_size])
                    for offset in range(0, len(jobs), dex_chunk_size)
                )
            else:
                cex_batches.append((venue_key, jobs))
        # DEX requests share a provider rate gate. Putting every DEX chunk at
        # the front of one pool occupied every worker waiting on that gate and
        # delayed CEX books until late in the cycle. Dedicated pools let exact
        # CEX ladders progress while the DEX rotation advances at its allowed
        # rate, keeping both families inside the same freshness window.
        dex_workers = min(2, max(1, len(dex_batches)))
        cex_workers = min(
            max(1, venue_workers - dex_workers),
            max(1, len(cex_batches)),
        )
        touched = {
            row_id for row_id in (_snapshot_row_key(row) for row in selected) if row_id
        }
        pending = {
            _snapshot_row_key(row): row
            for row in selected
            if _snapshot_row_key(row)
        }
        updated = failed = 0

        def publish_ready(*, final: bool) -> None:
            """Publish routes as soon as both exact legs have completed.

            A provider-safe 25-contract DEX pass takes close to the full
            90-second leader window. Waiting for the last contract before
            publishing made the first successful quote 70-80 seconds old at
            birth and it expired before the next cycle landed. This keeps the
            truth boundary unchanged: every row retains its actual leg time;
            only the atomic publication happens earlier.
            """

            nonlocal updated, failed
            changed = False
            changed_dex = False
            for row_id, row in list(pending.items()):
                keys = [_route_leg_key(row, side) for side in ("long", "short")]
                if not final and any(key is None or key not in leg_cache for key in keys):
                    continue
                if final:
                    # A venue task that reached the deadline leaves its
                    # unattempted keys absent. Treat those as unavailable; do
                    # not start new synchronous network calls after the bounded
                    # pools have deliberately stopped.
                    for key in keys:
                        if key is not None:
                            leg_cache.setdefault(key, None)
                success = self._apply_completed_route_quote(
                    row,
                    leg_cache=leg_cache,
                    target_notional_usd=target_notional_usd,
                )
                if success:
                    updated += 1
                else:
                    failed += 1
                pending.pop(row_id, None)
                changed = True
                changed_dex = changed_dex or _is_dex_route(row)
            if not changed and not final:
                return
            # The complete CEX catalogue is refreshed by its own bulk worker.
            # Publishing each tiny CEX canary batch here only invalidates the
            # large grouped caches; partial publication exists specifically to
            # stop slow on-chain chunks making completed DEX quotes stale.
            if not final and not changed_dex:
                return
            refreshed_at = _utc_now_iso()
            summary = {
                "status": "ok" if updated else "unavailable",
                "updated_at": refreshed_at,
                "updated_routes": updated,
                "failed_routes": failed,
                "selected_routes": len(selected),
                "target_notional_usd": target_notional_usd,
                "funding_legs_refreshed": funding_summary.get("legs", 0),
                "funding_venues": funding_summary.get("venues", 0),
                "cycle_complete": final,
            }
            payload["fast_quote_refresh"] = summary
            _publish_fast_quote_delta(
                snapshot_path,
                payload,
                touched=touched,
                summary=summary,
            )

        with (
            ThreadPoolExecutor(max_workers=dex_workers) as dex_pool,
            ThreadPoolExecutor(max_workers=cex_workers) as cex_pool,
        ):
            futures = [
                dex_pool.submit(
                    self._quote_venue_jobs,
                    venue_key,
                    jobs,
                    target_notional_usd=target_notional_usd,
                    deadline=deadline,
                    include_funding=include_leg_funding,
                )
                for venue_key, jobs in dex_batches
            ]
            futures.extend(
                cex_pool.submit(
                    self._quote_venue_jobs,
                    venue_key,
                    jobs,
                    target_notional_usd=target_notional_usd,
                    deadline=deadline,
                    include_funding=include_leg_funding,
                )
                for venue_key, jobs in cex_batches
            )
            for future in as_completed(futures):
                leg_cache.update(future.result())
                publish_ready(final=False)
        publish_ready(final=True)
        if funding_summary.get("legs"):
            # A funding sweep touches legs across the whole board, which a delta
            # cannot express, so that one still lands in the snapshot itself.
            _atomic_write(snapshot_path, payload)
        return payload["fast_quote_refresh"]

    def _apply_completed_route_quote(
        self,
        row: dict[str, Any],
        *,
        leg_cache: dict[tuple[str, str, str], dict[str, Any] | None],
        target_notional_usd: float,
    ) -> bool:
        """Apply one completed matched-size route quote without changing its time."""

        blockers = [
            str(item)
            for item in row.get("blockers") or []
            if not str(item).startswith("mirage_guard:fast_")
            and str(item) != "condition:fast_refresh_pending"
        ]
        long_quote = self._leg_quote(
            row,
            "long",
            target_notional_usd=target_notional_usd,
            cache=leg_cache,
            include_funding=True,
        )
        short_quote = self._leg_quote(
            row,
            "short",
            target_notional_usd=target_notional_usd,
            cache=leg_cache,
            include_funding=True,
        )
        if long_quote is None or short_quote is None:
            if _retain_current_fast_quote(row):
                # This is an operational refresh warning, not an identity or
                # price-integrity failure. Marking it as a mirage guard would
                # make lane_rankable() hide the still-current exact quote.
                blockers.append("condition:fast_refresh_pending")
            else:
                blockers.append("mirage_guard:fast_requote_unavailable")
                _retire_failed_fast_quote(row)
            row["blockers"] = list(dict.fromkeys(blockers))
            return False
        executable = spread_pct(long_quote["ask"], short_quote["bid"])
        depth = spread_pct(long_quote["ask_vwap"], short_quote["bid_vwap"])
        if executable is None or depth is None:
            blockers.append("mirage_guard:fast_target_depth_unavailable")
            row["blockers"] = list(dict.fromkeys(blockers))
            _retire_failed_fast_quote(row)
            return False
        if _is_dex_route(row) and max(executable, depth) > 90.0:
            blockers.append("mirage_guard:fast_spread_out_of_bounds")
            row["blockers"] = list(dict.fromkeys(blockers))
            _retire_failed_fast_quote(row)
            return False
        notes = row.setdefault("notes", {})
        route_inputs = notes.setdefault("route_inputs", {})
        route_inputs["long"] = {**(route_inputs.get("long") or {}), **long_quote}
        route_inputs["short"] = {**(route_inputs.get("short") or {}), **short_quote}
        row["executable_spread_pct"] = f"{executable:.8f}".rstrip("0").rstrip(".")
        row["depth_weighted_spread_pct"] = f"{depth:.8f}".rstrip("0").rstrip(".")
        row["displayed_open_spread_pct"] = row["executable_spread_pct"]
        blockers = [item for item in blockers if item != "depth_unverified"]
        row["depth_usd"] = target_notional_usd
        row["depth_unverified"] = False
        row["quote_ts_us"] = min(long_quote["quote_ts_us"], short_quote["quote_ts_us"])
        row["fast_quote_verified_at"] = _utc_now_iso()
        row["blockers"] = list(dict.fromkeys(blockers))
        return True

    def _quote_venue_jobs(
        self,
        venue_key: tuple[str, str],
        jobs: list[tuple[tuple[str, str, str], dict[str, Any], str]],
        *,
        target_notional_usd: float,
        deadline: float | None = None,
        include_funding: bool = True,
    ) -> dict[tuple[str, str, str], dict[str, Any] | None]:
        cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        # The one-call-per-venue bulk worker continuously refreshes the complete
        # CEX catalogue.  DEX route publication should spend its scarce provider
        # budget on the on-chain leg, rather than loading the same CEX venue
        # metadata again.  The cached book's original timestamp is preserved,
        # and the completed route still has to pass the 90-second truth gate.
        cache_book_age_seconds = max(
            5.0,
            min(
                30.0,
                float(os.environ.get("SPREADBOARD_FAST_CEX_BOOK_AGE_SECONDS", "20")),
            ),
        )
        try:
            for key, row, side in jobs:
                # Stopping a venue short is far better than being killed mid
                # cycle: the parent runs this as a subprocess with a hard
                # timeout, and a kill discards every quote taken so far.
                if deadline is not None and time.monotonic() >= deadline:
                    break
                cache[key] = self._leg_quote(
                    row,
                    side,
                    target_notional_usd=target_notional_usd,
                    cache=cache,
                    include_funding=include_funding,
                    cache_book_age_seconds=cache_book_age_seconds,
                )
        finally:
            self._discard_client(*venue_key)
        return cache

    def quote_route(
        self,
        row: dict[str, Any],
        *,
        target_notional_usd: float = 50.0,
    ) -> dict[str, Any]:
        """Reprice one exact route without changing the broad-board snapshot."""

        quoted = json.loads(json.dumps(row))
        with ThreadPoolExecutor(max_workers=2) as pool:
            long_future = pool.submit(
                self._leg_quote,
                quoted,
                "long",
                target_notional_usd=target_notional_usd,
                cache={},
                include_funding=True,
            )
            short_future = pool.submit(
                self._leg_quote,
                quoted,
                "short",
                target_notional_usd=target_notional_usd,
                cache={},
                include_funding=True,
            )
            long_quote = long_future.result()
            short_quote = short_future.result()
        if long_quote is None or short_quote is None:
            return {
                "status": "unavailable",
                "error": "exact_route_order_book_unavailable",
            }
        long_multiplier, short_multiplier = _relative_value_multipliers(quoted)
        executable = spread_pct(
            long_quote["ask"] * long_multiplier,
            short_quote["bid"] * short_multiplier,
        )
        depth = spread_pct(
            long_quote["ask_vwap"] * long_multiplier,
            short_quote["bid_vwap"] * short_multiplier,
        )
        if executable is None or depth is None:
            return {
                "status": "unavailable",
                "error": "exact_route_target_depth_unavailable",
            }
        if _is_dex_route(quoted) and max(executable, depth) > 90.0:
            return {
                "status": "unavailable",
                "error": "exact_route_spread_out_of_bounds",
            }
        notes = quoted.setdefault("notes", {})
        route_inputs = notes.setdefault("route_inputs", {})
        _carry_forward_funding(quoted, "long", long_quote)
        _carry_forward_funding(quoted, "short", short_quote)
        route_inputs["long"] = {**(route_inputs.get("long") or {}), **long_quote}
        route_inputs["short"] = {**(route_inputs.get("short") or {}), **short_quote}
        _sync_quoted_funding(quoted, long_quote, short_quote)
        quoted["executable_spread_pct"] = executable
        quoted["depth_weighted_spread_pct"] = depth
        quoted["quote_ts_us"] = min(long_quote["quote_ts_us"], short_quote["quote_ts_us"])
        quoted["target_notional_usd"] = target_notional_usd
        return {
            "status": "ok",
            "sample_source": "live_chart_exact_route",
            "target_notional_usd": target_notional_usd,
            "row": quoted,
        }

    def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self._clients.clear()
        self._client_request_locks.clear()
        gc.collect()

    def _leg_quote(
        self,
        row: dict[str, Any],
        side: str,
        *,
        target_notional_usd: float,
        cache: dict[tuple[str, str, str], dict[str, Any] | None],
        include_funding: bool,
        cache_book_age_seconds: float = 5.0,
    ) -> dict[str, Any] | None:
        venue = str(row.get(f"{side}_venue") or "")
        market_type = str(row.get(f"{side}_market_type") or "")
        notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
        route_inputs = (
            notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
        )
        leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
        symbol = str(
            leg.get("symbol") or row.get(f"{side}_market_symbol") or row.get(f"{side}_symbol") or ""
        )
        if "okx dex" in venue.casefold():
            chain, contract = _dex_chain_contract(row, side=side)
            # The opening route only consumes the long leg's ask or the short
            # leg's bid.  Keep direction in the key so one exact contract can
            # be reused across many same-direction pairings without making a
            # long-side quote masquerade as a current short-side quote.
            key = (venue, market_type, f"{chain}:{contract}:{side}")
        else:
            key = (venue, market_type, symbol)
        if key in cache:
            return cache[key]
        if "okx dex" in venue.casefold():
            value = _okx_dex_leg_quote(
                row,
                side,
                target_notional_usd=target_notional_usd,
                quote_both=False,
            )
            cache[key] = value
            return value
        # Some venues have no ccxt adapter but do publish a public book, and
        # this gate rejected them before the native fetcher below was ever
        # reached -- so their legs could never be repriced and their charts sat
        # frozen reading "Stream sampler unavailable".
        if not venue or not symbol:
            return None
        if venue not in VENUE_IDS and not supports_native_order_book(venue, market_type):
            return None
        try:
            route_has_dex_leg = _is_dex_route(row)
            live_book = live_book_cache.load_live_book(
                venue,
                market_type,
                symbol,
                # Direct route/chart samples keep the five-second default.
                # Broad refresh jobs may reuse a bounded recent CEX book. The
                # route timestamp below is still min(DEX, CEX), so reuse cannot
                # make either leg younger than it really is.
                max_age_seconds=cache_book_age_seconds,
            )
            native_book = (
                (live_book.bids, live_book.asks)
                if live_book is not None
                else _native_order_book(venue, market_type, symbol)
            )
            if native_book is None and route_has_dex_leg:
                # All CEX venues selected for DEX publication have a lightweight
                # native order-book adapter. If that exact endpoint cannot fill
                # the $50 probe, do not load the exchange's entire market
                # catalogue as a fallback: that 60-100s metadata path made the
                # already-completed on-chain quote expire before publication.
                cache[key] = None
                return None
            if native_book is None and venue not in VENUE_IDS:
                # No public book this time and no ccxt adapter to fall back on.
                cache[key] = None
                return None
            if native_book is None:
                client = self._client(venue, market_type)
                with self._client_request_lock(venue, market_type):
                    market = client.market(symbol)
                    book = client.fetch_order_book(symbol, limit=BOOK_DEPTH_LEVELS)
                    funding = (
                        _ccxt_current_funding(client, symbol, venue=venue)
                        if include_funding and market_type == "Futures"
                        else {}
                    )
                    if (
                        include_funding
                        and market_type == "Futures"
                        and (
                            funding.get("current_funding_pct") is None
                            or funding.get("funding_interval_hours") is None
                        )
                    ):
                        funding = {
                            **funding,
                            **{
                                key: value
                                for key, value in _native_current_funding(venue, symbol).items()
                                if value is not None
                            },
                        }
                    contract_size = (
                        _number(market.get("contractSize"), 1.0)
                        if market_type == "Futures"
                        else 1.0
                    )
                bids = _levels(book.get("bids"))
                asks = _levels(book.get("asks"))
            else:
                bids, asks = native_book
                funding = (
                    _native_current_funding(venue, symbol)
                    if include_funding and market_type == "Futures"
                    else {}
                )
                # The shared cache stores futures amounts already normalised to
                # base-asset quantity; native REST books still use contracts.
                # Applying the contract multiplier to a cached book again made
                # 100x contracts look one hundred times deeper than reality.
                contract_size = (
                    1.0
                    if live_book is not None
                    else _number(
                        leg.get("contract_size") or row.get(f"{side}_contract_size"),
                        1.0,
                    )
                )
            bid_vwap = depth_weighted_price(bids, target_notional_usd, contract_size=contract_size)
            ask_vwap = depth_weighted_price(asks, target_notional_usd, contract_size=contract_size)
            if not bids or not asks or bid_vwap is None or ask_vwap is None:
                cache[key] = None
                return None
            value = {
                "symbol": symbol,
                "bid": bids[0][0],
                "ask": asks[0][0],
                "bid_vwap": bid_vwap,
                "ask_vwap": ask_vwap,
                "contract_size": contract_size,
                "quote_ts_us": (
                    live_book.quote_ts_us if live_book is not None else int(time.time() * 1_000_000)
                ),
                "quote_source": (live_book.source if live_book is not None else "public_rest"),
                **funding,
            }
        except Exception:
            value = None
        cache[key] = value
        return value

    def _client(self, venue: str, market_type: str) -> Any:
        import ccxt

        key = (venue, market_type)
        with self._client_lock:
            client = self._clients.get(key)
            if client is not None:
                return client
            exchange_id = VENUE_IDS[venue]
            aliases = {
                "gateio": ("gateio", "gate"),
                "gate": ("gate", "gateio"),
            }
            klass = next(
                (
                    getattr(ccxt, candidate)
                    for candidate in aliases.get(exchange_id, (exchange_id,))
                    if hasattr(ccxt, candidate)
                ),
                None,
            )
            if klass is None:
                raise AttributeError(f"CCXT exchange adapter unavailable: {exchange_id}")
            client = klass(
                {
                    "enableRateLimit": True,
                    "timeout": 8_000,
                    "options": {"defaultType": "spot" if market_type == "Spot" else "swap"},
                }
            )
            client.load_markets()
            self._clients[key] = client
            self._client_request_locks.setdefault(key, Lock())
            return client

    def _client_request_lock(self, venue: str, market_type: str) -> Lock:
        key = (venue, market_type)
        with self._client_lock:
            return self._client_request_locks.setdefault(key, Lock())

    def _discard_client(self, venue: str, market_type: str) -> None:
        key = (venue, market_type)
        client = self._clients.pop(key, None)
        self._client_request_locks.pop(key, None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        gc.collect()


def _carry_forward_funding(
    row: dict[str, Any], side: str, quote: dict[str, Any]
) -> None:
    """Keep a recent board rate when the exact book endpoint omits funding."""

    if str(row.get(f"{side}_market_type") or "") != "Futures":
        return
    if quote.get("current_funding_pct") is None:
        current = _optional_number(
            row.get(f"{side}_current_funding_pct", row.get(f"{side}_funding_pct"))
        )
        if current is not None:
            quote["current_funding_pct"] = current
    if quote.get("funding_interval_hours") is None:
        interval = _optional_number(row.get(f"{side}_funding_interval_hours"))
        if interval is not None:
            quote["funding_interval_hours"] = interval
    if quote.get("next_funding_ts_us") is None:
        upcoming = _optional_int(row.get(f"{side}_next_funding_ts_us"))
        if upcoming is not None:
            quote["next_funding_ts_us"] = upcoming


def _sync_quoted_funding(
    row: dict[str, Any],
    long_quote: dict[str, Any],
    short_quote: dict[str, Any],
) -> None:
    """Make exact sampled cadence the row's displayed funding cadence."""

    daily: dict[str, float] = {"long": 0.0, "short": 0.0}
    for side, quote in (("long", long_quote), ("short", short_quote)):
        if str(row.get(f"{side}_market_type") or "") != "Futures":
            continue
        rate = _optional_number(quote.get("current_funding_pct"))
        interval = _optional_number(quote.get("funding_interval_hours"))
        upcoming = _optional_int(quote.get("next_funding_ts_us"))
        if rate is not None:
            row[f"{side}_current_funding_pct"] = rate
            row[f"{side}_funding_pct"] = rate
        if interval is not None and interval > 0:
            row[f"{side}_funding_interval_hours"] = interval
            row[f"{side}_funding_interval_assumed"] = False
        if upcoming is not None:
            row[f"{side}_next_funding_ts_us"] = upcoming
        if rate is not None and interval is not None and interval > 0:
            daily[side] = rate * (24.0 / interval)
    projected = daily["short"] - daily["long"]
    row["funding_projected_24h_pct"] = projected
    row["funding_daily_pct"] = projected
    row["funding_spread_pct"] = projected
    row["funding_apr_pct"] = projected * 365.0


def _external_funding_is_fresh(*, max_age_seconds: float = 600.0) -> bool:
    """Whether the independent all-venue funding sweep is current enough."""

    path = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data")) / "live_funding.json"
    try:
        return 0.0 <= time.time() - path.stat().st_mtime <= max_age_seconds
    except OSError:
        return False


def _fast_quote_delta_path(snapshot_path: Path) -> Path:
    return Path(snapshot_path).with_name("api_discovery_fast_quotes.json")


def _publish_fast_quote_delta(
    snapshot_path: Path,
    payload: dict[str, Any],
    *,
    touched: set[str],
    summary: dict[str, Any],
) -> None:
    """Atomically expose a partial or complete fast-quote generation.

    Rows keep their actual quote timestamps. Publishing a partial generation
    therefore improves availability without extending the life of an old DEX
    mark or weakening any downstream freshness check.
    """

    now_us = int(time.time() * 1_000_000)
    retention_seconds = max(
        60.0,
        float(os.environ.get("SPREADBOARD_LIVE_MAX_AGE_MIN", "5")) * 60.0,
    )
    rows = [
        row
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in payload.get(bucket) or []
        if isinstance(row, dict)
        and (
            _snapshot_row_key(row) in touched
            or _fresh_fast_quote_row(
                row,
                now_us=now_us,
                max_age_seconds=retention_seconds,
            )
        )
    ]
    lane_tokens: dict[str, set[str]] = {"DEX-FUTURES": set(), "DEX-SPOT": set()}
    for row in rows:
        lane = _fast_quote_lane(row)
        if lane not in lane_tokens or not _fresh_fast_quote_row(
            row,
            now_us=now_us,
            # This is the executable-spread promise, not the wider searchable
            # row retention window.
            max_age_seconds=90.0,
        ):
            continue
        token = str(row.get("token") or "").upper()
        if token:
            lane_tokens[lane].add(token)
    summary = {
        **summary,
        "lane_token_counts": {
            lane: len(tokens) for lane, tokens in lane_tokens.items()
        },
        "top_25_ready": {
            lane: len(tokens) >= 25 for lane, tokens in lane_tokens.items()
        },
    }
    _atomic_write(
        _fast_quote_delta_path(snapshot_path),
        {
            "schema": "spreadboard.fast_quote_delta.v1",
            "updated_at": summary["updated_at"],
            "fast_quote_refresh": summary,
            "rows": rows,
        },
    )


def _overlay_current_fast_delta(payload: dict[str, Any], snapshot_path: Path) -> int:
    """Overlay newer fast rows before selecting the next rotation."""

    try:
        delta = json.loads(_fast_quote_delta_path(snapshot_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    newer = {
        _snapshot_row_key(row): row
        for row in delta.get("rows") or []
        if isinstance(row, dict) and _snapshot_row_key(row)
    }
    overlaid = 0
    for bucket in ("api_discovered_rows", "dex_discovered_rows"):
        rows = payload.get(bucket)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            replacement = newer.get(_snapshot_row_key(row))
            if replacement is None:
                continue
            if _number(replacement.get("quote_ts_us"), 0.0) < _number(
                row.get("quote_ts_us"), 0.0
            ):
                continue
            rows[index] = replacement
            overlaid += 1
    return overlaid


def _fresh_fast_quote_row(
    row: dict[str, Any],
    *,
    now_us: int,
    max_age_seconds: float,
) -> bool:
    if not row.get("fast_quote_verified_at"):
        return False
    quoted_us = int(_number(row.get("quote_ts_us"), 0.0))
    return quoted_us > 0 and 0 <= now_us - quoted_us <= max_age_seconds * 1_000_000


def _retire_failed_fast_quote(row: dict[str, Any]) -> None:
    """Make a failed current check non-live until a later cycle recovers it."""

    row["quote_ts_us"] = 0
    row["fast_quote_verified_at"] = None
    row["freshness"] = "stale"
    row["status"] = "refreshing"


def _retain_current_fast_quote(
    row: dict[str, Any],
    *,
    now_us: int | None = None,
    max_age_seconds: float = 90.0,
) -> bool:
    """Preserve a prior exact quote only for the remainder of its truth TTL.

    A transient provider or CEX refresh failure is not evidence that a quote
    verified seconds earlier became false. Zeroing that timestamp immediately
    made otherwise healthy DEX lanes disappear on alternating rotations. The
    original timestamp is never changed, so the normal freshness gate still
    removes the row at exactly the same 90-second boundary.
    """

    current_us = int(time.time() * 1_000_000) if now_us is None else now_us
    if _fresh_fast_quote_row(
        row,
        now_us=current_us,
        max_age_seconds=max_age_seconds,
    ):
        row["status"] = "live_last_verified"
        return True
    return False


def _snapshot_row_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "")
        for key in (
            "token",
            "long_venue",
            "long_market_type",
            "long_market_symbol",
            "short_venue",
            "short_market_type",
            "short_market_symbol",
        )
    )


def _levels(value: Any) -> list[list[float]]:
    output = []
    for item in value or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        price = _number(item[0], 0.0)
        amount = _number(item[1], 0.0)
        if price > 0 and amount > 0:
            output.append([price, amount])
    return output


def _route_leg_key(
    row: dict[str, Any],
    side: str,
) -> tuple[str, str, str] | None:
    venue = str(row.get(f"{side}_venue") or "")
    market_type = str(row.get(f"{side}_market_type") or "")
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    route_inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
    symbol = str(
        leg.get("symbol") or row.get(f"{side}_market_symbol") or row.get(f"{side}_symbol") or ""
    )
    if "okx dex" in venue.casefold():
        chain, contract = _dex_chain_contract(row, side=side)
        symbol = f"{chain}:{contract}:{side}" if chain and contract else ""
    return (venue, market_type, symbol) if venue and market_type and symbol else None


def _fast_quote_lane(row: dict[str, Any]) -> str | None:
    long_type = str(row.get("long_market_type") or "")
    short_type = str(row.get("short_market_type") or "")
    long_venue = str(row.get("long_venue") or "")
    short_venue = str(row.get("short_venue") or "")
    venues = (long_venue, short_venue)
    has_okx_dex = any("okx dex" in venue.casefold() for venue in venues)
    cex_supported = all(venue in VENUE_IDS or "okx dex" in venue.casefold() for venue in venues)
    if not cex_supported:
        return None
    if has_okx_dex:
        blockers = {str(item) for item in row.get("blockers") or []}
        identity_unverified = (
            "cex_identity_unverified" in blockers
            or "identity_unverified" in blockers
            or any(item.startswith("identity_collision:") for item in blockers)
        )
        chain, contract = _dex_chain_contract(row)
        if identity_unverified or not chain or not contract:
            return None
        if {long_type, short_type} == {"Spot", "Futures"}:
            return "DEX-FUTURES"
        if long_type == short_type == "Spot":
            return "DEX-SPOT"
        return None
    if long_type == short_type == "Futures":
        return "FUTURES"
    if {long_type, short_type} == {"Spot", "Futures"}:
        return "FUTURES-SPOT"
    if long_type == short_type == "Spot":
        return "SPOT"
    return None


def _is_dex_route(row: dict[str, Any]) -> bool:
    return any(
        "okx dex" in str(row.get(f"{side}_venue") or "").casefold() for side in ("long", "short")
    )


def _unique_token_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    ranked = sorted(
        rows,
        key=lambda row: _number(row.get("depth_weighted_spread_pct"), -999999.0),
        reverse=True,
    )
    for row in ranked:
        token = str(row.get("token") or "").upper()
        if not token or token in seen:
            continue
        seen.add(token)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _expanded_token_rows(
    rows: list[dict[str, Any]],
    *,
    token_limit: int,
    route_limit: int,
) -> list[dict[str, Any]]:
    """Select one route per top token first, then its other ranked venue routes."""

    if route_limit <= 0 or token_limit <= 0:
        return []
    ranked = sorted(
        rows,
        key=lambda row: _number(row.get("depth_weighted_spread_pct"), -999999.0),
        reverse=True,
    )
    seeds = _unique_token_rows(ranked, limit=min(token_limit, route_limit))
    selected = list(seeds)
    selected_ids = {id(row) for row in selected}
    selected_tokens = {str(row.get("token") or "").upper() for row in selected}
    for row in ranked:
        if len(selected) >= route_limit:
            break
        token = str(row.get("token") or "").upper()
        if token in selected_tokens and id(row) not in selected_ids:
            selected.append(row)
            selected_ids.add(id(row))
    return selected


def _dex_priority_tokens() -> set[str]:
    """Public symbols whose DEX marks must not disappear between scans.

    This intentionally reads only token symbols: no account, user, position or
    PII fields leave the private database.  The static identity watchlist is
    also included because those are the DEX assets whose contract identity has
    already been reviewed.
    """

    tokens = {
        item.strip().upper()
        for item in os.environ.get("SPREADBOARD_DEX_PRIORITY_TOKENS", "").split(",")
        if item.strip()
    }
    try:
        from spreadboard import accounts

        tokens.update(
            str(item).upper()
            for item in accounts.all_watchlist_symbols(db_path=accounts.DEFAULT_DB_PATH)
        )
        tokens.update(
            str(item).upper()
            for item in accounts.all_open_position_symbols(db_path=accounts.DEFAULT_DB_PATH)
        )
    except Exception:
        pass
    try:
        watchlist = json.loads(
            (ROOT / "data" / "api_discovery_watchlist.json").read_text(encoding="utf-8")
        )
        tokens.update(
            str(item.get("symbol") or "").upper()
            for item in watchlist.get("tokens") or []
            if isinstance(item, dict) and item.get("symbol")
        )
    except (OSError, json.JSONDecodeError):
        pass
    return tokens


def _dex_opportunity_score(row: dict[str, Any]) -> float:
    """Independent current spread and carry evidence for DEX warm priority."""

    spread = max(0.0, _number(row.get("depth_weighted_spread_pct"), 0.0))
    carry = max(
        0.0,
        _number(
            row.get("funding_projected_24h_pct")
            or row.get("funding_daily_pct")
            or row.get("funding_spread_pct"),
            0.0,
        ),
    )
    return spread + carry


def _dex_rotating_rows(
    rows: list[dict[str, Any]],
    *,
    priority_tokens: set[str],
    route_limit: int,
) -> list[dict[str, Any]]:
    """Keep monitored DEX assets and current leaders warm without starvation.

    Reviewed/member-tracked tokens go first, oldest quote first; remaining
    slots go to current spread or funding leaders.  Because
    recent rows are retained in the rolling delta, old rows rise on the next
    pass and the set rotates rather than pinning the same headline tokens.
    """

    if route_limit <= 0:
        return []
    ranked = sorted(
        rows,
        key=lambda row: (
            _dex_opportunity_score(row),
            _number(row.get("depth_weighted_spread_pct"), -999999.0),
        ),
        reverse=True,
    )
    priority = [
        row
        for row in rows
        if str(row.get("token") or "").upper() in priority_tokens
    ]
    priority.sort(
        key=lambda row: (
            _number(row.get("quote_ts_us"), 0.0),
            -_dex_opportunity_score(row),
        )
    )
    # Every open-position/watchlist token fits before opportunistic leaders.
    # Dropping one merely because its current score cooled defeats the reason
    # it was pinned; public symbols only, never account or position data.
    priority_slots = min(len(priority_tokens), route_limit)
    seeds = _unique_rows_by_token(priority, priority_slots)
    selected_tokens = {str(row.get("token") or "").upper() for row in seeds}
    for row in _unique_rows_by_token(ranked, route_limit):
        if len(seeds) >= route_limit:
            break
        token = str(row.get("token") or "").upper()
        if token not in selected_tokens:
            seeds.append(row)
            selected_tokens.add(token)
    return seeds


def _select_fast_quote_rows(
    rows_by_lane: dict[str, list[dict[str, Any]]],
    *,
    route_limit: int,
    priority_tokens: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Allocate current-book work without making the DEX cycle self-stale.

    OKX Web3 needs two rate-limited requests per token. Treating DEX-FUTURES
    and DEX-SPOT as independent quotas sampled the same contract twice and
    selected 70+ rows; the earliest quote had expired before the atomic delta
    was published. One combined token rotation quotes the shared DEX mark once
    and leaves a few slots for current leaders after every tracked token.
    """

    cex_lanes = [lane for lane in FAST_QUOTE_LANES if not lane.startswith("DEX-")]
    dex_rows = [
        *rows_by_lane.get("DEX-FUTURES", []),
        *rows_by_lane.get("DEX-SPOT", []),
    ]
    active_cex_lanes = sum(bool(rows_by_lane.get(lane)) for lane in cex_lanes)
    dex_route_limit = (
        min(
            max(0, route_limit - active_cex_lanes),
            max(8, int(os.environ.get("SPREADBOARD_FAST_DEX_ROUTES", "70"))),
        )
        if dex_rows
        else 0
    )
    cex_route_limit = max(0, route_limit - dex_route_limit)
    cex_weight = sum(FAST_QUOTE_LANE_WEIGHTS[lane] for lane in cex_lanes)
    base_quota, extra = divmod(cex_route_limit, cex_weight)
    selected: list[dict[str, Any]] = []
    for index, lane in enumerate(cex_lanes):
        lane_limit = (
            base_quota * FAST_QUOTE_LANE_WEIGHTS[lane]
            + (1 if index < extra else 0)
        )
        selected.extend(
            _expanded_token_rows(
                rows_by_lane.get(lane) or [],
                token_limit=min(50, lane_limit),
                route_limit=lane_limit,
            )
        )
    dex_token_limit = min(
        dex_route_limit,
        max(8, int(os.environ.get("SPREADBOARD_FAST_DEX_ROUTES", "70"))),
    )
    priority = priority_tokens if priority_tokens is not None else _dex_priority_tokens()
    # DEX-FUTURES and DEX-SPOT often share one contract, but they are separate
    # client lanes. Selecting from their combined score alone let a rich
    # futures universe consume the provider budget and left Spot-DEX below the
    # public top-25 readiness boundary. Seed each lane independently, then add
    # combined leaders up to the configured unique-contract ceiling. Shared
    # contracts are still quoted once by leg_cache below.
    # One opening-direction OKX call is needed per exact directional leg. A
    # 70-contract rotation still cannot finish reliably inside the public
    # 90-second spread boundary at the provider-safe request cadence. Spend the
    # 70 route rows on at most 28 exact contracts, preferentially contracts
    # shared by both DEX lanes. Production currently has enough overlap for 25
    # independently ranked tokens in each lane. Small diagnostic/test budgets
    # retain the simple rotation semantics.
    if dex_route_limit < 30:
        dex_seeds = _dex_rotating_rows(
            dex_rows,
            priority_tokens=priority,
            route_limit=dex_token_limit,
        )
    else:
        contract_limit = min(
            dex_token_limit,
            max(12, int(os.environ.get("SPREADBOARD_FAST_DEX_CONTRACTS", "28"))),
        )
        dex_seeds = _shared_dex_lane_seeds(
            rows_by_lane,
            priority_tokens=priority,
            route_limit=dex_route_limit,
            contract_limit=contract_limit,
            # Production observations include two to five thin, unavailable,
            # or structurally rejected contracts in an otherwise healthy pass.
            # Keep thirty leaders in each lane so those failures do not reduce
            # the member-visible set below twenty-five. Every fallback is still
            # an exact quote with its original timestamp.
            lane_floor=min(contract_limit, 30, dex_route_limit // 2),
        )
    # The DEX provider is charged once per contract, not once per paired route:
    # leg_cache reuses that exact quote. Spend the remaining route budget on
    # other current CEX pairings for the already-selected tokens so token pages
    # and Telegram do not collapse to one arbitrary pair per DEX asset.
    selected.extend(_expand_selected_dex_tokens(dex_seeds, dex_rows, dex_route_limit))
    return selected


def _shared_dex_lane_seeds(
    rows_by_lane: dict[str, list[dict[str, Any]]],
    *,
    priority_tokens: set[str],
    route_limit: int,
    contract_limit: int,
    lane_floor: int,
) -> list[dict[str, Any]]:
    """Cover both public DEX lanes with the fewest provider quote calls."""

    lane_maps: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    for lane in ("DEX-FUTURES", "DEX-SPOT"):
        best: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows_by_lane.get(lane) or []:
            identity = _dex_contract_identity(row)
            current = best.get(identity)
            if current is None or _dex_pair_selection_score(row) > _dex_pair_selection_score(current):
                best[identity] = row
        lane_maps[lane] = best

    futures = lane_maps["DEX-FUTURES"]
    spot = lane_maps["DEX-SPOT"]
    shared = set(futures) & set(spot)

    def identity_score(identity: tuple[str, str, str]) -> tuple[int, float, float]:
        rows = [row for row in (futures.get(identity), spot.get(identity)) if row]
        token = identity[0]
        return (
            int(token in priority_tokens),
            max((_dex_opportunity_score(row) for row in rows), default=0.0),
            -min((_number(row.get("quote_ts_us"), 0.0) for row in rows), default=0.0),
        )

    # Small provider budgets rotate through a larger leader pool. Production
    # uses at least 25 contracts, however, and must keep one complete public
    # top-25 set in view. Rotating two halves looked cheaper but the first half
    # expired while the following subprocess was still running. A production
    # buffer above 25 provides honest fallbacks for transient provider failures.
    leader_pool_size = (
        contract_limit if contract_limit >= 25 else max(25, contract_limit * 2)
    )
    shared_leader_pool = set(
        sorted(shared, key=identity_score, reverse=True)[:leader_pool_size]
    )
    shared_leader_pool.update(
        identity for identity in shared if identity[0] in priority_tokens
    )

    def rotation_score(identity: tuple[str, str, str]) -> tuple[int, float, float]:
        rows = [row for row in (futures.get(identity), spot.get(identity)) if row]
        oldest_quote = min(
            (_number(row.get("quote_ts_us"), 0.0) for row in rows),
            default=0.0,
        )
        # Current leaders are the product promise. Rotate oldest-first *within*
        # the top shared leader pool, not above it, or historical tail assets
        # can displace the 25 routes a subscriber is actually trying to watch.
        return (
            -oldest_quote,
            int(identity[0] in priority_tokens),
            max((_dex_opportunity_score(row) for row in rows), default=0.0),
        )

    selected_identities = sorted(
        shared_leader_pool,
        key=rotation_score,
        reverse=True,
    )[:lane_floor]
    selected = set(selected_identities)

    all_identities = set(futures) | set(spot)
    # When overlap is sparse, share the remaining contract budget evenly. It
    # may be mathematically impossible to reach 25+25 inside the contract cap, but
    # one lane must never consume every slot merely because set ordering changed.
    ranked_by_lane = {
        "DEX-FUTURES": sorted(set(futures) - selected, key=identity_score, reverse=True),
        "DEX-SPOT": sorted(set(spot) - selected, key=identity_score, reverse=True),
    }
    lane_coverage = {
        "DEX-FUTURES": sum(identity in futures for identity in selected),
        "DEX-SPOT": sum(identity in spot for identity in selected),
    }
    cursors = {"DEX-FUTURES": 0, "DEX-SPOT": 0}
    while len(selected) < contract_limit and any(
        lane_coverage[lane] < lane_floor for lane in ranked_by_lane
    ):
        progressed = False
        for lane, ranked in ranked_by_lane.items():
            while cursors[lane] < len(ranked) and ranked[cursors[lane]] in selected:
                cursors[lane] += 1
            if lane_coverage[lane] >= lane_floor or cursors[lane] >= len(ranked):
                continue
            identity = ranked[cursors[lane]]
            cursors[lane] += 1
            selected.add(identity)
            lane_coverage["DEX-FUTURES"] += int(identity in futures)
            lane_coverage["DEX-SPOT"] += int(identity in spot)
            progressed = True
            if len(selected) >= contract_limit:
                break
        if not progressed:
            break

    # Never evict a member-tracked/open-position contract merely because its
    # current score cooled. Shared lane coverage is fixed first, then tracked
    # contracts and current leaders fill the remaining provider budget.
    priority_identities = [
        identity for identity in all_identities if identity[0] in priority_tokens
    ]
    for identity in sorted(priority_identities, key=identity_score, reverse=True):
        if len(selected) >= contract_limit:
            break
        selected.add(identity)
    for identity in sorted(all_identities, key=identity_score, reverse=True):
        if len(selected) >= contract_limit:
            break
        selected.add(identity)

    seeds: list[dict[str, Any]] = []
    # Shared contracts first: one provider quote updates a route in each lane.
    for identity in selected_identities:
        for lane_map in (futures, spot):
            row = lane_map.get(identity)
            if row is not None and len(seeds) < route_limit:
                seeds.append(row)
    for identity in sorted(selected - set(selected_identities), key=identity_score, reverse=True):
        if len(seeds) >= route_limit:
            break
        candidates = [row for row in (futures.get(identity), spot.get(identity)) if row]
        if candidates:
            seeds.append(max(candidates, key=_dex_opportunity_score))
    return seeds


def _expand_selected_dex_tokens(
    seeds: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    identities = {_dex_contract_identity(row) for row in seeds}
    selected = list(seeds[:limit])
    seen = {_snapshot_row_key(row) for row in selected}
    # A selected contract may have been seeded from only one lane when shared
    # coverage already met its floor. Add its best missing public-lane seed
    # before ordinary pair fallbacks; otherwise the same exact DEX quote is paid
    # for but cannot appear in the other valid lane.
    present_groups = {
        (_dex_contract_identity(row), _fast_quote_lane(row) or "") for row in selected
    }
    missing_group_best: dict[
        tuple[tuple[str, str, str], str], dict[str, Any]
    ] = {}
    for row in rows:
        group = (_dex_contract_identity(row), _fast_quote_lane(row) or "")
        if group[0] not in identities or group in present_groups:
            continue
        current = missing_group_best.get(group)
        if current is None or _dex_pair_selection_score(row) > _dex_pair_selection_score(current):
            missing_group_best[group] = row
    for row in sorted(missing_group_best.values(), key=_dex_pair_selection_score, reverse=True):
        if len(selected) >= limit:
            break
        key = _snapshot_row_key(row)
        if key in seen:
            continue
        selected.append(row)
        seen.add(key)
        present_groups.add((_dex_contract_identity(row), _fast_quote_lane(row) or ""))

    # The spare budget is insurance against one unavailable CEX pairing, not a
    # second leaderboard.  A global opportunity sort let one rich token consume
    # every spare row while thirty other selected token/lane groups had no
    # fallback.  Allocate one alternative per exact contract and public lane
    # before giving any group a third route.  Groups whose seed has never
    # produced a fast exact quote are tried first; a successful alternative then
    # becomes next cycle's preferred seed via _dex_pair_selection_score().
    seed_by_group: dict[tuple[tuple[str, str, str], str], dict[str, Any]] = {}
    group_order: list[tuple[tuple[str, str, str], str]] = []
    for row in selected:
        group = (_dex_contract_identity(row), _fast_quote_lane(row) or "")
        if group not in seed_by_group:
            group_order.append(group)
            seed_by_group[group] = row

    buckets: dict[tuple[tuple[str, str, str], str], list[dict[str, Any]]] = {}
    for row in rows:
        identity = _dex_contract_identity(row)
        key = _snapshot_row_key(row)
        if identity not in identities or key in seen:
            continue
        group = (identity, _fast_quote_lane(row) or "")
        if group not in seed_by_group:
            continue
        buckets.setdefault(group, []).append(row)
    for candidates in buckets.values():
        candidates.sort(key=_dex_pair_selection_score, reverse=True)

    group_order.sort(
        key=lambda group: (
            int(not bool(seed_by_group[group].get("fast_quote_verified_at"))),
            -_number(seed_by_group[group].get("quote_ts_us"), 0.0),
            _dex_opportunity_score(seed_by_group[group]),
        ),
        reverse=True,
    )
    # Keep the fallback budget symmetric. A score-only ordering could spend all
    # twenty production spare rows on DEX-SPOT before DEX-FUTURES received one,
    # even though both lanes make the same top-25 availability promise.
    ranked_by_lane: dict[str, list[tuple[tuple[str, str, str], str]]] = {
        lane: [group for group in group_order if group[1] == lane]
        for lane in ("DEX-FUTURES", "DEX-SPOT")
    }
    interleaved: list[tuple[tuple[str, str, str], str]] = []
    for index in range(max((len(values) for values in ranked_by_lane.values()), default=0)):
        for lane in ("DEX-FUTURES", "DEX-SPOT"):
            values = ranked_by_lane[lane]
            if index < len(values):
                interleaved.append(values[index])
    group_order = interleaved + [
        group for group in group_order if group[1] not in ranked_by_lane
    ]
    while len(selected) < limit:
        progressed = False
        for group in group_order:
            candidates = buckets.get(group) or []
            while candidates and _snapshot_row_key(candidates[0]) in seen:
                candidates.pop(0)
            if not candidates:
                continue
            row = candidates.pop(0)
            selected.append(row)
            seen.add(_snapshot_row_key(row))
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _dex_pair_selection_score(row: dict[str, Any]) -> tuple[int, float, float]:
    """Prefer a pairing already proven quoteable, then its current economics."""

    return (
        int(bool(row.get("fast_quote_verified_at"))),
        _number(row.get("quote_ts_us"), 0.0),
        _dex_opportunity_score(row),
    )


def _dex_contract_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """The exact reusable provider quote, with token fallback for fixtures."""

    token = str(row.get("token") or "").upper()
    chain, contract = _dex_chain_contract(row)
    return (token, chain, contract.casefold())


def _unique_rows_by_token(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        token = str(row.get("token") or "").upper()
        if not token or token in seen:
            continue
        selected.append(row)
        seen.add(token)
        if len(selected) >= limit:
            break
    return selected


def _has_permanent_mirage_guard(row: dict[str, Any]) -> bool:
    return any(
        str(item).startswith("mirage_guard:")
        and not str(item).startswith("mirage_guard:fast_")
        and str(item) != "mirage_guard:spot_sell_inventory_required"
        for item in row.get("blockers") or []
    )


def _cannot_lead_public_lane(
    row: dict[str, Any],
    *,
    rails: dict[str, dict[str, Any]] | None = None,
    metadata: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Cheap structural gates shared with the public lane admission rules.

    Exact books cannot turn two unverified tokenized instruments into the same
    legal asset, and cross-venue leveraged tokens cannot converge. Quoting
    those rows consumed scarce warm capacity and pushed verified leaders out.
    """

    token = str(row.get("token") or "").upper()
    if tokenized_assets.classify(row).get("status") == "blocked":
        return True
    if LEVERAGED_TOKEN_PATTERN.match(token) and row.get("long_venue") != row.get("short_venue"):
        return True
    entry = (metadata or {}).get(token) or {}
    volume = _number(entry.get("total_volume_usd"), 0.0)
    if volume and volume < 1_000.0:
        return True
    rail_map = rails or {}
    long_state = public_rails.rail_state(rail_map, row.get("long_venue"), token)
    short_state = public_rails.rail_state(rail_map, row.get("short_venue"), token)
    spread = max(
        abs(_number(row.get("executable_spread_pct"), 0.0)),
        abs(_number(row.get("depth_weighted_spread_pct"), 0.0)),
    )
    is_dex = "dex" in str(row.get("source_kind") or "").casefold()
    # High CEX dislocations require exact public contract evidence. Spending a
    # quote slot on a route the reader will necessarily guard reduced healthy
    # Futures and Spot coverage below 25 during ordinary provider failures.
    if (
        spread >= 5.0
        and not is_dex
        and (long_state or short_state)
        and not public_rails.exact_contract_match(long_state, short_state)
    ):
        return True
    if (
        str(row.get("long_market_type") or "") == "Spot"
        and str(row.get("short_market_type") or "") == "Spot"
    ):
        if long_state.get("withdraw") is False or short_state.get("deposit") is False:
            return True
        if public_rails.transfer_compatibility(long_state, short_state).get("status") == "incompatible":
            return True
    return False


def _native_order_book(
    venue: str,
    market_type: str,
    symbol: str,
) -> tuple[list[list[float]], list[list[float]]] | None:
    if market_type == "Spot":
        return _native_spot_order_book(venue, symbol)
    if market_type != "Futures" or venue not in NATIVE_FUTURES_VENUES:
        return None
    base, quote = _symbol_base_quote(symbol)
    compact = _native_linear_symbol(venue, base, quote)
    if venue == "Aster":
        url = (
            f"https://fapi.asterdex.com/fapi/v1/depth?{urlencode({'symbol': compact, 'limit': BOOK_DEPTH_LEVELS})}"
        )
    elif venue == "Binance":
        url = (
            f"https://fapi.binance.com/fapi/v1/depth?{urlencode({'symbol': compact, 'limit': BOOK_DEPTH_LEVELS})}"
        )
    elif venue == "Bingx":
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/depth?" + urlencode(
            {"symbol": f"{base}-{quote}", "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Bitget":
        url = "https://api.bitget.com/api/v2/mix/market/merge-depth?" + urlencode(
            {
                "symbol": compact,
                "productType": f"{quote}-FUTURES",
                "precision": "scale0",
                "limit": BOOK_DEPTH_LEVELS,
            }
        )
    elif venue == "Bybit":
        url = "https://api.bybit.com/v5/market/orderbook?" + urlencode(
            {"category": "linear", "symbol": compact, "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Gate":
        url = "https://api.gateio.ws/api/v4/futures/usdt/order_book?" + urlencode(
            {"contract": f"{base}_USDT", "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Kraken Futures":
        kraken_base = _kraken_asset_code(base)
        url = "https://futures.kraken.com/derivatives/api/v3/orderbook?" + urlencode(
            {"symbol": f"pf_{kraken_base.lower()}usd"}
        )
    elif venue == "Kucoin Futures":
        url = "https://api-futures.kucoin.com/api/v1/level2/depth20?" + urlencode(
            {"symbol": compact}
        )
    elif venue == "Mexc":
        url = f"https://contract.mexc.com/api/v1/contract/depth/{base}_{quote}"
    elif venue == "Ourbit":
        # A MEXC white-label: same path, same response shape, different host.
        url = f"https://futures.ourbit.com/api/v1/contract/depth/{base}_{quote}"
    elif venue == "HTX":
        url = "https://api.hbdm.com/linear-swap-ex/market/depth?" + urlencode(
            {"contract_code": f"{base}-{quote}", "depth": 20, "type": "step0"}
        )
    elif venue == "CoinEx":
        url = "https://api.coinex.com/v2/futures/depth?" + urlencode(
            {"market": compact, "limit": BOOK_DEPTH_LEVELS, "interval": "0"}
        )
    elif venue == "Phemex":
        url = "https://api.phemex.com/md/v2/orderbook?" + urlencode({"symbol": compact})
    elif venue == "WhiteBIT":
        url = f"https://whitebit.com/api/v4/public/orderbook/{base}_PERP?" + urlencode(
            {"limit": BOOK_DEPTH_LEVELS, "level": 2}
        )
    elif venue == "BitMart":
        url = "https://api-cloud-v2.bitmart.com/contract/public/depth?" + urlencode(
            {"symbol": compact}
        )
    elif venue == "XT":
        url = "https://fapi.xt.com/future/market/v1/public/q/depth?" + urlencode(
            {"symbol": f"{base.lower()}_{quote.lower()}", "level": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Coinbase International":
        url = f"https://api.international.coinbase.com/api/v1/instruments/{base}-PERP/quote"
    elif venue == "Hyperliquid":
        payload = _json_post(
            "https://api.hyperliquid.xyz/info",
            {"type": "l2Book", "coin": _hyperliquid_coin(base)},
        )
        levels = payload.get("levels") if isinstance(payload, dict) else []
        raw_bids = [
            [item.get("px"), item.get("sz")]
            for item in (levels[0] if isinstance(levels, list) and levels else [])
            if isinstance(item, dict)
        ]
        raw_asks = [
            [item.get("px"), item.get("sz")]
            for item in (levels[1] if isinstance(levels, list) and len(levels) > 1 else [])
            if isinstance(item, dict)
        ]
        return _sorted_book(raw_bids, raw_asks)
    else:
        url = "https://www.okx.com/api/v5/market/books?" + urlencode(
            {"instId": f"{base}-{quote}-SWAP", "sz": BOOK_DEPTH_LEVELS}
        )
    request = Request(url, headers={"User-Agent": "SpreadBoard/1.0"})
    with urlopen(request, timeout=8.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if venue == "Bybit":
        raw_bids = (payload.get("result") or {}).get("b")
        raw_asks = (payload.get("result") or {}).get("a")
    elif venue in {"Bingx", "Bitget"}:
        raw_bids = (payload.get("data") or {}).get("bids")
        raw_asks = (payload.get("data") or {}).get("asks")
    elif venue == "Gate":
        raw_bids = [[item.get("p"), item.get("s")] for item in payload.get("bids") or []]
        raw_asks = [[item.get("p"), item.get("s")] for item in payload.get("asks") or []]
    elif venue == "Kraken Futures":
        raw_bids = (payload.get("orderBook") or {}).get("bids")
        raw_asks = (payload.get("orderBook") or {}).get("asks")
    elif venue == "Kucoin Futures":
        raw_bids = (payload.get("data") or {}).get("bids")
        raw_asks = (payload.get("data") or {}).get("asks")
    elif venue in {"Mexc", "Ourbit"}:
        raw_bids = (payload.get("data") or {}).get("bids")
        raw_asks = (payload.get("data") or {}).get("asks")
    elif venue == "HTX":
        raw_bids = (payload.get("tick") or {}).get("bids")
        raw_asks = (payload.get("tick") or {}).get("asks")
    elif venue == "CoinEx":
        depth = (payload.get("data") or {}).get("depth") or {}
        raw_bids = depth.get("bids")
        raw_asks = depth.get("asks")
    elif venue == "Phemex":
        book = (payload.get("result") or {}).get("orderbook_p") or {}
        raw_bids = book.get("bids")
        raw_asks = book.get("asks")
    elif venue in {"WhiteBIT", "BitMart"}:
        data = payload.get("data") if venue == "BitMart" else payload
        raw_bids = (data or {}).get("bids")
        raw_asks = (data or {}).get("asks")
    elif venue == "XT":
        data = payload.get("result") or {}
        raw_bids = data.get("b")
        raw_asks = data.get("a")
    elif venue == "Coinbase International":
        raw_bids = [[payload.get("best_bid_price"), payload.get("best_bid_size")]]
        raw_asks = [[payload.get("best_ask_price"), payload.get("best_ask_size")]]
    elif venue == "OKX":
        books = payload.get("data") or []
        raw_bids = books[0].get("bids") if books else []
        raw_asks = books[0].get("asks") if books else []
    else:
        raw_bids = payload.get("bids")
        raw_asks = payload.get("asks")
    return _sorted_book(raw_bids, raw_asks)


#: How deep to ask for a public order book.
#:
#: Twenty levels is not enough to price the $50 probe on a thin contract: Gate
#: held $41.67 on the bid and $47.26 on the ask for BP across twenty levels and
#: the VWAP came back None, so the route could not be sampled and its chart
#: read "Stream sampler unavailable". Fifty levels of the same book held $107
#: and $233. Venues that cap lower return what they have.
BOOK_DEPTH_LEVELS = 50


def supports_native_order_book(venue: str, market_type: str) -> bool:
    if market_type == "Spot":
        return venue in NATIVE_SPOT_VENUES
    if market_type == "Futures":
        return venue in NATIVE_FUTURES_VENUES
    return False


def _native_spot_order_book(
    venue: str,
    symbol: str,
) -> tuple[list[list[float]], list[list[float]]] | None:
    if venue not in NATIVE_SPOT_VENUES:
        return None
    base, quote = _symbol_base_quote(symbol)
    compact = f"{base}{quote}"
    dashed = f"{base}-{quote}"
    if venue == "Binance":
        url = "https://api.binance.com/api/v3/depth?" + urlencode({"symbol": compact, "limit": BOOK_DEPTH_LEVELS})
    elif venue == "Bingx":
        url = "https://open-api.bingx.com/openApi/spot/v1/market/depth?" + urlencode(
            {"symbol": dashed, "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Bitget":
        url = "https://api.bitget.com/api/v2/spot/market/orderbook?" + urlencode(
            {"symbol": compact, "type": "step0", "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Bybit":
        url = "https://api.bybit.com/v5/market/orderbook?" + urlencode(
            {"category": "spot", "symbol": compact, "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Gate":
        url = "https://api.gateio.ws/api/v4/spot/order_book?" + urlencode(
            {"currency_pair": f"{base}_{quote}", "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Kucoin":
        url = f"https://api.kucoin.com/api/v1/market/orderbook/level2_20?symbol={dashed}"
    elif venue == "Mexc":
        url = "https://api.mexc.com/api/v3/depth?" + urlencode({"symbol": compact, "limit": BOOK_DEPTH_LEVELS})
    elif venue == "HTX":
        url = "https://api.huobi.pro/market/depth?" + urlencode(
            {"symbol": compact.lower(), "type": "step0", "depth": 20}
        )
    elif venue == "CoinEx":
        url = "https://api.coinex.com/v2/spot/depth?" + urlencode(
            {"market": compact, "limit": BOOK_DEPTH_LEVELS, "interval": "0"}
        )
    elif venue == "WhiteBIT":
        url = f"https://whitebit.com/api/v4/public/orderbook/{base}_{quote}?" + urlencode(
            {"limit": BOOK_DEPTH_LEVELS, "level": 2}
        )
    elif venue == "BitMart":
        url = "https://api-cloud.bitmart.com/spot/quotation/v3/books?" + urlencode(
            {"symbol": f"{base}_{quote}", "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "XT":
        url = "https://sapi.xt.com/v4/public/depth?" + urlencode(
            {"symbol": f"{base.lower()}_{quote.lower()}", "limit": BOOK_DEPTH_LEVELS}
        )
    elif venue == "Coinbase":
        url = f"https://api.exchange.coinbase.com/products/{base}-{quote}/book?" + urlencode(
            {"level": 2}
        )
    elif venue == "Kraken":
        url = "https://api.kraken.com/0/public/PreTrade?" + urlencode(
            {"symbol": f"{base}/{quote}"}
        )
    else:
        url = "https://www.okx.com/api/v5/market/books?" + urlencode({"instId": dashed, "sz": BOOK_DEPTH_LEVELS})
    payload = _json_url(url)
    if venue == "Bybit":
        raw_bids = (payload.get("result") or {}).get("b")
        raw_asks = (payload.get("result") or {}).get("a")
    elif venue in {"Bingx", "Bitget", "Kucoin"}:
        data = payload.get("data") or {}
        raw_bids = data.get("bids")
        raw_asks = data.get("asks")
    elif venue == "OKX":
        books = payload.get("data") or []
        raw_bids = books[0].get("bids") if books else []
        raw_asks = books[0].get("asks") if books else []
    elif venue == "HTX":
        raw_bids = (payload.get("tick") or {}).get("bids")
        raw_asks = (payload.get("tick") or {}).get("asks")
    elif venue == "CoinEx":
        depth = (payload.get("data") or {}).get("depth") or {}
        raw_bids = depth.get("bids")
        raw_asks = depth.get("asks")
    elif venue in {"BitMart", "XT"}:
        data = payload.get("data") if venue == "BitMart" else payload.get("result")
        raw_bids = (data or {}).get("bids")
        raw_asks = (data or {}).get("asks")
    elif venue == "Kraken":
        data = payload.get("result") or {}
        raw_bids = [[item.get("price"), item.get("qty")] for item in data.get("bids") or []]
        raw_asks = [[item.get("price"), item.get("qty")] for item in data.get("asks") or []]
    else:
        raw_bids = payload.get("bids")
        raw_asks = payload.get("asks")
    return _sorted_book(raw_bids, raw_asks)


def _native_current_funding(venue: str, symbol: str) -> dict[str, Any]:
    base, quote = _symbol_base_quote(symbol)
    compact = _native_linear_symbol(venue, base, quote)
    try:
        if venue in {"Aster", "Binance"}:
            host = "fapi.asterdex.com" if venue == "Aster" else "fapi.binance.com"
            payload = _json_url(
                f"https://{host}/fapi/v1/premiumIndex?" + urlencode({"symbol": compact})
            )
            info_payload = _json_url(
                f"https://{host}/fapi/v1/fundingInfo?" + urlencode({"symbol": compact})
            )
            info_rows = info_payload if isinstance(info_payload, list) else []
            funding_info = next(
                (
                    item
                    for item in info_rows
                    if isinstance(item, dict) and item.get("symbol") == compact
                ),
                {},
            )
            return _funding_fields(
                payload.get("lastFundingRate"),
                interval_hours=funding_info.get("fundingIntervalHours"),
                next_funding_ms=payload.get("nextFundingTime"),
            )
        if venue == "Bybit":
            payload = _json_url(
                "https://api.bybit.com/v5/market/tickers?"
                + urlencode({"category": "linear", "symbol": compact})
            )
            rows = (payload.get("result") or {}).get("list") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("fundingIntervalHour"),
                next_funding_ms=item.get("nextFundingTime"),
            )
        if venue == "OKX":
            payload = _json_url(
                "https://www.okx.com/api/v5/public/funding-rate?"
                + urlencode({"instId": f"{base}-{quote}-SWAP"})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            interval = _interval_hours(item.get("fundingTime"), item.get("nextFundingTime"))
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=interval,
                next_funding_ms=item.get("nextFundingTime"),
            )
        if venue == "Gate":
            payload = _json_url(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{base}_USDT")
            return _funding_fields(
                payload.get("funding_rate"),
                interval_hours=_seconds_to_hours(payload.get("funding_interval")),
                next_funding_seconds=payload.get("funding_next_apply"),
            )
        if venue == "Bitget":
            payload = _json_url(
                "https://api.bitget.com/api/v2/mix/market/current-fund-rate?"
                + urlencode({"symbol": compact, "productType": f"{quote}-FUTURES"})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("fundingRateInterval"),
                next_funding_ms=item.get("nextUpdate"),
            )
        if venue == "Bingx":
            payload = _json_url(
                "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?"
                + urlencode({"symbol": f"{base}-{quote}"})
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("lastFundingRate"),
                interval_hours=item.get("fundingIntervalHours"),
                next_funding_ms=item.get("nextFundingTime"),
            )
        if venue == "Kucoin Futures":
            payload = _json_url(
                f"https://api-futures.kucoin.com/api/v1/funding-rate/{compact}/current"
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("value"),
                interval_hours=_milliseconds_to_hours(item.get("granularity")),
                next_funding_ms=item.get("fundingTime"),
            )
        if venue == "Mexc":
            payload = _json_url(
                f"https://contract.mexc.com/api/v1/contract/funding_rate/{base}_{quote}"
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("collectCycle"),
                next_funding_ms=item.get("nextSettleTime"),
            )
        if venue == "Hyperliquid":
            payload = _json_post(
                "https://api.hyperliquid.xyz/info",
                {"type": "metaAndAssetCtxs"},
            )
            if not isinstance(payload, list) or len(payload) < 2:
                return {}
            meta = payload[0] if isinstance(payload[0], dict) else {}
            contexts = payload[1] if isinstance(payload[1], list) else []
            universe = meta.get("universe") if isinstance(meta.get("universe"), list) else []
            hyperliquid_coin = _hyperliquid_coin(base).upper()
            index = next(
                (
                    index
                    for index, item in enumerate(universe)
                    if isinstance(item, dict)
                    and (
                        str(item.get("name") or "").upper() == hyperliquid_coin
                    )
                ),
                None,
            )
            if index is None or index >= len(contexts) or not isinstance(contexts[index], dict):
                return {}
            next_hour_ms = int((time.time() // 3600 + 1) * 3600 * 1000)
            return _funding_fields(
                contexts[index].get("funding"),
                interval_hours=1,
                next_funding_ms=next_hour_ms,
            )
        if venue == "HTX":
            payload = _json_url(
                "https://api.hbdm.com/v5/market/funding_rate?"
                + urlencode({"contract_code": f"{base}-{quote}"})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("funding_rate"),
                interval_hours=_interval_hours(
                    item.get("funding_time"), item.get("next_funding_time")
                ),
                next_funding_ms=item.get("next_funding_time"),
            )
        if venue == "Phemex":
            payload = _json_url(
                "https://api.phemex.com/md/v2/ticker/24hr?" + urlencode({"symbol": compact})
            )
            item = payload.get("result") or {}
            return _funding_fields(item.get("fundingRateRr"), interval_hours=8)
        if venue == "CoinEx":
            payload = _json_url(
                "https://api.coinex.com/v2/futures/funding-rate?" + urlencode({"market": compact})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("next_funding_rate") or item.get("latest_funding_rate"),
                interval_hours=_interval_hours(
                    item.get("latest_funding_time"), item.get("next_funding_time")
                ),
                next_funding_ms=item.get("next_funding_time"),
            )
        if venue == "BitMart":
            payload = _json_url(
                "https://api-cloud-v2.bitmart.com/contract/public/funding-rate?"
                + urlencode({"symbol": compact})
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("rate_value") or item.get("expected_rate"),
                interval_hours=8,
                next_funding_ms=item.get("funding_time"),
            )
        if venue == "XT":
            payload = _json_url(
                "https://fapi.xt.com/future/market/v1/public/q/funding-rate?"
                + urlencode({"symbol": f"{base.lower()}_{quote.lower()}"})
            )
            item = payload.get("result") or {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("collectionInternal"),
                next_funding_ms=item.get("nextCollectionTime"),
            )
        if venue == "WhiteBIT":
            payload = _json_url("https://whitebit.com/api/v4/public/futures")
            rows = payload.get("result") if isinstance(payload, dict) else payload
            item = next(
                (
                    row
                    for row in rows or []
                    if isinstance(row, dict)
                    and str(row.get("ticker_id") or row.get("name") or "").upper()
                    in {f"{base}_PERP", f"{base}_{quote}"}
                ),
                {},
            )
            return _funding_fields(
                item.get("funding_rate"),
                interval_hours=(
                    _optional_number(item.get("funding_interval_minutes")) / 60.0
                    if _optional_number(item.get("funding_interval_minutes"))
                    else _seconds_to_hours(item.get("funding_interval"))
                ),
                next_funding_ms=item.get("next_funding_rate_timestamp"),
                next_funding_seconds=item.get("funding_next_apply"),
            )
        if venue == "Coinbase International":
            payload = _json_url(
                f"https://api.international.coinbase.com/api/v1/instruments/{base}-PERP/quote"
            )
            return _funding_fields(payload.get("predicted_funding"), interval_hours=1)
        if venue == "Kraken Futures":
            kraken_symbol = f"PF_{_kraken_asset_code(base)}USD"
            payload = _json_url("https://futures.kraken.com/derivatives/api/v3/tickers")
            item = next(
                (
                    row
                    for row in payload.get("tickers") or []
                    if isinstance(row, dict)
                    and str(row.get("symbol") or "").upper() == kraken_symbol
                ),
                {},
            )
            funding_velocity = _optional_number(item.get("fundingRate"))
            index_price = _optional_number(item.get("indexPrice"))
            if funding_velocity is None or index_price is None or index_price <= 0:
                return {}
            next_hour_ms = int((time.time() // 3600 + 1) * 3600 * 1000)
            return _funding_fields(
                funding_velocity / index_price,
                interval_hours=1,
                next_funding_ms=next_hour_ms,
            )
    except Exception:
        return {}
    return {}


def _milliseconds_to_hours(value: Any) -> float | None:
    parsed = _optional_number(value)
    return parsed / 3_600_000.0 if parsed is not None and parsed > 0 else None


def _ccxt_current_funding(
    client: Any,
    symbol: str,
    *,
    venue: str | None = None,
) -> dict[str, Any]:
    try:
        if not getattr(client, "has", {}).get("fetchFundingRate"):
            return {}
        payload = client.fetch_funding_rate(symbol) or {}
        interval = payload.get("interval")
        if isinstance(interval, str) and interval.casefold().endswith("h"):
            interval = interval[:-1]
        return _funding_fields(
            payload.get("fundingRate"),
            interval_hours=interval,
            next_funding_ms=payload.get("fundingTimestamp"),
        )
    except Exception:
        return {}


def _funding_fields(
    rate: Any,
    *,
    interval_hours: Any = None,
    next_funding_ms: Any = None,
    next_funding_seconds: Any = None,
) -> dict[str, Any]:
    parsed = _optional_number(rate)
    if parsed is None:
        return {}
    interval = _optional_number(interval_hours)
    next_ms = _optional_number(next_funding_ms)
    if next_ms is None:
        next_seconds = _optional_number(next_funding_seconds)
        next_ms = next_seconds * 1000 if next_seconds is not None else None
    output = {"current_funding_pct": parsed * 100.0}
    if interval is not None:
        output["funding_interval_hours"] = interval
    if next_ms is not None:
        output["next_funding_ts_us"] = int(next_ms * 1000)
    return output


def _json_url(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"})
    with urlopen(request, timeout=6.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, (dict, list)) else {}


def _json_post(url: str, payload: dict[str, Any]) -> Any:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SpreadBoard/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=6.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _symbol_base_quote(symbol: str) -> tuple[str, str]:
    base, _, quote = str(symbol).partition("/")
    return base.upper(), (quote.split(":", 1)[0] or "USDT").upper()


def _hyperliquid_coin(base: str) -> str:
    normalized = str(base).upper()
    if normalized.startswith("XYZ-"):
        return f"xyz:{normalized.removeprefix('XYZ-')}"
    return normalized


def _relative_value_multipliers(row: dict[str, Any]) -> tuple[float, float]:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    value = notes.get("relative_value") if isinstance(notes.get("relative_value"), dict) else {}
    return (
        max(0.000001, _number(value.get("long_multiplier"), 1.0)),
        max(0.000001, _number(value.get("short_multiplier"), 1.0)),
    )


def _kraken_asset_code(base: str) -> str:
    return {"BTC": "XBT", "DOGE": "XDG"}.get(base.upper(), base.upper())


def _native_linear_symbol(venue: str, base: str, quote: str) -> str:
    if quote.upper() == "USDC" and venue in {"Bitget", "Bybit"}:
        return f"{base.upper()}PERP"
    if venue == "Kucoin Futures":
        return f"{_kraken_asset_code(base)}{quote.upper()}M"
    return f"{base.upper()}{quote.upper()}"


def _sorted_book(
    raw_bids: Any,
    raw_asks: Any,
) -> tuple[list[list[float]], list[list[float]]]:
    bids = sorted(_levels(raw_bids), key=lambda level: level[0], reverse=True)
    asks = sorted(_levels(raw_asks), key=lambda level: level[0])
    return bids, asks


def _okx_dex_leg_quote(
    row: dict[str, Any],
    side: str,
    *,
    target_notional_usd: float,
    quote_both: bool = True,
) -> dict[str, Any] | None:
    chain, contract = _dex_chain_contract(row, side=side)
    if not chain or not contract:
        return None
    try:
        from spreadarb.dex import okx_quotes

        buy: dict[str, Any] | None = None
        sell: dict[str, Any] | None = None
        quantity: Decimal | None = None
        decimals: int | None = None

        if side == "long" or quote_both:
            buy = okx_quotes.quote_usdc_to_token(
                chain=chain,
                token_address=contract,
                notional_usd=Decimal(str(target_notional_usd)),
            )
            quantity = Decimal(str(buy.get("out_qty") or "0"))
            decimals = _optional_int(buy.get("to_token_decimals"))
            if buy.get("status") != "ok" or quantity <= 0 or decimals is None:
                return None

        if side == "short" or quote_both:
            if quantity is None or decimals is None:
                quantity, decimals = _dex_directional_sell_inputs(
                    row,
                    side=side,
                    target_notional_usd=target_notional_usd,
                )
            # An old or incomplete discovery row may lack decimals. Preserve
            # the two-sided fallback for that exceptional row; ordinary fast
            # rotations carry exact identity decimals and therefore need only
            # one provider request per opening direction.
            if quantity is None or quantity <= 0 or decimals is None:
                buy = okx_quotes.quote_usdc_to_token(
                    chain=chain,
                    token_address=contract,
                    notional_usd=Decimal(str(target_notional_usd)),
                )
                quantity = Decimal(str(buy.get("out_qty") or "0"))
                decimals = _optional_int(buy.get("to_token_decimals"))
                if buy.get("status") != "ok" or quantity <= 0 or decimals is None:
                    return None
            sell = okx_quotes.quote_token_to_usdc(
                chain=chain,
                token_address=contract,
                token_quantity=quantity,
                token_decimals=decimals,
            )
            if sell.get("status") != "ok":
                return None

        bid = _optional_number((sell or {}).get("dex_sell_price_usd"))
        ask = _optional_number((buy or {}).get("dex_buy_price_usd"))
        if (side == "long" and ask is None) or (side == "short" and bid is None):
            return None
        result: dict[str, Any] = {
            "symbol": str(row.get("token") or ""),
            "contract_size": 1.0,
            "quote_ts_us": int(time.time() * 1_000_000),
            "chain_id": chain,
            "token_address": contract,
            "sample_side": side,
        }
        if bid is not None:
            result.update({"bid": bid, "bid_vwap": bid})
        if ask is not None:
            result.update({"ask": ask, "ask_vwap": ask})
        return result
    except Exception:
        return None


def _dex_directional_sell_inputs(
    row: dict[str, Any],
    *,
    side: str,
    target_notional_usd: float,
) -> tuple[Decimal | None, int | None]:
    """Recover the exact token amount for a one-request DEX sell probe."""

    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    identities = notes.get("identity") if isinstance(notes.get("identity"), dict) else {}
    identity = identities.get(side) if isinstance(identities.get(side), dict) else {}
    decimals = _optional_int(identity.get("decimals"))
    route_inputs = (
        notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    )
    leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
    if decimals is None:
        decimals = _optional_int(leg.get("to_token_decimals") or leg.get("token_decimals"))
    reference = _optional_number(leg.get("bid") or leg.get("ask"))
    if reference is None or reference <= 0:
        return None, decimals
    return Decimal(str(target_notional_usd)) / Decimal(str(reference)), decimals


def _dex_chain_contract(
    row: dict[str, Any],
    *,
    side: str | None = None,
) -> tuple[str, str]:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    identities = notes.get("identity") if isinstance(notes.get("identity"), dict) else {}
    sides = (side,) if side in {"long", "short"} else ("long", "short")
    for candidate_side in sides:
        venue = str(row.get(f"{candidate_side}_venue") or "")
        identity = (
            identities.get(candidate_side)
            if isinstance(identities.get(candidate_side), dict)
            else {}
        )
        if "okx dex" not in venue.casefold():
            continue
        nested_chain = str(identity.get("chain_id") or "").strip()
        nested_contract = str(identity.get("token_address") or "").strip()
        if nested_chain and nested_contract:
            return nested_chain, nested_contract
    chain = str(row.get("dex_chain") or "").strip()
    contract = str(row.get("dex_contract") or "").strip()
    if chain and contract:
        return chain, contract
    return "", ""


def _interval_hours(current_ms: Any, next_ms: Any) -> float | None:
    current = _optional_number(current_ms)
    upcoming = _optional_number(next_ms)
    if current is None or upcoming is None or upcoming <= current:
        return None
    return (upcoming - current) / 3_600_000.0


def _seconds_to_hours(value: Any) -> float | None:
    parsed = _optional_number(value)
    return parsed / 3600.0 if parsed is not None else None


def _optional_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".fast.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
