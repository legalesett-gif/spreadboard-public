"""Transfer rails decide whether a fat spread is an opportunity or a closed door."""

from __future__ import annotations

import json
from contextlib import contextmanager

from spreadboard import public_rails

BINANCE_PAYLOAD = {
    "data": [
        {
            "coin": "HNT",
            "name": "Helium",
            "depositAllEnable": False,
            "withdrawAllEnable": False,
            "networkList": [
                {
                    "network": "SOL",
                    "depositEnable": False,
                    "withdrawEnable": False,
                    "contractAddress": "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux",
                }
            ],
        },
        {
            "coin": "AGLD",
            "name": "Adventure Gold",
            "depositAllEnable": True,
            "withdrawAllEnable": True,
            "networkList": [
                {
                    "network": "ETH",
                    "depositEnable": True,
                    "withdrawEnable": True,
                    "contractAddress": "0x32353a6c91143bfd6c7d363b546e62a9a2489a20",
                }
            ],
        },
        {"coin": "NOTREQUESTED", "depositAllEnable": True, "withdrawAllEnable": True},
    ]
}


@contextmanager
def _fake_response(payload):
    class Response:
        def read(self):
            return json.dumps(payload).encode("utf-8")

    yield Response()


def test_binance_rails_come_from_a_public_endpoint(monkeypatch) -> None:
    """CCXT sends fetch_currencies to a credentialed endpoint on Binance and,
    with no keys, returns an empty dict rather than raising -- indistinguishable
    from a venue that lists none of our tokens."""
    monkeypatch.setattr(public_rails, "urlopen", lambda *a, **k: _fake_response(BINANCE_PAYLOAD))
    rails = public_rails._fetch_native_venue_rails("Binance", {"HNT", "AGLD"})
    assert sorted(rails) == ["AGLD", "HNT"]
    assert rails["HNT"]["deposit"] is False and rails["HNT"]["withdraw"] is False
    assert rails["AGLD"]["deposit"] is True


def test_rails_carry_the_contract_address() -> None:
    """Identity verification needs to prove two venues list the same asset, not
    merely the same ticker, and the contract is the only thing that proves it."""
    with_contract = BINANCE_PAYLOAD["data"][1]["networkList"][0]["contractAddress"]
    assert with_contract.startswith("0x")


def test_binance_contract_reaches_the_parsed_rail(monkeypatch) -> None:
    monkeypatch.setattr(public_rails, "urlopen", lambda *a, **k: _fake_response(BINANCE_PAYLOAD))
    rails = public_rails._fetch_native_venue_rails("Binance", {"AGLD"})
    assert rails["AGLD"]["networks"][0]["contract"] == (
        "0x32353a6c91143bfd6c7d363b546e62a9a2489a20"
    )


def test_venues_without_a_public_source_fall_through_to_ccxt() -> None:
    assert public_rails._fetch_native_venue_rails("Kucoin", {"BTC"}) is None


def test_an_unreachable_public_source_falls_through(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise TimeoutError("slow")

    monkeypatch.setattr(public_rails, "urlopen", boom)
    assert public_rails._fetch_native_venue_rails("Binance", {"BTC"}) is None


def test_a_blind_venue_is_reported_not_silently_empty(tmp_path, monkeypatch) -> None:
    """An empty result read as 'this venue has no shut rails', which is the
    opposite of the truth when the endpoint simply needed credentials."""
    monkeypatch.setattr(public_rails, "_fetch_venue_rails", lambda venue, tokens: {})
    snapshot = {
        "api_discovered_rows": [
            {
                "token": "SIREN",
                "long_venue": "Mexc",
                "long_market_type": "Spot",
                "short_venue": "Kucoin",
                "short_market_type": "Spot",
            }
        ],
        "dex_discovered_rows": [],
    }
    payload = public_rails.refresh_public_rails(
        snapshot, path=tmp_path / "rails.json", force=True
    )
    assert payload["errors"]["Mexc"] == "no_public_rail_data:credentials_required"
    assert payload["errors"]["Kucoin"] == "no_public_rail_data:credentials_required"
