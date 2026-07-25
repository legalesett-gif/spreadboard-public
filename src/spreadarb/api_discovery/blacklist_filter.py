"""Filter discovery snapshots with the demo-derived live-entry blacklist."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_BLACKLIST_OVERRIDE_PATH = Path("data/blacklist_overrides.json")


@dataclass(frozen=True, slots=True)
class BlacklistFilterResult:
    api_rows: list[dict[str, Any]]
    dex_rows: list[dict[str, Any]]
    metadata: dict[str, Any]


def load_blacklist_overrides(path: Path | None = DEFAULT_BLACKLIST_OVERRIDE_PATH) -> set[str]:
    if path is None:
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {str(token).upper() for token in payload.get("allow") or [] if str(token).strip()}


def filter_blacklisted_rows(
    *,
    api_rows: Iterable[dict[str, Any]],
    dex_rows: Iterable[dict[str, Any]],
    blacklist: Mapping[str, Any],
    overrides: Iterable[str] = (),
    enabled: bool = True,
    load_error: str | None = None,
) -> BlacklistFilterResult:
    override_tokens = {str(token).upper() for token in overrides}
    blacklist_tokens = {str(token).upper() for token in blacklist if str(token).strip()}
    blocked_tokens = blacklist_tokens - override_tokens if enabled else set()
    excluded: list[dict[str, Any]] = []

    def _filter(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for row in rows:
            token = str(row.get("token") or "").upper()
            if token and token in blocked_tokens:
                excluded.append(row)
                continue
            kept.append(row)
        return kept

    filtered_api = _filter(api_rows)
    filtered_dex = _filter(dex_rows)
    excluded_tokens = sorted({str(row.get("token") or "").upper() for row in excluded if row.get("token")})
    metadata: dict[str, Any] = {
        "enabled": bool(enabled),
        "blacklist_token_count": len(blocked_tokens),
        "override_tokens": sorted(override_tokens),
        "excluded_count": len(excluded),
        "excluded_tokens": excluded_tokens,
    }
    if load_error:
        metadata["load_error"] = load_error
    return BlacklistFilterResult(api_rows=filtered_api, dex_rows=filtered_dex, metadata=metadata)
