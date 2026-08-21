"""Release checks must prove the running package matches the checkpoint.

The production revision marker said the latest audit checkpoint was deployed
while ``fair_price.py`` and the core ``fast_quotes.py`` path were both older.
Page screenshots cannot catch every dormant code-path regression, so the
release gate needs a complete package manifest rather than a hand-picked hash.
"""

from __future__ import annotations

import pytest

from scripts import verify_production_source_sync as source_sync


def test_manifest_comparison_names_changed_missing_and_unexpected_modules() -> None:
    expected = {
        "spreadboard/fair_price.py": "fair-current",
        "spreadboard/fast_quotes.py": "quotes-current",
        "spreadboard/server.py": "server-current",
    }
    actual = {
        "spreadboard/fair_price.py": "fair-old",
        "spreadboard/server.py": "server-current",
        "spreadboard/retired.py": "retired",
    }

    assert source_sync.compare_manifests(expected, actual) == {
        "changed": ["spreadboard/fair_price.py"],
        "missing": ["spreadboard/fast_quotes.py"],
        "unexpected": ["spreadboard/retired.py"],
    }


def test_sha256_manifest_parser_keeps_only_package_python_files() -> None:
    output = "\n".join(
        (
            "a" * 64 + "  spreadboard/server.py",
            "b" * 64 + "  spreadboard/nested/helper.py",
            "c" * 64 + "  scripts/run_spreadboard_service.py",
            "not-a-checksum  spreadboard/broken.py",
        )
    )

    assert source_sync.parse_sha256_manifest(output) == {
        "spreadboard/nested/helper.py": "b" * 64,
        "spreadboard/server.py": "a" * 64,
    }


def test_sha256_manifest_parser_rejects_duplicate_paths() -> None:
    output = "\n".join(
        (
            "a" * 64 + "  spreadboard/server.py",
            "b" * 64 + "  spreadboard/server.py",
        )
    )

    with pytest.raises(ValueError, match="duplicate_manifest_path"):
        source_sync.parse_sha256_manifest(output)


def test_remote_container_names_are_validated_before_entering_a_shell_command() -> None:
    assert source_sync.valid_container_name("app-collector-1") is True
    assert source_sync.valid_container_name("app; touch /tmp/owned") is False


def test_ssh_destination_cannot_be_interpreted_as_an_openssh_option() -> None:
    assert source_sync.valid_ssh_host("root@178.128.126.204") is True
    assert source_sync.valid_ssh_host("deploy@spreadboard.example") is True
    assert source_sync.valid_ssh_host("-oProxyCommand=touch /tmp/owned") is False
