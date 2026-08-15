#!/usr/bin/env python3
"""Fail on new Ruff findings without demanding the legacy pile be fixed first.

The tree carries hundreds of pre-existing findings. Fixing them in one sweep
would produce a diff nobody can review, and leaving the linter unenforced lets
the pile grow. This freezes today's findings in a committed baseline and fails
only when a (file, rule) pair appears or grows.

Counts are keyed by file and rule, never by line number: line numbers shift
whenever anything above them changes, and a gate with false alarms gets turned
off. Fixing findings is always allowed -- the baseline is an upper bound.

    python3 scripts/ruff_ratchet.py            check the tree
    python3 scripts/ruff_ratchet.py --update   re-freeze after fixing things
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "ruff-baseline.txt"
TARGETS = ("spreadboard", "scripts", "tests", "src")
FINDING = re.compile(r"^(?P<path>[^:]+):\d+:\d+: (?P<rule>[A-Z]+\d+)\b")

Baseline = dict[tuple[str, str], int]


def parse(output: str) -> Baseline:
    """Turn concise Ruff output into {(path, rule): count}."""
    counts: Counter[tuple[str, str]] = Counter()
    for line in output.splitlines():
        match = FINDING.match(line.strip())
        if match is None:
            continue
        counts[(match.group("path").replace("\\", "/"), match.group("rule"))] += 1
    return dict(counts)


def regressions(
    baseline: Baseline, current: Baseline
) -> list[tuple[str, str, int, int]]:
    """Every (path, rule) that is new or more numerous than the baseline."""
    found = []
    for key, count in current.items():
        allowed = baseline.get(key, 0)
        if count > allowed:
            found.append((key[0], key[1], allowed, count))
    return sorted(found)


def load_baseline(path: Path) -> Baseline:
    if not path.is_file():
        return {}
    baseline: Baseline = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        file_path, rule, count = stripped.rsplit("\t", 2)
        baseline[(file_path, rule)] = int(count)
    return baseline


def write_baseline(path: Path, counts: Baseline) -> None:
    lines = [
        "# Ruff findings frozen by scripts/ruff_ratchet.py.",
        "# New or increased (file, rule) counts fail; fixing findings never does.",
        "# Regenerate with: python3 scripts/ruff_ratchet.py --update",
        # A different Ruff reports a different set, which would show up as
        # phantom regressions. CI pins this exact version.
        f"# frozen-with: {ruff_version()}",
    ]
    lines += [f"{p}\t{rule}\t{count}" for (p, rule), count in sorted(counts.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ruff_command() -> list[str] | None:
    """Ruff is not a project dependency, so accept either way of reaching it.

    CI installs it directly; the uv-managed local venv has no pip, and there
    `uvx ruff` is what developers already use.
    """
    if shutil.which("ruff"):
        return ["ruff"]
    if shutil.which("uvx"):
        return ["uvx", "ruff"]
    return None


def ruff_version() -> str:
    command = ruff_command()
    if command is None:
        return "unknown"
    result = subprocess.run(
        [*command, "--version"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def run_ruff() -> str:
    command = ruff_command()
    if command is None:
        raise SystemExit("ruff is unavailable: install ruff or uv")
    result = subprocess.run(
        [*command, "check", "--output-format=concise", *TARGETS],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    # Ruff exits 1 when it finds anything, which is the normal case here.
    if result.returncode not in (0, 1):
        raise SystemExit(f"ruff failed to run:\n{result.stderr}")
    return result.stdout


def main(argv: list[str]) -> int:
    current = parse(run_ruff())
    if "--update" in argv:
        write_baseline(BASELINE_PATH, current)
        print(f"froze {sum(current.values())} findings in {BASELINE_PATH.name}")
        return 0

    found = regressions(load_baseline(BASELINE_PATH), current)
    if not found:
        print(f"no new Ruff findings ({sum(current.values())} known)")
        return 0

    print("New Ruff findings are not allowed:\n")
    for file_path, rule, allowed, count in found:
        print(f"  {file_path}: {rule} {allowed} -> {count}")
    print(
        "\nFix them, or run `python3 scripts/ruff_ratchet.py --update` if the "
        "baseline is genuinely meant to change."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
