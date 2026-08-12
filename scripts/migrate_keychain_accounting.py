#!/usr/bin/env python3
"""Encrypt one user's Keychain exchange credentials for server migration.

Plaintext is read locally from macOS Keychain and never printed or uploaded.
Only account/venue-bound encrypted envelopes are emitted.  The destination
worker private key is not needed on this device.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from spreadarb.public_runtime import keychain
from spreadboard import credential_crypto, exchange_credentials


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--venues", nargs="+", required=True)
    args = parser.parse_args()
    public_pem = args.public_key.read_bytes()
    output = []
    for raw_venue in args.venues:
        venue = exchange_credentials.normalize_venue(raw_venue)
        spec = exchange_credentials.VENUES[venue]
        payload = {}
        for field in spec["fields"]:
            service = f"SPREADARB/{venue}/{field}"
            value = keychain(service)
            if not value:
                raise SystemExit(f"missing_keychain_field:{venue}:{field}")
            payload[field] = value
        output.append(
            {
                "user_id": args.user_id,
                "venue": venue,
                "credential_encrypted": credential_crypto.encrypt(
                    payload,
                    context=credential_crypto.context(args.user_id, venue),
                    public_pem=public_pem,
                ),
                "credential_fields": list(spec["fields"]),
                "terms_version": exchange_credentials.TERMS_VERSION,
            }
        )
    json.dump({"connections": output}, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
