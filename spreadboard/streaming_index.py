"""Read immutable route-index generations without a whole-file decode buffer."""
from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

import ijson

CHUNK_BYTES = 65536
SHARED_KEY_LIMIT = 2048
SHARED_VALUE_LIMIT = 32768
SHARED_VALUE_FIELDS = (
    "token", "token_name", "long_venue", "short_venue", "long_market_type",
    "short_market_type", "long_market_symbol", "short_market_symbol",
    "long_quote", "short_quote", "route_kind", "asset_class", "source_name",
    "freshness", "long_exchange_url", "short_exchange_url",
)


class _DigestReader:
    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        size = CHUNK_BYTES if size < 0 else min(size, CHUNK_BYTES)
        data = self.source.read(size)
        self.digest.update(data)
        self.bytes_read += len(data)
        return data


def _shared_fields(value: Any, keys: dict[str, str]) -> Any:
    # Streaming parsers otherwise allocate every repeated field name anew.
    # Keep only a bounded per-load pool, returning ordinary dicts and lists.
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            shared = keys.get(key)
            if shared is None:
                shared = key
                if len(keys) < SHARED_KEY_LIMIT:
                    keys[key] = key
            result[shared] = _shared_fields(item, keys)
        return result
    if isinstance(value, list):
        return [_shared_fields(item, keys) for item in value]
    if isinstance(value, Decimal):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite_index_number")
        return number
    if isinstance(value, int) and not -(2**63) <= value < 2**64:
        raise ValueError("index_integer_out_of_range")
    return value


def _shared_route_values(row: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    # These immutable values repeat across thousands of directed pairs. Unique
    # route IDs remain untouched, and the pool cannot grow across generations.
    for field in SHARED_VALUE_FIELDS:
        value = row.get(field)
        if not isinstance(value, str):
            continue
        shared = values.get(value)
        if shared is not None:
            row[field] = shared
        elif len(values) < SHARED_VALUE_LIMIT:
            values[value] = value
    return row


def read_index(path: Path, *, expected_bytes: int, expected_sha256: str) -> dict[str, dict[str, Any]] | None:
    """Return complete validated rows, or None; partial generations never escape."""
    try:
        with path.open("rb") as source:
            # kvitems alone would turn a scalar/array root into an empty dict.
            # Published files are compact; accept ordinary JSON whitespace too.
            first = source.read(1)
            while first and first in b" \t\r\n":
                first = source.read(1)
            if first != b"{":
                return None
            source.seek(0)
            reader = _DigestReader(source)
            keys: dict[str, str] = {}
            values: dict[str, str] = {}
            rows = {}
            # Decimal mode preserves uint64 integers. use_float=True overflows
            # above int64 on YAJL; convert only non-integer numbers afterward.
            for key, row in ijson.kvitems(reader, "", use_float=False, buf_size=CHUNK_BYTES):
                if not isinstance(key, str) or not isinstance(row, dict):
                    return None
                rows[key] = _shared_route_values(_shared_fields(row, keys), values)
            if reader.bytes_read != expected_bytes or reader.digest.hexdigest() != expected_sha256:
                return None
            return rows
    except (OSError, ValueError, ijson.JSONError, OverflowError, RecursionError):
        return None
