"""Lossless token-sized route storage, decoded only while that token is read."""
from __future__ import annotations

import base64
import zlib
from collections.abc import Iterator, Sequence
from typing import Any

import orjson

# A single token is normally below 5 MB. Bound corrupt input before inflation;
# this is a storage integrity limit, not a market or opportunity shortlist.
MAX_TOKEN_BYTES = 64 * 1024 * 1024
CODEC = "zlib-json-v1"


class PackedRoutes(Sequence):
    """Immutable compressed rows; each iteration owns its decoded dictionaries."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        raw = orjson.dumps(rows)
        if len(raw) > MAX_TOKEN_BYTES:
            raise ValueError("funding_token_storage_limit")
        self.data = zlib.compress(raw, 1)
        self.raw_bytes = len(raw)
        self.count = len(rows)

    def __len__(self) -> int:
        return self.count

    def _decode(self) -> list[dict[str, Any]]:
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(self.data, self.raw_bytes + 1)
            if (len(raw) != self.raw_bytes or not decoder.eof
                    or decoder.unused_data or decoder.unconsumed_tail):
                raise ValueError("invalid_packed_funding_size")
            rows = orjson.loads(raw)
        except (zlib.error, orjson.JSONDecodeError) as exc:
            raise ValueError("invalid_packed_funding_data") from exc
        if (not isinstance(rows, list) or len(rows) != self.count
                or any(not isinstance(row, dict) for row in rows)):
            raise ValueError("invalid_packed_funding_rows")
        return rows

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._decode())

    def __getitem__(self, index):
        return self._decode()[index]

    def envelope(self) -> dict[str, Any]:
        return {"codec": CODEC, "count": self.count, "raw_bytes": self.raw_bytes,
                "data": base64.b64encode(self.data).decode("ascii")}

    @classmethod
    def restore(cls, value: dict[str, Any]) -> PackedRoutes:
        if (value.get("codec") != CODEC
                or type(value.get("raw_bytes")) is not int
                or not 0 < value["raw_bytes"] <= MAX_TOKEN_BYTES
                or type(value.get("count")) is not int
                or not 0 <= value["count"] <= value["raw_bytes"]
                or not isinstance(value.get("data"), str)
                or len(value["data"]) > MAX_TOKEN_BYTES * 2):
            raise ValueError("invalid_packed_funding_envelope")
        obj = cls.__new__(cls)
        try:
            obj.data = base64.b64decode(value["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_packed_funding_encoding") from exc
        obj.raw_bytes = value["raw_bytes"]
        obj.count = value["count"]
        obj._decode()  # Validate even an advertised zero-row block before publishing.
        return obj
