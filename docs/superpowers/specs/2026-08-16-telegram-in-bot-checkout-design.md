# In-bot Telegram checkout — design

Date: 2026-08-16
Bot: `@spreadarbitragesubscription_bot`
Status: approved design, not yet implemented

## Purpose

Sell a subscription entirely inside Telegram. Today `/subscribe` replies with a
button to `{SPREADBOARD_PUBLIC_URL}/subscription`, so every buyer is handed off
to the website to finish. This removes that handoff: tier, length, email,
payment details and confirmation all happen in the chat, and the buyer only
ever touches the website afterwards, to set their password.

## Non-goals

- No card payments. Payment stays exact-amount USDC/USDT on Arbitrum One,
  settled by the existing chain watcher on six confirmations.
- No Telegram Mini App. The chat itself is the interface.
- No change to pricing, tiers, entitlement rules, or the website checkout,
  which keeps working unchanged.
- No recurring billing. These are prepaid periods, as today.

## Product shape

Both tiers sell in the bot, at all three lengths:

| Tier | 30d | 90d | 365d | Private channel |
|---|---|---|---|---|
| Scanner | $49 | $135 | $490 | no |
| Research Pro | $149 | $375 | $1365 | yes |

Scanner is a deliberate choice for buyers who want the website and place no
value on the community, not a downgrade. The tier keyboard states what each
includes; it never frames Scanner as a refusal or a missing feature. The
distinction is shown *before* payment, so nobody discovers it afterwards.

## Conversation flow

`/start` keeps its current behaviour for chats that already have a linked
account: the existing help text, unchanged. For a chat with no linked account it
gains a "Subscribe" button above that help, so someone arriving at the bot for
the first time is one tap from buying. `/subscribe` always enters the flow.

```
/subscribe  (or the Subscribe button on a first-time /start)
  step=tier    → inline keyboard: Scanner | Research Pro
  step=period  → inline keyboard: 30d | 90d | 365d, priced for the chosen tier
  step=email   → bot asks for an email; buyer replies with free text
  step=confirm → summary + terms link + [Agree and get payment details]
  step=invoice → exact amount, receiving address, QR photo, expiry
                 [Check payment status]
  (chain watcher settles)
  → DM: paid, check your email
  → Research Pro only: [Request channel access]
```

Every step up to and including `invoice` is a synchronous webhook reply, so it
needs no outbound permission and cannot be blocked by the outbound guard. Only
the settlement DM is an asynchronous push; `SPREADBOARD_TELEGRAM_OUTBOUND=1` is
already set in production, and `_api_call` raises `TelegramOutboundDisabled`
rather than failing silently if that is ever turned off.

## Data model

One new table in the accounts database, created additively through the existing
`initialize()` / `_ensure_columns()` mechanism. No existing table changes shape.

```sql
CREATE TABLE IF NOT EXISTS telegram_checkout_sessions (
    chat_id INTEGER PRIMARY KEY,
    step TEXT NOT NULL CHECK (step IN ('tier','period','email','confirm','invoice')),
    tier TEXT CHECK (tier IN ('scanner','research_pro')),
    period_days INTEGER,
    email TEXT,
    invoice_id INTEGER REFERENCES crypto_invoices(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
```

One open checkout per chat. Restarting `/subscribe` replaces the session rather
than accumulating them. `invoice_id` links the session to the invoice so the
notifier knows which chat to tell.

## Components

**Checkout state machine** — a new `spreadboard/telegram_checkout.py`. Pure
functions over a session row returning the next reply payload, so each step is
testable without a webhook or a network. `telegram_bot.handle_update` dispatches
callback queries and plain-text replies into it. This is a new module rather
than more branches in `telegram_bot.py`, which is already carrying the whole
command surface.

**Settlement notifier** — a worker polling for invoices that are settled, came
from a Telegram checkout, and have not been announced. Deliberately *not* called
from inside `record_transfer`: billing must not depend on a chat platform, and a
failed Telegram call must never roll back or complicate a real payment. This
mirrors `subscription_lifecycle`, so it is retryable, idempotent and survives
restarts. It records its own delivery state so a redelivery cannot double-send.

**Account provisioning** — on settlement:
1. `create_invited_user()` if the email is new, else extend the existing account.
2. Bind the paying `chat_id` into `telegram_links`.
3. Email a single-use link: an `invite` token for a new account, a `reset` token
   for an existing one.
4. Research Pro only: create a join-request invite link, record the membership
   as `pending`, and DM it.

Binding the chat is what makes channel access work with no new approval logic:
the existing `chat_join_request` handler already auto-approves active Research
Pro linked accounts, and the existing membership sweep already removes members
whose subscription lapses.

## Security decisions

**Email existence is never disclosed.** An existing address gets a reset link, a
new one gets an invite link, and the bot says the same thing either way.
Otherwise checkout becomes an account-enumeration oracle available to anyone
with Telegram.

**Open invoices are capped per chat.** `_allocate_amount` hands out one-cent
slots from `MAX_SLOTS = 100` so concurrent invoices stay distinguishable by
amount. The website checkout sits behind a login; a public bot does not. Without
a cap, anyone could spam `/subscribe` and exhaust all 100 slots, breaking
checkout for every paying customer on both surfaces.

The cap is **3 open invoices per `chat_id`**, counted by querying
`crypto_invoices` rather than by keeping a counter on the session, so it cannot
drift out of step with the invoices it is meant to be counting. Beyond the cap
the bot re-shows the newest open invoice instead of issuing another. Stale
sessions expire on each interaction.

**Consent stays mandatory.** `test_consent_is_required_before_an_invoice_is_issued`
enforces that no invoice is issued without a `subscription_consents` row. The
`confirm` step shows the terms link and records consent on the tap, so the bot
cannot become the path that buys without agreeing to anything.

**Email is validated before anything is created.** The existing `create_user`
validation rules apply; an invalid address re-prompts and creates no rows.

## Edge cases

| Case | Behaviour |
|---|---|
| Invoice expires unpaid (60 min) | Session ends; `/subscribe` starts cleanly. The amount slot is released by `expire_stale_invoices`, as today. |
| Underpaid / overpaid | Unchanged: parked for admin, never guessed into a tier. The buyer is told it is under review, not that it failed. |
| Duplicate transfer | Unchanged: idempotent per `(tx_hash, log_index)`. |
| Buyer restarts mid-flow | Session replaced. Any open invoice stays valid and payable. |
| Buyer pays after the DM fails | Notifier retries; entitlement is already granted regardless. |
| Existing active subscriber renews | Expiry extends from current expiry, never truncated. |
| Scanner buyer asks for channel | `/access` explains Research Pro carries the forum, as today. |

## Testing

- Unit tests per state transition, with no webhook or network.
- A bot-driven variant of `tests/test_subscription_journey.py`: tier → period →
  email → consent → invoice → confirmed transfer → entitlement → linked chat →
  channel grant, for Research Pro; and the Scanner path asserting an account but
  no channel invite.
- Slot-exhaustion test: repeated `/subscribe` from one chat cannot consume more
  than the cap.
- Enumeration test: the reply for a known and an unknown email is byte-identical.
- Consent test: no invoice row exists without a matching consent row.
- Outbound-guard test: the pre-payment steps work with outbound disabled.

## Rollout

Ship behind `SPREADBOARD_TELEGRAM_CHECKOUT=1`, default off. With the flag off,
`/subscribe` keeps its current website-link behaviour, so the change is
reversible without a deploy. Turn it on once a testnet or small-value real
purchase has walked the whole path.

This flow is also the natural vehicle for the first authorized real-USDT
lifecycle (handover item 2), which still has no settled payment in the ledger.
