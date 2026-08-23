#!/usr/bin/env python3
"""Verify dedicated OKX OnchainOS access without exposing credentials."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

okx_quotes = importlib.import_module("spreadarb.dex.okx_quotes")


CHAINS = ("1", "56", "42161", "8453", "137", "501")
WETH_ETHEREUM = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def verify() -> dict[str, object]:
    credentials = okx_quotes.load_okx_dex_credentials()
    if credentials is None:
        return {
            "ok": False,
            "blockers": ["dedicated_okx_onchainos_credentials_missing"],
        }

    counts: dict[str, int] = {}
    blockers: list[str] = []
    for chain in CHAINS:
        result = okx_quotes.list_tokens(chain=chain, credentials=credentials)
        if result.get("status") != "ok":
            blockers.extend(str(item) for item in result.get("blockers") or [])
            counts[chain] = 0
            continue
        counts[chain] = len(result.get("tokens") or [])

    buy = okx_quotes.quote_usdc_to_token(
        chain="1",
        token_address=WETH_ETHEREUM,
        notional_usd=Decimal(10),
        credentials=credentials,
    )
    sell: dict[str, object] = {}
    if buy.get("status") == "ok":
        try:
            quantity = Decimal(str(buy.get("out_qty") or "0"))
        except Exception:  # noqa: BLE001 - provider result validation.
            quantity = Decimal(0)
        if quantity > 0:
            sell = okx_quotes.quote_token_to_usdc(
                chain="1",
                token_address=WETH_ETHEREUM,
                token_quantity=quantity,
                token_decimals=int(buy.get("to_token_decimals") or 18),
                credentials=credentials,
            )
        else:
            blockers.append("okx_dex_buy_quantity_missing")
    else:
        blockers.extend(str(item) for item in buy.get("blockers") or [])
    if sell and sell.get("status") != "ok":
        blockers.extend(str(item) for item in sell.get("blockers") or [])

    blockers = list(dict.fromkeys(blockers))
    bidirectional_quote_ok = buy.get("status") == "ok" and sell.get("status") == "ok"
    catalogue_ok = sum(counts.values()) > 0 and any(value > 0 for value in counts.values())
    return {
        "ok": bool(catalogue_ok and bidirectional_quote_ok and not blockers),
        "provider": "OKX OnchainOS DEX API v6",
        "project_id_present": bool(credentials.project_id),
        "catalogue_token_counts": counts,
        "catalogue_total": sum(counts.values()),
        "bidirectional_weth_quote": bidirectional_quote_ok,
        "buy_price_present": buy.get("dex_buy_price_usd") is not None,
        "sell_price_present": sell.get("dex_sell_price_usd") is not None,
        "network_fee_evidence_present": (
            buy.get("trade_fee_usd") is not None and sell.get("trade_fee_usd") is not None
        ),
        "price_impact_evidence_present": (
            buy.get("price_impact_pct") is not None and sell.get("price_impact_pct") is not None
        ),
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify()
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("OKX OnchainOS access: " + ("verified" if result["ok"] else "blocked"))
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
