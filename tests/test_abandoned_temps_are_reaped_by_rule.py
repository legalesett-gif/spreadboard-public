"""The temp reaper knew one filename and missed every other writer.

`_cleanup_abandoned_discovery_temps` matched exactly
`.api_discovery_refresh.json.*.tmp`, so temps left by any other atomic writer
accumulated forever. Production carried 140MB across 7 files dating to Aug 24 --
`.complete_funding_catalog.json.<pid>.<tid>.tmp` and four
`.telegram_*_snapshot.json.<random>` files, the latter written by
`tempfile.mkstemp` and so carrying no `.tmp` suffix at all.

They are the residue of workers dying mid-write, which is what an OOM kill does.
The reaper now works from the shape shared by both atomic-write conventions
rather than a list of names, so a new writer cannot silently opt out of it.
"""

from __future__ import annotations

import os

from scripts import run_spreadboard_service as service

OLD = 1_000.0
NOW = 100_000.0


def _aged(path, *, age_seconds: float) -> None:
    stamp = NOW - age_seconds
    os.utime(path, (stamp, stamp))


def test_every_atomic_writer_convention_is_reaped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)

    reaped = [
        # `path.with_suffix(".tmp")` style, already covered
        ".api_discovery_refresh.json.4.9.tmp",
        # f".{name}.{pid}.{tid}.tmp" -- funding catalogue, 40.8MB in production
        ".complete_funding_catalog.json.1339.124457015696256.tmp",
        # tempfile.mkstemp(prefix=f".{name}.") -- no .tmp suffix at all
        ".telegram_spread_snapshot.json.irh2h3xu",
        ".telegram_funding_snapshot.json.kyjky_1h",
    ]
    for name in reaped:
        p = tmp_path / name
        p.write_text("xxx", encoding="utf-8")
        _aged(p, age_seconds=86_400)

    cleanup = service._cleanup_abandoned_discovery_temps(
        max_age_seconds=21_600, now=NOW
    )

    assert cleanup["removed"] == len(reaped), (
        f"reaped {cleanup['removed']} of {len(reaped)} abandoned temps"
    )
    for name in reaped:
        assert not (tmp_path / name).exists(), f"{name} survived"


def test_a_live_write_in_progress_is_never_deleted(tmp_path, monkeypatch) -> None:
    """The age gate is the entire safety property.

    Deleting a temp file a writer still holds corrupts that write. No atomic
    write takes six hours, so age is what separates debris from work in flight.
    """

    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)

    fresh = tmp_path / ".complete_funding_catalog.json.99.1.tmp"
    fresh.write_text("in flight", encoding="utf-8")
    _aged(fresh, age_seconds=60)

    cleanup = service._cleanup_abandoned_discovery_temps(
        max_age_seconds=21_600, now=NOW
    )

    assert cleanup["removed"] == 0
    assert fresh.exists(), "deleted a temp file a writer may still be holding"


def test_real_artifacts_and_plain_dotfiles_are_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)

    keep = [
        "venue_funding_history.json",   # the artifact itself, no leading dot
        "complete_funding_catalog.json",
        ".deployed_revision",           # a real dotfile, not a temp
        ".env",
    ]
    for name in keep:
        p = tmp_path / name
        p.write_text("keep", encoding="utf-8")
        _aged(p, age_seconds=86_400)

    cleanup = service._cleanup_abandoned_discovery_temps(
        max_age_seconds=21_600, now=NOW
    )

    assert cleanup["removed"] == 0
    for name in keep:
        assert (tmp_path / name).exists(), f"{name} was deleted"


def test_bytes_are_reported_so_the_log_is_meaningful(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)

    p = tmp_path / ".complete_funding_catalog.json.1.2.tmp"
    p.write_text("0123456789", encoding="utf-8")
    _aged(p, age_seconds=86_400)

    cleanup = service._cleanup_abandoned_discovery_temps(
        max_age_seconds=21_600, now=NOW
    )

    assert cleanup == {"removed": 1, "bytes": 10}
