# SpreadBoard executor boundary

SpreadBoard is the public-market research product. It must not load exchange
credentials, read private balances, or expose mutation endpoints.

Any future executor is a separate product with a different origin, deployment,
database, secret store, and authorization model. A route is not executable
merely because SpreadBoard displays it. The executor must independently refresh
the exact route, matched-size books, identity, funding/account placement,
short-leg support, conflicts, adapter coverage, and user authorization.

The research deployment has no order handoff endpoint. A separate executor must
implement per-route size/expiry authorization, idempotent two-leg state, a kill
switch, append-only audit logs, and independent post-trade reconciliation before
it can be considered for live use.
