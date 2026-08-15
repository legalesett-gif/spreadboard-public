"""The lint gate only has to stop the pile growing.

Mass-fixing hundreds of legacy findings would bury real review in noise, so the
ratchet freezes what exists today and fails only on something new. Counts are
keyed by (file, rule) rather than by line, because line numbers move whenever
anything above them is edited and a gate that cries wolf gets switched off.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ruff_ratchet

ROOT = Path(__file__).resolve().parents[1]


def test_an_unchanged_tree_passes() -> None:
    baseline = {("spreadboard/board.py", "I001"): 2}
    assert ruff_ratchet.regressions(baseline, dict(baseline)) == []


def test_a_new_rule_in_an_existing_file_is_a_regression() -> None:
    baseline = {("spreadboard/board.py", "I001"): 2}
    current = {("spreadboard/board.py", "I001"): 2, ("spreadboard/board.py", "S110"): 1}

    assert ruff_ratchet.regressions(baseline, current) == [
        ("spreadboard/board.py", "S110", 0, 1)
    ]


def test_more_of_the_same_rule_is_a_regression() -> None:
    baseline = {("spreadboard/board.py", "I001"): 2}
    current = {("spreadboard/board.py", "I001"): 3}

    assert ruff_ratchet.regressions(baseline, current) == [
        ("spreadboard/board.py", "I001", 2, 3)
    ]


def test_a_brand_new_file_must_be_clean() -> None:
    baseline: dict[tuple[str, str], int] = {}
    current = {("spreadboard/new_module.py", "BLE001"): 1}

    assert ruff_ratchet.regressions(baseline, current) == [
        ("spreadboard/new_module.py", "BLE001", 0, 1)
    ]


def test_fixing_findings_is_never_a_failure() -> None:
    baseline = {("spreadboard/board.py", "I001"): 2, ("spreadboard/old.py", "S110"): 4}
    current = {("spreadboard/board.py", "I001"): 1}

    assert ruff_ratchet.regressions(baseline, current) == []


def test_parsing_ignores_lines_that_are_not_findings() -> None:
    output = (
        "spreadboard/board.py:12:1: I001 [*] Import block is un-sorted\n"
        "spreadboard/board.py:40:9: I001 [*] Import block is un-sorted\n"
        "scripts/worker.py:3:1: EXE001 Shebang is present\n"
        "Found 3 errors.\n"
        "[*] 2 fixable with the `--fix` option.\n"
    )

    assert ruff_ratchet.parse(output) == {
        ("spreadboard/board.py", "I001"): 2,
        ("scripts/worker.py", "EXE001"): 1,
    }


def test_windows_style_paths_are_normalised() -> None:
    output = "spreadboard\\board.py:12:1: I001 [*] Import block is un-sorted"

    assert ruff_ratchet.parse(output) == {("spreadboard/board.py", "I001"): 1}


def test_the_committed_baseline_is_readable_and_not_empty() -> None:
    baseline = ruff_ratchet.load_baseline(ruff_ratchet.BASELINE_PATH)

    assert baseline, "the baseline must record the legacy findings it freezes"
    assert all(count > 0 for count in baseline.values())


@pytest.mark.skipif(ruff_ratchet.ruff_command() is None, reason="ruff is unavailable")
def test_the_tree_has_no_new_findings_against_the_baseline() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ruff_ratchet.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
