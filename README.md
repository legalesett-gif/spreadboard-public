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

## Monthly Billing

SpreadBoard uses Stripe-hosted Checkout and the Stripe Billing Portal. The app
never receives or stores card data. Add these values to the production
`app.env` file:

```bash
SPREADBOARD_STRIPE_SECRET_KEY=sk_live_...
SPREADBOARD_STRIPE_PRICE_ID=price_...
SPREADBOARD_STRIPE_WEBHOOK_SECRET=whsec_...
```

Create a recurring monthly Price in Stripe, then register this HTTPS webhook:

```text
https://spreadboard.178.128.126.204.sslip.io/api/billing/webhook
```

Subscribe it to `checkout.session.completed`,
`customer.subscription.created`, `customer.subscription.updated`,
`customer.subscription.deleted`, `invoice.paid`, and
`invoice.payment_failed`. Access changes only after a signed webhook is applied;
the Checkout success redirect does not grant access.
