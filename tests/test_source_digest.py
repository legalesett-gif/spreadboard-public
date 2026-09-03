"""A deploy that reports OK must have shipped the source it was given."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import source_digest  # noqa: E402


def _tree(root: Path, body: str) -> Path:
    for name in source_digest.TREES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "spreadboard" / "market_history.py").write_text(body, encoding="utf-8")
    return root


def test_the_same_source_under_a_different_root_matches(tmp_path) -> None:
    """A working tree and /app inside the image are compared directly."""

    here = _tree(tmp_path / "worktree", "value = 1\n")
    there = _tree(tmp_path / "app", "value = 1\n")

    assert source_digest.digest(here) == source_digest.digest(there)


def test_a_stale_file_changes_the_digest(tmp_path) -> None:
    """The exact failure this exists to catch: one file left behind."""

    fresh = _tree(tmp_path / "fresh", "value = 2\n")
    stale = _tree(tmp_path / "stale", "value = 1\n")

    assert source_digest.digest(fresh) != source_digest.digest(stale)


def test_compiled_artifacts_do_not_move_the_digest(tmp_path) -> None:
    """Only the container writes __pycache__, and it must not fail the check."""

    root = _tree(tmp_path / "app", "value = 1\n")
    before = source_digest.digest(root)
    cache = root / "spreadboard" / "__pycache__"
    cache.mkdir()
    (cache / "market_history.py").write_text("junk\n", encoding="utf-8")

    assert source_digest.digest(root) == before
