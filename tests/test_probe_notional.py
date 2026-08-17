"""One probe size, stated in one place.

The board advertises that a spread is executable at a given size. That claim is
worthless if the depth gate, the chart worker, the alert re-quote and the
on-page copy each carry their own number, so the size lives in one constant and
everything reads it.

The operator raised the probe from $50 to $500: $50 proves almost nothing about
whether a farm can actually be entered.
"""

from __future__ import annotations

import re
from pathlib import Path

from spreadboard import api_spreads, catalog_pairs

ROOT = Path(__file__).resolve().parents[1]


def test_the_probe_is_five_hundred_dollars() -> None:
    assert api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD == 500.0


def test_the_pair_catalogue_probes_the_same_size() -> None:
    """Two different probe sizes would rank routes against different bars."""
    assert catalog_pairs.TARGET_NOTIONAL_USD == api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD


def test_no_module_hardcodes_the_old_fifty_dollar_probe() -> None:
    """Each stray literal is a surface that silently disagrees with the board."""
    offenders = []
    for name in (
        "spreadboard/api_spreads.py",
        "spreadboard/alerts.py",
        "spreadboard/server.py",
        "spreadboard/catalog_pairs.py",
        "scripts/route_quote_worker.py",
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        if re.search(r"target_notional_usd\s*=\s*50\.0", text):
            offenders.append(name)
    assert not offenders, f"hardcoded $50 probe still in: {offenders}"


def test_the_page_does_not_quote_a_stale_probe_size_in_prose() -> None:
    """Copy that says "$50 rankings" outlives the constant it describes."""
    text = (ROOT / "spreadboard/server.py").read_text(encoding="utf-8")
    assert "$50 rankings" not in text


def test_every_probe_mention_on_the_page_tracks_the_constant() -> None:
    """Copy must not be able to disagree with the gate it describes."""
    from spreadboard import server

    assert server.PROBE_LABEL == "$500"
    text = (ROOT / "spreadboard/server.py").read_text(encoding="utf-8")
    # The size-preset buttons legitimately offer $50 as a choice; nothing else
    # may state the probe size as a literal.
    stale = [
        line.strip()
        for line in text.splitlines()
        if "$50 " in line and "PROBE_LABEL" not in line and "data-size-preset" not in line
    ]
    assert not stale, f"copy still hardcodes the probe size: {stale[:3]}"
