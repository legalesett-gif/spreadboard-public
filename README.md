# SpreadBoard

SpreadBoard is a read-only crypto spread and funding dashboard. It groups live
public exchange quotes by token, compares venue routes, normalizes funding over
24 hours, and stores short local chart history.

## Public-Site Boundaries

- Public market APIs only
- No exchange credentials
- No balances, positions, orders, transfers, swaps, or execution controls
- No private Telegram messages
- Telegram and notification settings are browser-local templates only

## Run

```bash
uv sync --frozen --no-dev
SPREADBOARD_PUBLIC_MODE=1 uv run python scripts/run_spreadboard_service.py
```

The service listens on `HOST` and `PORT`, refreshes every
`SPREADBOARD_REFRESH_SECONDS`, and exposes health at `/api/health`.
