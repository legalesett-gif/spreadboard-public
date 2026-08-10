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

## Prepaid Crypto Billing

Production access is prepaid in allowlisted USDC or USDT on Arbitrum One. The
app is watch-only: it stores no wallet key and cannot spend or withdraw funds.
It assigns an exact amount to each one-hour invoice, observes confirmed ERC-20
transfers, and activates only the matching account.

Add these values to the production `app.env` file:

```bash
SPREADBOARD_CRYPTO_RECEIVING_ADDRESS=0x...
SPREADBOARD_CRYPTO_RPC_URL=https://arb-mainnet.example/rpc
SPREADBOARD_CRYPTO_CONFIRMATIONS=6
SPREADBOARD_CRYPTO_POLL_SECONDS=30
```

The checkout lets the member choose USDC or USDT, shows the verified token
contract, and produces an ERC-681 QR/wallet URI containing the exact token
amount. Transfers on another chain or from an unapproved token contract never
activate access. The production service starts the watcher automatically.

Stripe remains an optional, inactive card fallback. If card billing is enabled
later, configure hosted Checkout and the signed webhook with:

```bash
SPREADBOARD_STRIPE_SECRET_KEY=sk_live_...
SPREADBOARD_STRIPE_SCANNER_PRICE_ID=price_...
SPREADBOARD_STRIPE_RESEARCH_PRO_PRICE_ID=price_...
SPREADBOARD_STRIPE_WEBHOOK_SECRET=whsec_...
```

Never grant card access from a success redirect or an unsigned callback.

## Telegram Subscription Bot

The Telegram bot is a second front door to the same subscription account. It
can show status and open the signed-in crypto checkout, but it cannot activate
an account. Only a confirmed transfer observed by the watcher (or an explicitly
audited administrative action) can do that.

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
```

Members connect the bot from Account settings using a single-use link that
expires after ten minutes. `/top` works as a public preview in direct chat;
linked members can use `/subscribe`, `/mysubscription`, and `/access`.

For the separate public research feed, create a dedicated Telegram channel,
add the bot as an administrator, and set its channel username or numeric chat
ID. Never point this value at the private subscriber group:

```bash
SPREADBOARD_TELEGRAM_PUBLIC_FEED_CHAT_ID=@spreadboard_research
SPREADBOARD_TELEGRAM_OUTBOUND=1
```

## Transactional Email

Password recovery and membership lifecycle notices prefer Resend's HTTPS API,
which works even when a hosting provider blocks outbound SMTP ports. Keep the
send-only key in the production secret file, never in source control:

```bash
SPREADBOARD_RESEND_API_KEY=re_...
SPREADBOARD_SMTP_FROM='SpreadBoard <support@spreadarbitrage.ink>'
```

The existing `SPREADBOARD_SMTP_*` settings remain a fallback for deployments
whose network permits SMTP. The public status page reports recovery as ready
when either delivery path is fully configured.

## Browser Push

Browser alerts use an application-specific VAPID key pair. Keep the private
key in production secrets and expose only the public key to the browser:

```bash
SPREADBOARD_VAPID_PUBLIC_KEY=base64url_public_key
SPREADBOARD_VAPID_PRIVATE_KEY=base64url_private_key
SPREADBOARD_VAPID_SUBJECT=mailto:support@spreadarbitrage.ink
```

The account page reports delivery as unavailable until all three values are
configured. The service worker and push worker remain fail-closed otherwise.

## External Crypto Processors

The current production path does not depend on Whitepay or another custodial
checkout provider. Any future processor integration must verify its signed
server callback and exact chain, token, amount, invoice, and transaction state;
a browser redirect alone must never grant access.
