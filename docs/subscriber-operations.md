# SpreadBoard subscriber operations

This runbook is the operating contract for the first 100 paying members. The
website remains a read-only research product; no subscriber supplies exchange
credentials and no customer action can reach the trading executors.

## Source of truth

- `spreadboard_accounts.sqlite3` is the authority for users, prepaid tier,
  exact expiry, crypto invoices/payments, Telegram links/membership state,
  alerts, notification preferences, and lifecycle-delivery records.
- Access is evaluated server-side on every protected request. `role=admin`
  receives Research Pro; every other user must have `active` or `trialing`
  status and an expiry in the future.
- Crypto settlement extends the same tier from the later of now or the existing
  expiry. A different tier cannot replace an active prepaid term.
- Telegram forum access is independently reconciled from the same account row.
  Only linked Research Pro members remain in the private forum; Scanner members
  keep personal alerts without forum membership.

## Customer journey

1. Admin creates the account and gives the customer the single-use seven-day
   password link. The admin never chooses or sees the final password.
2. Customer accepts the product terms and creates an exact crypto invoice.
3. The Arbitrum watcher waits for six confirmations, matches the exact token and
   amount to one open invoice, and atomically records payment plus tier/expiry.
4. Customer links Telegram from Account settings. Research Pro can use
   `/access`; the bot approves the join request and records membership.
5. The lifecycle worker creates service notices at seven days, three days, one
   day, and expiry. Delivery uses in-app notification and Web Push, plus email
   and Telegram DM when those channels are configured/linked.
6. At expiry the account is made inactive and the Telegram membership worker
   removes non-admin access. Account records and paid history remain intact for
   support and renewal.

## Daily operator check

- Open `/api/health` and confirm website/market health, crypto watcher readiness,
  Telegram query snapshot readiness/age, lifecycle worker running, and zero
  delivery or membership errors.
- In the admin Members panel check active Scanner/Research Pro counts, expiring
  within seven days, Telegram-linked accounts, and lifecycle delivery errors.
- Review unmatched crypto payments before replying to a customer. Never assign
  an underpayment, overpayment, duplicate, or expired-invoice transfer by guess.
- Confirm the public `/status` page. `setup_needed` for email is honest until
  SMTP and domain-authentication records are installed.

## Weekly operator check

- Run a consistent SQLite backup and `PRAGMA quick_check`; restore it into a
  disposable path and count users, invoices, payments, links, and lifecycle
  events.
- Review disk, RAM, container health/restart count, Caddy TLS expiry, market-data
  age, Telegram webhook errors, crypto watcher cursor lag, and notification
  delivery failures.
- Test one non-production member through login, `$TOKEN`, `@bot TOKEN`, alert
  creation, Telegram linking, and password reset. Do not send real crypto unless
  the exact transfer is separately authorized.

## Capacity at 100 subscribers

- SQLite uses WAL, foreign keys, a ten-second busy timeout, and indexed lookups.
  A few hundred customer/account writes are negligible beside the market-data
  workload. Subscription notification batches are persisted and deduplicated.
- Telegram membership reconciliation is sequential and bounded. One hundred
  `getChatMember` checks per minute remain comfortably below Telegram’s global
  bot rate, while webhook token lookups read a dedicated atomic snapshot and do
  no exchange I/O or board rebuild.
- HTTP is threaded and public/member pages are served from warmed bounded
  caches. The market refresh engine—not the account count—is the present CPU/RAM
  constraint. Measure before increasing venue breadth or refresh frequency.
- Move accounts to managed PostgreSQL only when write contention, multi-host
  application replicas, or measured customer traffic requires it. Do not add a
  distributed database merely because the member count reaches 100.

## Required external services before public onboarding

1. Transactional SMTP provider with verified sender, SPF, DKIM, and DMARC.
2. Recurring encrypted off-server restic repository and a tested restore alert.
3. External uptime check from outside DigitalOcean with owner notification.
4. A support mailbox or ticket address displayed in terms/refunds pages.
5. One authorized real payment/link/expiry acceptance test before the first
   unrelated paying customer.

## Incidents

- Website DNS errors: first compare the exact spelling, then query authoritative,
  Cloudflare, and Google resolvers; test HTTPS directly against the origin.
- Telegram silence: check `getWebhookInfo`, pending/error fields, bot privacy
  state, registered community ID, query-snapshot age, and one synthetic webhook
  that cannot post externally.
- Payment not credited: preserve transaction hash, token contract, amount,
  invoice ID/expiry, safe block, and watcher cursor. Never ask for a seed phrase.
- Restore need: stop writes, preserve the failed DB, validate the backup hash and
  `quick_check`, restore to a new file, then swap only after row-count and
  foreign-key verification.
