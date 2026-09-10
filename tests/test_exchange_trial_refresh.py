import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from spreadarb.venue_policy import exchange_filter_matches, opportunity_venue_enabled

from spreadboard import accounts, funding_catalog, server, warm_query_projection


def test_exclusions_match_either_leg_and_do_not_change_other_venues():
    assert not opportunity_venue_enabled(' HTX ')
    assert opportunity_venue_enabled('Hyperliquid')
    for a, b in [('WhiteBIT', 'Hyperliquid'), ('Hyperliquid', 'WhiteBIT')]:
        assert not exchange_filter_matches(a, b, '!whitebit,!Mexc')
    assert exchange_filter_matches('Bybit', 'Hyperliquid', 'Hyperliquid,!WhiteBIT')
    assert not exchange_filter_matches('Gate', 'Bybit', 'Hyperliquid,!WhiteBIT')


def test_funding_and_warm_projection_apply_exclusion_before_ranking():
    row = {'token': 'BTC', 'route_kind': 'FUTURES', 'long_venue': 'Hyperliquid', 'short_venue': 'WhiteBIT'}
    assert not funding_catalog._common_eligible(row, route_kind=None, symbol=None, exchange='!WhiteBIT', quote=None)
    assert not warm_query_projection._matches_structural(row, {'exchange': '!WhiteBIT'})
    row['short_venue'] = 'Bybit'
    assert funding_catalog._common_eligible(row, route_kind=None, symbol=None, exchange='!WhiteBIT', quote=None)
    assert warm_query_projection._matches_structural(row, {'exchange': '!WhiteBIT'})


def test_multiple_exchange_controls_survive_links_and_checked_rendering():
    q = {'exchange': ['', '!WhiteBIT', '!Mexc'], 'rank': ['7d']}
    assert server._query_first(q, 'exchange') == '!WhiteBIT,!Mexc'
    assert server._query_with(q, offset=25)['exchange'] == '!WhiteBIT,!Mexc'
    html = server.render_exchange_filter(q, ['HTX', 'WhiteBIT', 'Mexc', 'Hyperliquid'])
    assert 'value="!HTX"' not in html
    assert 'value="!WhiteBIT" checked' in html
    assert 'value="!Hyperliquid"' in html


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv('SPREADBOARD_ADMIN_EMAIL', raising=False)
    monkeypatch.delenv('SPREADBOARD_ADMIN_PASSWORD', raising=False)
    p = tmp_path / 'accounts.sqlite3'
    accounts.initialize(p)
    return p


def register(db, email):
    return accounts.register_user(email=email, display_name='Trial', password='long-test-password-123', db_path=db)[0]


def link(db, user, chat):
    token = accounts.create_telegram_link_token(user.id, db_path=db)
    return accounts.bind_telegram_chat(token, chat, db_path=db)


def test_trial_requires_link_and_expires_in_seven_days_without_reset(db):
    user = register(db, 'trial@example.com')
    assert not user.subscription_active
    start = datetime.now(UTC)
    user = link(db, user, 90001)
    assert user.has_tier('research_pro')
    assert user.subscription_status == 'trialing'
    expiry = datetime.fromisoformat(user.subscription_expires_at)
    assert timedelta(days=7) <= expiry-start <= timedelta(days=7, seconds=3)
    accounts.unlink_telegram_chat(user.id, db_path=db)
    assert link(db, user, 90001).subscription_expires_at == user.subscription_expires_at
    with sqlite3.connect(db) as con:
        con.execute('UPDATE users SET subscription_expires_at=? WHERE id=?', ((start-timedelta(seconds=1)).isoformat(), user.id))
    assert not accounts.get_user_object(user.id, db_path=db).subscription_active


def test_unlink_and_new_account_cannot_claim_again(db):
    one = link(db, register(db, 'one@example.com'), 90001)
    accounts.unlink_telegram_chat(one.id, db_path=db)
    two = link(db, register(db, 'two@example.com'), 90001)
    assert not two.subscription_active


def test_deleted_account_and_email_alias_cannot_restart_trial(db):
    one = link(db, register(db, 'first.last+one@gmail.com'), 90001)
    with sqlite3.connect(db) as con:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('DELETE FROM users WHERE id=?', (one.id,))
    two = link(db, register(db, 'firstlast+two@googlemail.com'), 90002)
    assert not two.subscription_active


def test_concurrent_claim_has_only_one_winner(db):
    users = [register(db, f'concurrent{i}@example.com') for i in range(2)]
    def attempt(user):
        try:
            return link(db, user, 90003).subscription_active
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, users)) == 1


def test_paid_access_is_not_replaced_by_trial(db):
    user = register(db, 'paid@example.com')
    until = (datetime.now(UTC)+timedelta(days=90)).isoformat()
    with sqlite3.connect(db) as con:
        con.execute("UPDATE users SET subscription_status='active', subscription_expires_at=?, subscription_tier='scanner' WHERE id=?", (until,user.id))
    after = link(db, user, 90004)
    assert after.subscription_status == 'active'
    assert after.subscription_expires_at == until
    assert after.subscription_tier == 'scanner'


def test_demand_tail_keeps_a_rotating_priority_turn(tmp_path, monkeypatch):
    from scripts import run_spreadboard_service as service
    from spreadboard import chart_catalog, funding_history_demand
    from spreadboard import venue_funding_history as history
    legs = [('Kraken Futures', 'S/USD:USD'), ('Bybit', 'ICX/USDT:USDT')]
    summary = history.coverage_summary([], cache_path=tmp_path/'missing.json')
    monkeypatch.setattr(chart_catalog, 'load', lambda: {'markets': []})
    monkeypatch.setattr(history, 'coverage_summary', lambda _: summary)
    monkeypatch.setattr(funding_history_demand, 'legs', lambda: legs)
    monkeypatch.setattr(accounts, 'all_open_position_futures_legs', lambda **_: [])
    monkeypatch.setattr(accounts, 'all_watchlist_symbols', lambda **_: [])
    monkeypatch.setattr(service, '_LAST_VENUE_HISTORY_PRIORITY_AT', 0)
    monkeypatch.setattr(service, '_LAST_VENUE_HISTORY_CATALOG_AT', 0)
    calls = []
    def build(_legs, **kwargs):
        calls.append(kwargs)
        return {}
    monkeypatch.setattr(history, 'build', build)
    service._refresh_venue_funding_history(extra_priority_legs=legs)
    assert calls[0]['priority_recency_order']
    assert calls[0]['budget_seconds'] == 30
    rotating = [call for call in calls if call['priority_only'] and not call.get('priority_recency_order')]
    assert len(rotating) == 1
    assert rotating[0]['priority_legs'] == legs
    assert rotating[0]['budget_seconds'] == 90


@pytest.mark.parametrize('venue', ['CoinEx','Phemex'])
def test_funding_retirement_preserves_price_coverage_and_rejects_old_payload(venue):
    from spreadarb.venue_policy import funding_venue_enabled, opportunity_payload_enabled

    from spreadboard import bulk_quotes
    row={'token':'ONE','route_kind':'FUTURES','long_venue':'Hyperliquid','short_venue':venue}
    assert opportunity_venue_enabled(venue)
    assert not funding_venue_enabled(venue)
    assert not bulk_quotes._funding_key_enabled(venue+'|ONE/USDT:USDT')
    assert not funding_catalog._common_eligible(row,route_kind=None,symbol=None,exchange=None,quote=None)
    assert opportunity_payload_enabled({'groups':[{'routes':[row]}]})
    assert not opportunity_payload_enabled({'groups':[{'routes':[row]}]}, funding_only=True)


@pytest.mark.parametrize('venue', ['CoinEx','Phemex','HTX','Ourbit'])
def test_retired_funding_venue_never_starts_public_network_parser(monkeypatch, venue):
    from spreadboard.fast_quotes import FastQuoteRefresher
    refresher=FastQuoteRefresher()
    def forbidden(*args):
        raise AssertionError('Retired funding parser was invoked')
    monkeypatch.setattr(refresher,'_bulk_funding_rates_raw',forbidden)
    assert refresher._bulk_funding_rates(venue) == {}
