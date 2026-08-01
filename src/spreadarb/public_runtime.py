"""Minimal public-service configuration and secret lookup helpers."""

from __future__ import annotations

import os
import re
import subprocess


def secret_env_name(service: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", service).strip("_").upper()


def keychain(service: str) -> str | None:
    """Read a secret from the environment or macOS Keychain without logging it."""

    env_value = os.environ.get(secret_env_name(service))
    if env_value:
        return env_value
    user = os.environ.get("USER")
    if not user:
        return None
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-a", user, "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def discovery_max_spread_pct() -> float:
    try:
        # A large dislocation is a reason to require exact identity and fresh
        # books, not a reason to silently remove the market from discovery.
        return float(os.environ.get("SPREADARB_MAX_ENTRY_SPREAD_PCT", "0"))
    except ValueError:
        return 0.0


def discovery_min_spread_pct() -> float:
    """Spread floor for admitting a candidate pair.

    The reference product's whole Spot lane sits at 0.10-0.16%, so a 1% floor
    made that lane structurally unreachable: we were not missing those rows, we
    were declining to look at them.
    """
    try:
        return float(os.environ.get("SPREADARB_MIN_SPREAD_PCT", "0.05"))
    except ValueError:
        return 0.05


def discovery_min_funding_apr_pct() -> float:
    """Funding floor for admitting a candidate pair.

    A 25% floor plus APR-descending slot selection meant our tail above 200%
    consumed every funding slot, so the 100-340% band the reference product
    actually displays was never verified. Any positive carry is an opportunity.
    """
    try:
        return float(os.environ.get("SPREADARB_MIN_FUNDING_APR_PCT", "0.01"))
    except ValueError:
        return 0.01
