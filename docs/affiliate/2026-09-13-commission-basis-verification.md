# Affiliate commission basis — code verification

Verified 13 September 2026 against `spreadboard/affiliates.py` and the
`affiliate_*` schema in `spreadboard/accounts.py`. Nothing was rebuilt and no
partner record was created. Live DB at verification time: **0 rows** in
`affiliate_partners`, `affiliate_clicks`, `affiliate_attributions`,
`affiliate_commissions`, `affiliate_payout_batches`.

## 1. The adopted basis is the collected amount — confirmed in code

`record_settled_commission()`:

```python
base = max(0, list_amount - discount)
commission = _bps(base, int(partner["commission_bps"]))
```

Commission is charged on what the buyer **actually paid**, not list price. The
handoff asked not to assume this; it is now established rather than assumed.

Constants: `DEFAULT_DISCOUNT_BPS = 2_000` (20%), `DEFAULT_COMMISSION_BPS = 5_000`
(50%). Rounding is `(amount * bps + 5_000) // 10_000` — round-half-up to the cent.

Computed by calling the real code, not by hand:

| | list | discount | buyer pays | partner earns |
|---|---:|---:|---:|---:|
| First month | $149.00 | −$29.80 | **$119.20** | **$59.60** |
| Each renewal | $149.00 | $0.00 | **$149.00** | **$74.50** |
| First 12 months | | | $1,758.20 | **$879.10** |

This matches the worked example in the handoff exactly.

## 2. Terms document already agrees with the code

`docs/affiliate-partner-agreement-draft.md` needs no change:

| Clause | Says | Code |
|---|---|---|
| §3.1 | 20% off the first 30-day membership value, once | `invoice_offer`, first-month gate |
| §4.1 | 50% of plan revenue **actually received** | `base = list − discount` |
| §5.2 | USDT on **Arbitrum One** only | schema defaults `USDT` / `Arbitrum` |
| Summary | "50% of settled plan revenue after discount" | same |

## 3. Controls that hold

- **Discount once only.** `invoice_offer` returns 0 if the user already has a
  `paid` invoice *or* an `open` invoice carrying a discount — so an open invoice
  reserves the entitlement and cannot be farmed by opening several.
- **Payout idempotency.** `affiliate_commissions.invoice_id` is
  `NOT NULL UNIQUE`, and the insert is `INSERT OR IGNORE`. A replayed settlement
  cannot double-credit. (The `INSERT OR IGNORE` is only safe *because* of that
  constraint — worth keeping them together if either is ever touched.)
- **Attribution is first-touch and durable.** `attach_registration` returns the
  existing attribution if one exists and never reassigns; the click must belong to
  a partner whose `status == 'active'`.
- **Recurring renewals are covered.** Commission is recorded per settled invoice,
  so renewals and restarts earn at the full-price base under the same attribution.

## 4. Two gaps found — flagged, NOT changed

These touch earned liabilities and partner-facing policy, so they are the owner's
call rather than a silent edit:

1. **No self-referral guard.** `attach_registration` does not check that the
   registering `user_id` is not the partner's own user, so a partner can take 50%
   of their own subscription and 20% off it. Cheap to add where the click is
   matched; wanted only if the owner's terms forbid it (§ of the draft does not
   currently say).
2. **Refunds after payout cannot be reversed.** `void_commission` raises
   `commission_cannot_be_voided` unless `status == 'pending'`. Once a commission
   is `in_batch` or `paid`, a later refund or chargeback has no reversal path and
   would need a manual negative adjustment. No such adjustment mechanism exists.

Neither is exploitable today — there are no partners and no commissions.

## 5. Exactly what is still needed, and from whom

**From the owner (blocks sending the agreement at all):**

| Placeholder in the draft | Meaning |
|---|---|
| `[FULL LEGAL NAME]` | Provider legal name |
| `[REGISTERED ADDRESS]` | Provider registered address |

The outreach email already warns: *"Do not send the signature agreement until the
Provider legal name, address … are filled."*

**From the influencer (collect in the reply, before creating a partner):**

- Full legal name or entity, and address with country
  (`[FULL LEGAL NAME OR ENTITY]`, `[ADDRESS AND COUNTRY]`)
- Contact email
- Channel URL
- Preferred slug (optional — `available_partner_slug()` will derive and
  de-duplicate one)
- **USDT wallet address *and* network** — terms restrict payouts to **Arbitrum
  One**; a wallet on another network cannot be paid
- Signed agreement and date (`[DATE]`)

**Ready to send once the two Provider fields are filled:**
`docs/affiliate/affiliate-outreach-email.md` (replace `[First name]`,
`[Channel]`), with `docs/affiliate/SpreadBoard Affiliate Partner Pack.pdf`.
Rebuild the pack with `docs/affiliate/build_affiliate_partner_pack.py` if the
source changes.

## 6. Currency note the handoff asked to clarify

"Always USDT" is an **affiliate payout** rule, not a checkout rule. Subscription
checkout accepts **USDC and USDT**; affiliate payouts are USDT-on-Arbitrum only.
These are separate policies and the accepted checkout currencies should not be
narrowed to match the payout rule.

**No email was sent, no partner record created, no money moved.**
