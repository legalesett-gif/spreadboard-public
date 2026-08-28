"""Atomic, restart-safe materialized navigation views.

The public discovery snapshot is intentionally rich, and grouping it for every
screen is CPU-bound.  A website process should never repeat that grouping just
because it restarted or a member opened page two.  This store publishes a whole
generation only after every required view and the chart-route index are safely
on disk.  Readers keep using the previous complete generation if a builder
fails, is interrupted, or produces a corrupt file.

Market prices and current funding are *not* trusted from this store.  The server
applies its live-book/funding overlay after loading the structural payload.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import orjson

SCHEMA = "spreadboard.materialized_views.v1"
LIVE_ROUTE_SCHEMA = "spreadboard.live_route_index.v1"
ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_ROOT = RUNTIME_DIR / "materialized_views"
NON_DATA_QUERY_KEYS = frozenset({"no_cache", "view"})


def canonical_query(query: dict[str, list[str]] | None) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the stable data-query identity, excluding the cache-control flag."""

    return tuple(
        sorted(
            (str(key), tuple(str(value) for value in values))
            for key, values in (query or {}).items()
            if key not in NON_DATA_QUERY_KEYS
            # Explicit URL defaults are the same data request as omissions.
            and not (key == "sort" and tuple(map(str, values)) == ("edge",))
            and not (key == "direction" and tuple(map(str, values)) == ("desc",))
            and not (key == "offset" and tuple(map(str, values)) == ("0",))
        )
    )


def query_identity(query: dict[str, list[str]] | None) -> str:
    encoded = orjson.dumps(canonical_query(query))
    return hashlib.sha256(encoded).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _query_dict(query: dict[str, list[str]] | None) -> dict[str, list[str]]:
    return {
        key: list(values)
        for key, values in canonical_query(query)
    }


class GenerationWriter:
    """Write one complete generation and atomically make it current."""

    def __init__(
        self,
        store: Store,
        *,
        required_queries: Iterable[dict[str, list[str]]],
        source_signature: dict[str, Any],
    ) -> None:
        self.store = store
        self.generation = f"{time.time_ns()}-{uuid.uuid4().hex[:12]}"
        self.required = {query_identity(query) for query in required_queries}
        self.source_signature = source_signature
        self.generations_dir = store.root / "generations"
        self.generations_dir.mkdir(parents=True, exist_ok=True)
        store.cleanup_staging()
        self.staging = Path(
            tempfile.mkdtemp(
                prefix=f".{self.generation}.", suffix=".tmp", dir=self.generations_dir
            )
        )
        self.views: dict[str, dict[str, Any]] = {}
        self.extras: dict[str, dict[str, Any]] = {}
        self.route_index_meta: dict[str, Any] | None = None
        self._published = False

    def write_view(self, query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        identity = query_identity(query)
        raw = _json_bytes(payload)
        filename = f"view-{identity}.json"
        _write_bytes(self.staging / filename, raw)
        self.views[identity] = {
            "identity": identity,
            "query": _query_dict(query),
            "file": filename,
            "bytes": len(raw),
            "sha256": _sha256(raw),
            "group_count": len(payload.get("groups") or []),
            "row_count": len(payload.get("rows") or []),
        }

    def write_route_index(self, rows: dict[str, dict[str, Any]]) -> None:
        raw = _json_bytes(rows)
        filename = "route-index.json"
        _write_bytes(self.staging / filename, raw)
        self.route_index_meta = {
            "file": filename,
            "bytes": len(raw),
            "sha256": _sha256(raw),
            "row_count": len(rows),
        }

    def write_extra(self, name: str, payload: dict[str, Any]) -> None:
        clean_name = "".join(character for character in str(name) if character.isalnum() or character in "-_")
        if not clean_name or clean_name != name:
            raise ValueError("invalid_extra_name")
        raw = _json_bytes(payload)
        filename = f"extra-{clean_name}.json"
        _write_bytes(self.staging / filename, raw)
        self.extras[clean_name] = {
            "file": filename,
            "bytes": len(raw),
            "sha256": _sha256(raw),
        }

    def publish(self) -> dict[str, Any]:
        missing = sorted(self.required - set(self.views))
        if missing:
            raise ValueError("missing_required_views:" + ",".join(missing))
        if self.route_index_meta is None:
            raise ValueError("missing_route_index")
        manifest = {
            "schema": SCHEMA,
            "generation": self.generation,
            "built_at_unix": time.time(),
            "source_signature": self.source_signature,
            "views": [self.views[key] for key in sorted(self.views)],
            "extras": self.extras,
            "route_index": self.route_index_meta,
        }
        manifest_raw = _json_bytes(manifest)
        _write_bytes(self.staging / "manifest.json", manifest_raw)
        _fsync_directory(self.staging)

        final = self.generations_dir / self.generation
        os.replace(self.staging, final)
        _fsync_directory(self.generations_dir)
        pointer = {
            "schema": SCHEMA,
            "generation": self.generation,
            "manifest_sha256": _sha256(manifest_raw),
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".current.", suffix=".tmp", dir=self.store.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_json_bytes(pointer))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.store.pointer_path)
            _fsync_directory(self.store.root)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        self._published = True
        self.store.invalidate()
        self.store._remove_obsolete_generations(keep=2)
        return manifest

    def abort(self) -> None:
        if not self._published and self.staging.exists():
            shutil.rmtree(self.staging)


class Store:
    """Read the current complete generation without owning any recomputation."""

    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.pointer_path = self.root / "current.json"
        self.live_route_pointer_path = self.root / "live-route-index-current.json"
        self._lock = threading.RLock()
        self._pointer_signature: tuple[int, int] | None = None
        self._manifest: dict[str, Any] | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._pointer_signature = None
            self._manifest = None

    def cleanup_staging(
        self, *, max_age_seconds: float = 21_600.0, now: float | None = None
    ) -> dict[str, int]:
        """Remove only abandoned builder-owned staging directories."""

        moment = time.time() if now is None else float(now)
        generations = self.root / "generations"
        removed = 0
        bytes_removed = 0
        try:
            entries = list(generations.iterdir())
        except OSError:
            return {"removed": 0, "bytes": 0}
        for entry in entries:
            if (
                not entry.is_dir()
                or entry.is_symlink()
                or not entry.name.startswith(".")
                or not entry.name.endswith(".tmp")
            ):
                continue
            try:
                if moment - entry.stat().st_mtime < max(60.0, max_age_seconds):
                    continue
                size = sum(
                    item.stat().st_size
                    for item in entry.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
                shutil.rmtree(entry)
            except OSError:
                continue
            removed += 1
            bytes_removed += size
        return {"removed": removed, "bytes": bytes_removed}

    def status(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        if manifest is None:
            return {"ready": False, "generation": None, "built_at_unix": None}
        return {
            "ready": True,
            "generation": manifest.get("generation"),
            "built_at_unix": manifest.get("built_at_unix"),
            "source_signature": manifest.get("source_signature") or {},
            "view_count": len(manifest.get("views") or []),
            "route_count": int((manifest.get("route_index") or {}).get("row_count") or 0),
        }

    def payload_for(
        self,
        query: dict[str, list[str]] | None,
        *,
        board_path: Path | str | None = None,
    ) -> dict[str, Any] | None:
        manifest = self._load_manifest()
        if manifest is None or not self._board_compatible(manifest, board_path):
            return None
        requested = _query_dict(query)
        identity = query_identity(requested)
        views = {
            str(item.get("identity") or ""): item
            for item in manifest.get("views") or []
            if isinstance(item, dict)
        }
        meta = views.get(identity)
        projected = False
        if meta is None:
            meta = self._pagination_superset(views.values(), requested)
            projected = meta is not None
        if meta is None:
            return None
        payload = self._read_verified_json(manifest, meta)
        if not isinstance(payload, dict):
            return None
        if projected:
            payload["_materialized_projection"] = {"query": requested}
        return payload

    def route_index(
        self, *, board_path: Path | str | None = None
    ) -> dict[str, dict[str, Any]] | None:
        manifest = self._load_manifest()
        if manifest is None or not self._board_compatible(manifest, board_path):
            return None
        payload = self._read_verified_json(manifest, manifest.get("route_index") or {})
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in payload.items()
        ):
            return None
        return payload

    def write_live_route_index(
        self,
        rows: dict[str, dict[str, Any]],
        *,
        source_signature: dict[str, Any],
        coverage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically publish the fast structural index independently of views."""

        payload = _json_bytes(rows)
        identity = f"{time.time_ns()}-{uuid.uuid4().hex[:10]}"
        filename = f"live-route-index-{identity}.json"
        path = self.root / filename
        meta = {
            "schema": LIVE_ROUTE_SCHEMA,
            "file": filename,
            "bytes": len(payload),
            "sha256": _sha256(payload),
            "row_count": len(rows),
            "built_at_unix": time.time(),
            "source_signature": source_signature,
            "coverage": dict(coverage or {}),
        }
        pointer = _json_bytes(meta)
        pointer_temp = self.root / f".{self.live_route_pointer_path.name}.{uuid.uuid4().hex}.tmp"
        _write_bytes(path, payload)
        _write_bytes(pointer_temp, pointer)
        os.replace(pointer_temp, self.live_route_pointer_path)
        _fsync_directory(self.root)
        self._remove_obsolete_live_route_indexes(keep=2)
        return meta

    def live_route_index_status(self) -> dict[str, Any]:
        try:
            meta = orjson.loads(self.live_route_pointer_path.read_bytes())
        except (OSError, orjson.JSONDecodeError):
            return {"ready": False, "built_at_unix": None, "row_count": 0}
        if not isinstance(meta, dict) or meta.get("schema") != LIVE_ROUTE_SCHEMA:
            return {"ready": False, "built_at_unix": None, "row_count": 0}
        filename = str(meta.get("file") or "")
        if not filename or Path(filename).name != filename:
            return {"ready": False, "built_at_unix": None, "row_count": 0}
        path = self.root / filename
        try:
            stat = path.stat()
        except OSError:
            return {"ready": False, "built_at_unix": None, "row_count": 0}
        return {
            **meta,
            "ready": stat.st_size == int(meta.get("bytes") or -1),
        }

    def live_route_index(
        self, *, board_path: Path | str | None = None
    ) -> dict[str, dict[str, Any]] | None:
        meta = self.live_route_index_status()
        if not meta.get("ready"):
            return None
        if board_path is not None:
            source = (
                meta.get("source_signature")
                if isinstance(meta.get("source_signature"), dict)
                else {}
            )
            expected = str(source.get("board_path") or "")
            if expected and expected != str(Path(board_path).resolve()):
                return None
        path = self.root / str(meta.get("file") or "")
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) != int(meta.get("bytes") or -1) or _sha256(raw) != str(
            meta.get("sha256") or ""
        ):
            return None
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in payload.items()
        ):
            return None
        return payload

    @staticmethod
    def _board_compatible(
        manifest: dict[str, Any], board_path: Path | str | None
    ) -> bool:
        if board_path is None:
            return True
        source = (
            manifest.get("source_signature")
            if isinstance(manifest.get("source_signature"), dict)
            else {}
        )
        expected = str(source.get("board_path") or "")
        return not expected or expected == str(Path(board_path).resolve())

    def extra(self, name: str) -> dict[str, Any] | None:
        manifest = self._load_manifest()
        if manifest is None:
            return None
        extras = manifest.get("extras") if isinstance(manifest.get("extras"), dict) else {}
        meta = extras.get(name)
        if not isinstance(meta, dict):
            return None
        payload = self._read_verified_json(manifest, meta)
        return payload if isinstance(payload, dict) else None

    def _pointer_stat(self) -> tuple[int, int] | None:
        try:
            stat = self.pointer_path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _load_manifest(self) -> dict[str, Any] | None:
        with self._lock:
            signature = self._pointer_stat()
            if signature == self._pointer_signature:
                return self._manifest
            self._pointer_signature = signature
            self._manifest = None
            if signature is None:
                return None
            try:
                pointer_raw = self.pointer_path.read_bytes()
                pointer = orjson.loads(pointer_raw)
                if not isinstance(pointer, dict) or pointer.get("schema") != SCHEMA:
                    return None
                generation = str(pointer.get("generation") or "")
                if not generation or Path(generation).name != generation:
                    return None
                manifest_raw = (
                    self.root / "generations" / generation / "manifest.json"
                ).read_bytes()
                if _sha256(manifest_raw) != str(pointer.get("manifest_sha256") or ""):
                    return None
                manifest = orjson.loads(manifest_raw)
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("schema") != SCHEMA
                    or manifest.get("generation") != generation
                ):
                    return None
            except (OSError, orjson.JSONDecodeError):
                return None
            self._manifest = manifest
            return manifest

    def _read_verified_json(
        self, manifest: dict[str, Any], meta: dict[str, Any]
    ) -> Any | None:
        filename = str(meta.get("file") or "")
        if not filename or Path(filename).name != filename:
            return None
        path = (
            self.root
            / "generations"
            / str(manifest.get("generation") or "")
            / filename
        )
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if len(raw) != int(meta.get("bytes") or -1) or _sha256(raw) != str(
            meta.get("sha256") or ""
        ):
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None

    @staticmethod
    def _pagination_superset(
        views: Iterable[dict[str, Any]], requested: dict[str, list[str]]
    ) -> dict[str, Any] | None:
        requested_base = {
            key: value for key, value in requested.items() if key not in {"limit", "offset"}
        }
        try:
            requested_limit = max(1, min(500, int((requested.get("limit") or [25])[0])))
            requested_offset = max(0, int((requested.get("offset") or [0])[0]))
        except (TypeError, ValueError):
            return None
        candidates: list[tuple[int, dict[str, Any]]] = []
        for meta in views:
            query = meta.get("query") if isinstance(meta.get("query"), dict) else {}
            base = {key: value for key, value in query.items() if key not in {"limit", "offset"}}
            if base != requested_base:
                continue
            try:
                offset = max(0, int((query.get("offset") or [0])[0]))
                limit = max(1, int((query.get("limit") or [25])[0]))
            except (TypeError, ValueError):
                continue
            if offset == 0 and limit >= requested_offset + requested_limit:
                candidates.append((limit, meta))
        return min(candidates, key=lambda item: item[0])[1] if candidates else None

    @staticmethod
    def _project_page(payload: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        try:
            offset = max(0, int((query.get("offset") or [0])[0]))
            limit = max(1, min(500, int((query.get("limit") or [25])[0])))
        except (TypeError, ValueError):
            return payload
        all_groups = list(payload.get("groups") or [])
        groups = all_groups[offset : offset + limit]
        route_keys = {
            str(route.get("route_key") or "")
            for group in groups
            if isinstance(group, dict)
            for route in group.get("routes") or []
            if isinstance(route, dict) and route.get("route_key")
        }
        result = dict(payload)
        result["groups"] = groups
        result["rows"] = [
            row
            for row in payload.get("rows") or []
            if isinstance(row, dict) and str(row.get("route_key") or "") in route_keys
        ]
        result["pagination"] = {
            "offset": offset,
            "limit": limit,
            "returned_rows": len(groups),
            "matching_rows": len(all_groups),
            "has_previous": offset > 0,
            "has_more": offset + len(groups) < len(all_groups),
        }
        filters = dict(result.get("filters") or {})
        filters.update({"offset": offset, "limit": limit})
        result["filters"] = filters
        summary = dict(result.get("summary") or {})
        summary["matching_tokens"] = len(all_groups)
        summary["returned_tokens"] = len(groups)
        summary["returned_rows"] = len(result["rows"])
        result["summary"] = summary
        funding = result.get("funding_catalog")
        if isinstance(funding, dict):
            funding = dict(funding)
            funding.update(
                {
                    "offset": offset,
                    "limit": limit,
                    "matching_token_count": len(all_groups),
                    "returned_token_count": len(groups),
                    "returned_route_count": len(result["rows"]),
                }
            )
            result["funding_catalog"] = funding
        return result

    def _remove_obsolete_generations(self, *, keep: int) -> None:
        generations = self.root / "generations"
        manifest = self._load_manifest() or {}
        current = str(manifest.get("generation") or "")
        valid: list[tuple[int, Path]] = []
        try:
            entries = list(generations.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
                continue
            try:
                candidate = orjson.loads((entry / "manifest.json").read_bytes())
            except (OSError, orjson.JSONDecodeError):
                continue
            if (
                isinstance(candidate, dict)
                and candidate.get("schema") == SCHEMA
                and candidate.get("generation") == entry.name
            ):
                valid.append((entry.stat().st_mtime_ns, entry))
        retained = {path for _stamp, path in sorted(valid, reverse=True)[: max(1, keep)]}
        if current:
            retained.add(generations / current)
        for _stamp, path in valid:
            if path not in retained:
                shutil.rmtree(path)

    def _remove_obsolete_live_route_indexes(self, *, keep: int) -> None:
        current = str(self.live_route_index_status().get("file") or "")
        try:
            candidates = sorted(
                (
                    path
                    for path in self.root.glob("live-route-index-*.json")
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            return
        retained = set(candidates[: max(1, keep)])
        if current:
            retained.add(self.root / current)
        for path in candidates:
            if path not in retained:
                try:
                    path.unlink()
                except OSError:
                    pass


_DEFAULT_STORE: Store | None = None


def default_store() -> Store:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None or _DEFAULT_STORE.root != DEFAULT_ROOT:
        _DEFAULT_STORE = Store(DEFAULT_ROOT)
    return _DEFAULT_STORE


def finalize_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Slice only after the server has live-repriced and globally re-ranked."""

    projection = payload.get("_materialized_projection")
    if not isinstance(projection, dict) or not isinstance(projection.get("query"), dict):
        return payload
    result = Store._project_page(payload, projection["query"])
    result.pop("_materialized_projection", None)
    return result
