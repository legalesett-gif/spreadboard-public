"""A member must never meet the "warming" board on a lane that was warmed.

`api_market_spreads` only returns `_market_warming_payload()` -- the state the
UI renders as "Spread refreshing" -- when it has no previous copy of that view
to fall back on. Everything else is served instantly from the stale cache while
a rebuild runs behind the request.

So the whole question is whether a warmed view still HAS its stale copy when
the next member arrives. Two settings decide that, and both were wrong:

* stale entries were dropped after 1800s while warms were observed an hour
  apart, so for roughly half of every hour every lane had nothing to fall back
  on;
* the cache held 14 entries against more warmed views than that, so the ones
  that did survive evicted each other.

These tests pin the relationship rather than the numbers: retention has to
outlive the warm cadence, and the cache has to hold everything that is warmed.
"""

from __future__ import annotations

import re
from pathlib import Path

from spreadboard import server

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts/run_spreadboard_service.py"


def _warm_query_count() -> int:
    source = SERVICE.read_text(encoding="utf-8")
    match = re.search(r"WARM_QUERIES:[^=]*=\s*\((.*?)\n\)\n", source, re.DOTALL)
    assert match is not None, "WARM_QUERIES not found"
    body = match.group(1)
    # Each warmed view is one dict literal at the top level of the tuple.
    return len(re.findall(r"^\s*\{", body, re.MULTILINE))


#: What actually triggers a warm in the web process. `SharedArtifactWatcher`
#: requests one when the discovery snapshot changes -- there is no periodic
#: timer -- and that snapshot was observed republishing about once an hour
#: (warms logged at 15:57, 16:56, 17:58). The configured WARM_INTERVAL_SECONDS
#: only rate-limits the non-forced path, so it is not the cadence that matters.
OBSERVED_WARM_CADENCE_SECONDS = 3600.0


def test_a_stale_copy_outlives_the_gap_between_warms() -> None:
    """Otherwise the fallback is gone exactly when it is needed.

    Warms were measured an hour apart in production while retention was 1800s.
    """
    assert server._MARKET_STALE_MAX_SECONDS >= 2 * OBSERVED_WARM_CADENCE_SECONDS


def test_the_cache_holds_every_view_that_gets_warmed() -> None:
    """Warming more views than the cache can hold just evicts the earlier ones."""
    warmed = _warm_query_count()
    assert warmed > 0

    # Plus the free board and the member default, which are not in WARM_QUERIES
    # but are the two most requested views on the site.
    assert server._MARKET_CACHE_MAX_ENTRIES >= warmed + 2


def test_the_fresh_window_is_not_shorter_than_the_quote_cadence() -> None:
    """Quotes republish every few minutes and re-key every entry.

    A fresh window shorter than that means the fresh cache is empty almost
    always, which is what left it holding one or two entries while eleven sat
    in stale.
    """
    assert server._MARKET_CACHE_TTL_SECONDS >= 300.0
