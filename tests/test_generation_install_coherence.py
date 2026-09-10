"""Installing structure must preserve the latest complete price observation."""
import time

import pytest

from spreadboard import warm_query_projection as projection


@pytest.mark.parametrize('newer', ['structural', 'previous'])
def test_newest_update_preserves_all_exact_price_evidence(newer):
    old = (1., None, 100, 'top_book', 10., 10.1, 1., 10.05, 10.04)
    new = (2., .5, 200, 'matched_vwap', 20., 20.4, 2., 20.1, 20.2)
    previous, structural = (old, new) if newer == 'structural' else (new, old)
    result = projection._newest_route_update(previous, structural)
    assert result == (new[0], previous[1], *new[2:])


def test_install_keeps_a_refresh_published_during_structural_preparation(monkeypatch):
    now_us = int(time.time() * 1_000_000)
    universe = projection.LiveRouteUniverse()
    stale = (1., .1, now_us-100_000_000, 'top_book', 10., 10.1, 1.)
    fresh = (2., None, now_us, 'top_book', 10., 10.2, 2., 10.1, 10.15)
    universe._updates = {'A': stale}
    universe._rows = {'A': {'token':'X', 'route_kind':'FUTURES'}}
    monkeypatch.setattr(projection.api_spreads, 'live_route_updates_for', lambda *a, **kw: {'A': fresh})

    def structural(row):
        # A price refresh publishes by replacing this immutable map while the
        # installer is outside its reader lock preparing structural seeds.
        universe.refresh_route_kinds({'FUTURES'})
        return stale

    monkeypatch.setattr(projection, '_structural_route_update', structural)
    universe.install({'A': {'token':'X', 'route_kind':'FUTURES'}})
    updates, status = universe.update_snapshot()
    assert status['current_priced_route_count'] == 1
    assert updates['A'] == fresh
