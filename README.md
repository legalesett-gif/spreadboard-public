# SpreadBoard

SpreadBoard is a read-only crypto spread and funding dashboard. It groups live
public exchange quotes by token, compares venue routes, normalizes funding over
24 hours, and stores short local chart history.

## Public-Site Boundaries

- Public market APIs only
- No exchange credentials
- No balances, positions, orders, transfers, swaps, or execution controls
- Telegram subscription commands only after an explicit one-time account link
- No Telegram usernames, private message history, or payment credentials are stored

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
SPREADBOARD_SUBSCRIPTION_LABEL=$180/month
```

Create a recurring monthly Price in Stripe, then register this HTTPS webhook:

```text
https://spreadarbitrage.ink/api/billing/webhook
```

Subscribe it to `checkout.session.completed`,
`customer.subscription.created`, `customer.subscription.updated`,
`customer.subscription.deleted`, `invoice.paid`, and
`invoice.payment_failed`. Access changes only after a signed webhook is applied;
the Checkout success redirect does not grant access.

## Telegram Subscription Bot

The Telegram bot is a second front door to the same subscription account. It
can show status and create a hosted checkout link, but it cannot activate an
account. Only a verified payment-provider webhook can do that.

1. Create a dedicated bot with BotFather using `/newbot`.
2. Add the token, username without `@`, and a random webhook secret to
   production `app.env`:

```bash
SPREADBOARD_TELEGRAM_BOT_TOKEN=123456:replace_me
SPREADBOARD_TELEGRAM_BOT_USERNAME=spreadboard_bot
SPREADBOARD_TELEGRAM_WEBHOOK_SECRET=replace_with_a_random_32_byte_value
```

3. Restart the app, then register the webhook without putting the token on the
   command line:

```bash
SPREADBOARD_PUBLIC_URL=https://spreadarbitrage.ink \
  uv run python scripts/configure_telegram_webhook.py

SPREADBOARD_PUBLIC_URL=https://spreadarbitrage.ink \
  uv run python scripts/configure_stripe_webhook.py
```

Members connect the bot from Account settings using a single-use link that
expires after ten minutes. Supported commands are `/subscribe` and
`/mysubscription`.

## Crypto Checkout

Whitepay is a crypto payment processor, not Stripe. A Whitepay invoice is a
one-time crypto payment; recurring membership and renewal state remain the
responsibility of SpreadBoard. Whitepay production checkout is intentionally
disabled until merchant onboarding supplies the exact API contract, credentials,
and signed webhook specification. Do not grant access from a browser redirect
or an unsigned payment-status callback.
