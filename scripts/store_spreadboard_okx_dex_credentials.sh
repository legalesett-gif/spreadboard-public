#!/bin/zsh
set -eu

repo_dir="${0:A:h:h}"
keychain_account="${USER}"

print "Use an API key created inside an OKX OnchainOS developer project."
print "An ordinary OKX exchange trading API key will not work here."
read -rs "okx_dex_api_key?Paste the OnchainOS API key (input is hidden): "
print
read -rs "okx_dex_secret?Paste the OnchainOS secret (input is hidden): "
print
read -rs "okx_dex_passphrase?Paste the OnchainOS passphrase (input is hidden): "
print
read -rs "okx_dex_project_id?Paste the project ID if shown, or press Return (input is hidden): "
print

if [[ -z "$okx_dex_api_key" || -z "$okx_dex_secret" || -z "$okx_dex_passphrase" ]]; then
  print -u2 "API key, secret and passphrase are required; nothing was saved."
  exit 1
fi

store_secret() {
  security add-generic-password -U -a "$keychain_account" -s "$1" -w "$2" >/dev/null
}

store_secret "SPREADARB/okx_dex/api_key" "$okx_dex_api_key"
store_secret "SPREADARB/okx_dex/secret" "$okx_dex_secret"
store_secret "SPREADARB/okx_dex/passphrase" "$okx_dex_passphrase"
if [[ -n "$okx_dex_project_id" ]]; then
  store_secret "SPREADARB/okx_dex/project_id" "$okx_dex_project_id"
else
  security delete-generic-password -a "$keychain_account" -s "SPREADARB/okx_dex/project_id" >/dev/null 2>&1 || true
fi

unset okx_dex_api_key okx_dex_secret okx_dex_passphrase okx_dex_project_id
print "Saved dedicated OKX OnchainOS credentials in macOS Keychain."
print "Running read-only token-list and bidirectional quote validation..."
cd "$repo_dir"
UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/verify_okx_dex_access.py

if [[ "${1:-}" == "--sync-production" ]]; then
  print "Local validation passed. Syncing to production and restarting only the collector..."
  UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/sync_okx_dex_credentials.py
fi
