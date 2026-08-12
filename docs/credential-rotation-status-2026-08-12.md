# Credential rotation status — 2026-08-12

This record deliberately contains credential names and outcomes only. It must
never contain secret values, partial values, or recoverable fingerprints.

## Completed in the coordinated maintenance window

- Telegram BotFather token: revoked at BotFather, replaced in production and
  macOS Keychain, then accepted with `getMe`, webhook, command-menu, and empty
  error-queue checks.
- Application field-encryption key: rotated after rewrapping encrypted Pushover
  fields.
- Telegram webhook secret: rotated and the webhook re-registered.
- VAPID public/private pair: rotated; pre-existing browser subscriptions were
  invalidated so they cannot retain an obsolete key pair.
- Subscriber-accounting RSA-3072 key pair: generated on the production host;
  the private key is mounted only into the unprivileged accounting worker and
  is backed up in macOS Keychain. The web application receives only the public
  key.
- Obsolete plaintext production environment backups and the temporary
  pre-rotation accounts database snapshot were securely removed after
  acceptance.

## Still requires rotation at the issuing provider

Changing a value only in the server does not revoke an exposed provider key.
The following credentials must be replaced or revoked in their provider
console, then supplied through a hidden local/Keychain handoff and accepted in
production:

- Exchange API credentials: OKX DEX, MEXC, Bybit, OKX, BingX, Binance,
  KuCoin, Gate, Bitget, and Kraken.
- Resend API key. The currently configured production and Keychain values both
  fail Resend authentication; SMTP fallback also does not connect from the
  production host. Password-recovery email is therefore not accepted.
- Pushover application token.
- Stripe test restricted/secret key and webhook secret. Stripe checkout is
  deliberately disabled; this does not block the live crypto checkout.
- Crypto RPC provider URL if its embedded provider token was present in the
  exposed transcript.

Provider-issued rotation must not be marked complete until the old credential
is revoked at source and the corresponding live acceptance check passes.

## Acceptance checklist for remaining provider rotations

1. Revoke or rotate the old key in the provider console.
2. Enter the replacement through hidden input on the operator device; never
   paste it into chat, command arguments, source control, or a plaintext local
   file.
3. Update production without printing the value, recreate only the dependent
   service, and store the replacement in macOS Keychain.
4. Verify the narrow function: private read-only account access, Resend
   password recovery, Pushover test, or Stripe test webhook as applicable.
5. Verify the old credential is rejected and record only the outcome.

