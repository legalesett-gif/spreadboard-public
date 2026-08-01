# Graph Report - spreadboard-public-release-clean  (2026-08-01)

## Corpus Check
- 70 files · ~157,261 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2958 nodes · 7225 edges · 47 communities detected
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 254 edges (avg confidence: 0.71)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]

## God Nodes (most connected - your core abstractions)
1. `h()` - 129 edges
2. `ii()` - 80 edges
3. `yi` - 69 edges
4. `a()` - 61 edges
5. `vn()` - 54 edges
6. `ci` - 53 edges
7. `_float_or_none()` - 49 edges
8. `ht` - 48 edges
9. `wn` - 46 edges
10. `fmt_signed_pct()` - 43 edges

## Surprising Connections (you probably didn't know these)
- `test_dex_contract_guard_rejects_contract_from_another_token()` --calls--> `WatchAsset`  [INFERRED]
  tests/test_release_audit.py → src/spreadarb/api_discovery/identity.py
- `test_okx_dex_uses_usd_network_fee_not_raw_gas_units()` --calls--> `WatchAsset`  [INFERRED]
  tests/test_release_audit.py → src/spreadarb/api_discovery/identity.py
- `test_okx_dynamic_catalogue_keeps_only_unique_symbol_contracts()` --calls--> `MarketQuote`  [INFERRED]
  tests/test_release_audit.py → src/spreadarb/api_discovery/models.py
- `test_okx_dynamic_catalogue_prioritizes_funding_before_volume()` --calls--> `MarketQuote`  [INFERRED]
  tests/test_release_audit.py → src/spreadarb/api_discovery/models.py
- `client()` --calls--> `SpreadBoardServer`  [INFERRED]
  tests/test_crypto_billing_http.py → spreadboard/server.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.01
Nodes (76): a(), ae, ai, At, b(), bi(), bs(), Bt (+68 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (29): _(), an(), as(), bn(), ct, En, fi, g() (+21 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (320): active_class(), api_alert_context(), api_alert_preview(), api_board(), api_community(), api_funding_watch(), api_health(), api_history() (+312 more)

### Community 3 - "Community 3"
Cohesion: 0.02
Nodes (220): _checks_from_payload(), empty(), ExecutorAttestation, ExecutorAttestationRegistry, load_executor_attestations(), Read-only executor/preflight attestations for API discovery rows., route_key(), BlacklistFilterResult (+212 more)

### Community 4 - "Community 4"
Cohesion: 0.03
Nodes (11): ar(), ds(), gs(), hs(), ii(), pt, q(), r() (+3 more)

### Community 5 - "Community 5"
Cohesion: 0.04
Nodes (89): depth_weighted_price(), Small read-only order-book math helpers., main(), main(), _atomic_write(), _ccxt_current_funding(), _dex_chain_contract(), _expanded_token_rows() (+81 more)

### Community 6 - "Community 6"
Cohesion: 0.04
Nodes (91): _age_min(), _bool_or_none(), _dedupe_rows(), _depth_from_api(), _dex_contract_mirage_reasons(), _dex_spot_source_status(), _effective_funding_24h(), _effective_funding_24h_dict() (+83 more)

### Community 7 - "Community 7"
Cohesion: 0.03
Nodes (51): Every lane that carries data must be reachable in the UI.      Spot-DEX had rows, Every lane that carries data must be reachable in the UI.      Spot-DEX had rows, Ourbit has no ccxt adapter; it is an MEXC white-label we retarget., A retarget bug would silently quote MEXC prices under the Ourbit name., The repo data/ dir is read-only in the container.      Enabling broad DEX-spot d, Discovery rows must survive between scans or they are never shown.      High-spr, Futures legs settle in margin and a DEX leg sits in your own wallet., SIREN sat at ~100% DEX->Kucoin on an identical contract purely because     Kucoi (+43 more)

### Community 8 - "Community 8"
Cohesion: 0.03
Nodes (8): es(), is(), se, ss, tt, we, xn(), y

### Community 9 - "Community 9"
Cohesion: 0.06
Nodes (80): _action_badges(), _action_next(), _action_priority(), _action_reason(), _action_status(), _age_min(), _alert_example_with_freshness(), _alert_freshness() (+72 more)

### Community 10 - "Community 10"
Cohesion: 0.05
Nodes (40): BaseHTTPRequestHandler, _atomic_write_snapshot(), _env_bool(), _funding_lane(), _funding_refresh_route_keys(), _log(), main(), _merge_newer_fast_quotes() (+32 more)

### Community 11 - "Community 11"
Cohesion: 0.04
Nodes (67): _allocate_amount(), config(), create_invoice(), CryptoBillingError, CryptoConfig, format_amount(), get_invoice(), _invoice_dict() (+59 more)

### Community 12 - "Community 12"
Cohesion: 0.04
Nodes (7): ft, ht, ir, lr, tn(), Wt(), yt

### Community 13 - "Community 13"
Cohesion: 0.07
Nodes (65): add_alert_rule(), add_funding_cashflow(), add_market_alert_rule(), apply_billing_event(), _assert_customer_owner(), _billing_user_id(), bind_telegram_chat(), _bootstrap_admin() (+57 more)

### Community 14 - "Community 14"
Cohesion: 0.04
Nodes (16): e(), fs(), js(), ms(), mt, nt, qs(), re (+8 more)

### Community 15 - "Community 15"
Cohesion: 0.06
Nodes (57): chain_index(), _decimal_or_none(), erc20_decimals(), _eth_call(), _http_get(), _is_rate_limited(), list_tokens(), load_okx_dex_credentials() (+49 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (64): best_spreads(), _bool_or_none(), _build_route_detail(), _build_token_data(), _ccxt_exchange_class(), convergence_hint(), _deposit_withdraw_status(), _display_venue() (+56 more)

### Community 17 - "Community 17"
Cohesion: 0.07
Nodes (9): cn(), dn(), fn(), j, on, rn(), u(), un() (+1 more)

### Community 18 - "Community 18"
Cohesion: 0.05
Nodes (31): test_settled_funding_propagates_to_every_route_using_same_leg(), board_event(), board_file(), message(), Token lookups in the subscriber Telegram group., Whatever a member types becomes a bounded, inert token string., Whatever a member types becomes a bounded, inert token string., Whatever a member types becomes a bounded, inert token string. (+23 more)

### Community 19 - "Community 19"
Cohesion: 0.1
Nodes (34): _age_min(), _apply_filters(), BoardRow, BoardSnapshot, _bool_or_none(), build_source_health(), _chart_url(), _depth_usd() (+26 more)

### Community 20 - "Community 20"
Cohesion: 0.11
Nodes (30): make_user(), pay(), Crypto (Arbitrum USDC/USDT) prepaid billing.  The invariant that matters most: a, An exchange withdrawal fee must not cost the member their access., A token calling itself USDC must not buy access., Renewing with time left must not forfeit the days already paid for., Dollars -> raw 6-decimal token units., test_admin_can_settle_a_parked_payment() (+22 more)

### Community 21 - "Community 21"
Cohesion: 0.1
Nodes (28): _api_call(), config(), _configure_group(), _create_join_request_link(), _handle_callback(), _handle_group_query(), _handle_inline_query(), _handle_join_request() (+20 more)

### Community 22 - "Community 22"
Cohesion: 0.12
Nodes (15): AlertWatcher, config_flags(), _float_or_default(), _json_or_text(), _public_detail(), _pushover_users(), Disabled-by-default Pushover alerts for SpreadBoard., Poll the board and alert when a symbol newly crosses the configured threshold. (+7 more)

### Community 23 - "Community 23"
Cohesion: 0.14
Nodes (13): BookWorker, _desired_legs(), _leg_key(), _levels(), main(), _number(), _websocket_depth_limit(), cache_key() (+5 more)

### Community 24 - "Community 24"
Cohesion: 0.19
Nodes (14): _any_network_state(), _atomic_write(), _bool_or_none(), _fetch_venue_rails(), _load_payload(), _normalize_network(), _payload_is_fresh(), Public deposit/withdraw rail metadata for spot legs. (+6 more)

### Community 25 - "Community 25"
Cohesion: 0.26
Nodes (16): _connect(), _ensure_columns(), _exit_spread_pct(), _float_or_none(), _int_or_none(), _is_contaminated_dex_sample(), load_history(), Compact persistent history for canonical public-API spread routes. (+8 more)

### Community 26 - "Community 26"
Cohesion: 0.21
Nodes (14): _align(), _atomic_json(), _cache_path(), evenly_sample(), _fetch_leg(), load_or_fetch(), Indicative long-window spread history built from aligned public OHLCV closes., Return full-window indicative history without presenting candles as books. (+6 more)

### Community 27 - "Community 27"
Cohesion: 0.32
Nodes (11): fake_rpc(), make_user(), Arbitrum log watcher. No network access -- the RPC transport is injected., test_cursor_advances_and_prevents_reprocessing(), test_filter_is_scoped_to_allowlisted_tokens_and_receiver(), test_impostor_token_in_logs_is_ignored(), test_one_malformed_log_does_not_stall_the_cursor(), test_only_scans_confirmed_blocks() (+3 more)

### Community 29 - "Community 29"
Cohesion: 0.18
Nodes (2): di(), o

### Community 30 - "Community 30"
Cohesion: 0.35
Nodes (7): _event(), _member(), test_customer_cannot_be_reassigned(), test_payment_failed_and_deleted_revoke_access(), test_subscription_event_is_idempotent(), test_unrelated_invoice_cannot_activate_membership(), test_webhook_signature_and_expiry()

### Community 31 - "Community 31"
Cohesion: 0.18
Nodes (11): Badge rows whose route feasibility or identity is unproven.      These rows used, Badge rows whose route feasibility or identity is unproven.      These rows used, Badge rows whose route feasibility or identity is unproven.      These rows used, Badge rows whose route feasibility or identity is unproven.      These rows used, Badge rows whose route feasibility or identity is unproven.      These rows used, Badge rows whose route feasibility or identity is unproven.      These rows used, Badge rows whose route feasibility or identity is unproven.      These rows used, Badge rows whose route feasibility or identity is unproven.      These rows used (+3 more)

### Community 32 - "Community 32"
Cohesion: 0.49
Nodes (9): _audit_route(), _endpoint(), _formula_errors(), _funding_errors(), _history(), _json_url(), main(), _number() (+1 more)

### Community 33 - "Community 33"
Cohesion: 0.36
Nodes (6): _linked_user(), test_group_messages_are_ignored(), test_group_setup_requires_admin_and_records_community(), test_join_request_only_approves_active_linked_subscriber(), test_membership_worker_removes_expired_non_admin(), test_subscription_command_uses_linked_account_checkout()

### Community 34 - "Community 34"
Cohesion: 0.43
Nodes (7): _backup_sqlite(), _ensure_repository(), _excluded(), Stage consistent databases plus small operational state files., _require_restic_configuration(), run_backup(), stage_snapshot()

### Community 35 - "Community 35"
Cohesion: 0.33
Nodes (1): be

### Community 36 - "Community 36"
Cohesion: 0.53
Nodes (4): _database(), test_login_uses_opaque_session_and_subscription_expiry(), test_position_funding_and_alert_records_are_user_scoped(), test_telegram_link_is_one_time_and_chat_cannot_be_reassigned()

### Community 37 - "Community 37"
Cohesion: 0.6
Nodes (3): _credentials(), test_signed_get_does_not_retry_injected_test_client(), test_signed_get_retries_default_client_rate_limit()

### Community 38 - "Community 38"
Cohesion: 0.7
Nodes (4): _float(), main(), Resolve DEX contract addresses for Futures-DEX watchlist candidates.  Futures-DE, resolve()

### Community 39 - "Community 39"
Cohesion: 0.5
Nodes (4): exchange_market_url(), _market_parts(), Official exchange market links for read-only leg navigation., Return an official market page for a normalized exchange leg.

### Community 40 - "Community 40"
Cohesion: 0.5
Nodes (1): f()

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (2): main(), telegram_call()

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (2): main(), stripe_request()

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): Spread arbitrage research bot.

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Utility helpers for spreadarb.

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): Narrow live-operation helpers.  The main live engine remains locked. Modules in

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): Read-only decentralized exchange integrations.

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (1): Read-only API discovery for Telegram opportunity visibility.  The package is int

## Knowledge Gaps
- **302 isolated node(s):** `HTTP boundary for crypto checkout.  The subtle failure this guards against: memb`, `The tutorial must be reachable without an account -- it is a conversion page.`, `Crypto (Arbitrum USDC/USDT) prepaid billing.  The invariant that matters most: a`, `Dollars -> raw 6-decimal token units.`, `An exchange withdrawal fee must not cost the member their access.` (+297 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 29`** (12 nodes): `di()`, `o`, `.constructor()`, `.hi()`, `.ht()`, `.i()`, `.m()`, `.p()`, `.st()`, `.$t()`, `.u()`, `.v()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (7 nodes): `be`, `.CM()`, `.constructor()`, `.dM()`, `.draw()`, `.et()`, `.ht()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (4 nodes): `f()`, `.constructor()`, `.ht()`, `.st()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (3 nodes): `main()`, `configure_telegram_webhook.py`, `telegram_call()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (3 nodes): `main()`, `configure_stripe_webhook.py`, `stripe_request()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (2 nodes): `Spread arbitrage research bot.`, `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (2 nodes): `__init__.py`, `Utility helpers for spreadarb.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (2 nodes): `Narrow live-operation helpers.  The main live engine remains locked. Modules in`, `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (2 nodes): `Read-only decentralized exchange integrations.`, `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (2 nodes): `Read-only API discovery for Telegram opportunity visibility.  The package is int`, `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `FastQuoteRefresher` connect `Community 5` to `Community 10`, `Community 2`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Why does `WatchAsset` connect `Community 3` to `Community 6`, `Community 7`?**
  _High betweenness centrality (0.039) - this node is a cross-community bridge._
- **Why does `CryptoBillingError` connect `Community 11` to `Community 15`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **What connects `HTTP boundary for crypto checkout.  The subtle failure this guards against: memb`, `The tutorial must be reachable without an account -- it is a conversion page.`, `Crypto (Arbitrum USDC/USDT) prepaid billing.  The invariant that matters most: a` to the rest of the system?**
  _302 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.01 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.01 - nodes in this community are weakly interconnected._
- **Should `Community 2` be split into smaller, more focused modules?**
  _Cohesion score 0.02 - nodes in this community are weakly interconnected._