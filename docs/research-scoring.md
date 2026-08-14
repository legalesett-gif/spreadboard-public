# SpreadBoard research scoring

## Current production method

`deterministic_dual_opportunity_evidence_v5` keeps two different trade theses
separate. It is a rule-based research screen, not an AI prediction or a promise
of profit.

### Funding opportunity (0-100)

- Current settled 24-hour carry, with current-rate projection only as fallback.
- Settled 1-day, 7-day and 30-day persistence.
- Historical positive-rate ratio, funding volatility and sign reversals.
- Evidence-backed 24-hour spread-convergence contribution.
- Matched liquidity, quote freshness, exact symbols, identity/integrity and
  collateral stress.

The spread contribution starts neutral. It improves the funding score only
when comparable historical positive entry bases actually compressed. Adverse
historical widening reduces it. Missing history is neutral and lowers
confidence.

### Spread opportunity (0-100)

- Current executable long-cheap / short-rich entry basis.
- Frequency and median size of 24-hour convergence across six-hour-spaced,
  comparable-magnitude historical positive entries, plus observed time to halve
  where available. Spacing prevents overlapping hourly windows from pretending
  to be independent evidence.
- Expected 24-hour funding contribution.
- Matched liquidity, quote freshness, exact symbols, identity/integrity and
  collateral stress.

Positive funding improves the spread economics; negative funding reduces it.
The Watchlist may select a different best route for funding and for spread
entry. A large historical funding route therefore cannot silently replace the
best live convergence route, or vice versa.

### Confidence and costs

Each thesis has its own evidence confidence. Sparse history, unresolved token
identity, missing executable prices or a retained rather than live quote lower
confidence. Gross edge is shown separately from costs. A net edge is calculated
only when route-specific known costs are supplied; unknown account fees,
borrow, gas and exit slippage are never silently guessed.

The collateral reserve remains separate from both opportunity scores. It uses
observed leg volatility, adverse 24-hour tails, basis widening, correlation,
liquidity, route type and evidence quality. It is not a personalized
liquidation calculation.

## AI/ML development backlog

Do not replace the deterministic system with an LLM. A future statistical/ML
layer should begin only after enough clean, versioned history exists and should
predict measurable outcomes:

1. Probability that net funding remains positive over 8 and 24 hours.
2. Expected net carry after route-specific fees, slippage, gas and borrow.
3. Probability and expected magnitude/time of basis convergence.
4. P95 adverse future basis widening and per-leg adverse excursion.
5. Liquidation probability using exact venue tiers, account equity, margin mode
   and other positions.

Release gates are time-split/walk-forward backtesting, probability calibration,
cost-aware comparison against simple baselines, data-leakage tests, drift and
coverage monitoring, shadow-mode evaluation, an explanation for every output,
and an automatic deterministic fallback. Until these gates pass, ML outputs
must not be subscriber-facing or control position sizing.

Readiness is assessed on one immutable scoring-method version at a time. Older
versioned observations remain in the audit database but are excluded from a
newer candidate's training and test splits rather than permanently blocking
future experiments.
