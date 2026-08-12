#!/usr/bin/env python3
"""Import client-sealed exchange bundles from stdin without logging them."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from spreadboard import accounts, credential_crypto, exchange_credentials


DB_PATH = Path("/app/runtime/spreadboard_accounts.sqlite3")


def main() -> None:
    payload = json.load(sys.stdin)
    saved = []
    for row in payload.get("connections") or []:
        user_id = int(row["user_id"])
        venue = exchange_credentials.normalize_venue(str(row["venue"]))
        envelope = str(row["credential_encrypted"])
        credential_crypto.validate_envelope(envelope)
        fields = list(row.get("credential_fields") or [])
        expected = list(exchange_credentials.VENUES[venue]["fields"])
        if sorted(fields) != sorted(expected):
            raise ValueError("invalid_credential_fields")
        accounts.save_exchange_connection(
            user_id,
            venue,
            envelope,
            credential_fields=fields,
            terms_version=str(row.get("terms_version") or exchange_credentials.TERMS_VERSION),
            db_path=DB_PATH,
        )
        saved.append(venue)
    print(json.dumps({"ok": True, "saved": saved, "plaintext_received": False}))


if __name__ == "__main__":
    main()
