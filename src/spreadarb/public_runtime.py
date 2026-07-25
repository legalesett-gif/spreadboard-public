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
        return float(os.environ.get("SPREADARB_MAX_ENTRY_SPREAD_PCT", "70"))
    except ValueError:
        return 70.0
