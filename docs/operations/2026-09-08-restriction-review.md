# Restriction review — 2026-09-08

## Outcome

One recently added project restriction was broader than the risk it protected
against and has been narrowed. The other reviewed controls remain tied to a
specific execution, security, data-integrity, or production-reliability risk.

## Removed or superseded

### App-only deploys no longer wait for collector discovery scans

Commit `6f88b9e` added a pre-deploy scan guard after three collector scans were
interrupted in one hour. The guard ran for every service selection, so even
`deploy_production.sh app` refused while a scan was active although an app-only
release uses `docker compose up --no-deps app` and never restarts the collector.

The guard now checks for an active scan only when `collector` is actually among
the requested services. Tests simulate an active scan and prove both outcomes:

- `app` proceeds without querying the collector process table;
- `collector` still refuses unless the operator explicitly uses `--force`.

The updated helper was installed on the production host without restarting any
service. Its prior version is backed up under `/opt/spreadboard/backups/`.

### Turn-scoped no-deploy/no-restart instruction

The broad no-production-mutation boundary was supplied in the original user
request that launched the SpreadExec dashboard implementation. It was not an
assistant-platform restriction and was not encoded as a permanent repository
policy. The user explicitly superseded it for the 2026-09-08 OPENAI Portfolio
hotfix. Stale operator notes saying the fix was forbidden from deployment have
been removed or rewritten to record the completed release.

## Retained controls

The following are not redundant restrictions and remain unchanged:

- no withdrawals and no withdrawal-capable exchange credentials;
- no public-process access to private exchange credentials;
- no arbitrary exchange REST proxy;
- no silent token, venue, route, market-type, leverage, sizing, or threshold
  substitution;
- no transfer, conversion, borrow, repayment, or live order without exact
  authorization and a fresh route-specific preflight;
- immutable/idempotent intents, owned-order reconciliation, partial-fill repair,
  global pause/kill behavior, and fail-closed live certification;
- collector-scan protection when the collector itself will be restarted;
- source-digest, health, OOM, rollback, and exact Portfolio reconciliation gates;
- preservation of unrelated dirty-worktree changes.

These controls address irreversible financial actions, credential isolation,
cross-venue non-atomicity, false route identity, duplicate orders, incomplete
hedges, and previously observed production/data-loss incidents. Removing them
would weaken correctness rather than reduce unnecessary friction.

## OPENAI release evidence

The app-only release changed the current marked-spread calculation for explicit
relative-value routes. OPENAI position `47` declares a `1:1` economic ratio, so
its deliberately unequal dollar-sized base quantities no longer distort the
market-price spread. Production verification at `2026-09-08T21:46:03Z` returned
MEXC mark `1496.4`, Hyperliquid mark `1621.2005`, API spread
`8.3400494520%`, and rendered `+8.340%`. External health was HTTP 200, the app
was healthy with zero restarts/OOM, and collector/accounting container identities
and start times were unchanged.

Rollback assets:

- source backup:
  `/opt/spreadboard/backups/portfolio.py-before-openai-spread-20260908T214413Z`
- image tag: `app-app:pre-openai-spread-20260908T214413Z`
- previous image ID:
  `ff1ab9c94d05fd48606f1f33dc2d30d138d57eb6bd7af9a185ef6be84033f80b`

