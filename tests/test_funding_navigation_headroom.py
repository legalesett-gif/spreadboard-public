"""A heavy child must not be spawned into a cgroup that cannot hold it.

The funding-navigation worker needs about 1.05GB of anonymous memory. The
collector's steady workers already hold 2.1-3.4GB of its 4GiB cgroup, so the
child was spawned regardless of headroom and the kernel killed it (exit=-9)
roughly every twenty minutes on 2026-08-28, each kill raising an owner alert
and wasting the whole build.

Headroom oscillates between about 605MB and 2,028MB as the periodic quote
workers cycle, so waiting for a real trough converts a hard kill into a short
deferral while the previous valid generation keeps serving.
"""

from __future__ import annotations

import pytest

from scripts import run_spreadboard_service as service


@pytest.fixture(autouse=True)
def _collector_role(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "_service_role", lambda: "collector")
    monkeypatch.setattr(service, "_LAST_FUNDING_NAVIGATION_AT", 0.0)
    monkeypatch.setattr(service, "_FUNDING_NAVIGATION_RETRY_AFTER", 0.0)
    monkeypatch.setattr(service, "_FUNDING_NAVIGATION_DEFERRED_SINCE", 0.0)
    yield


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> list:
    spawned: list = []

    def _run_worker(cmd, **_kwargs):
        spawned.append(cmd)
        raise AssertionError("the child must not be spawned without headroom")

    monkeypatch.setattr(service, "_run_worker", _run_worker)
    return spawned


def test_a_build_is_deferred_when_the_cgroup_cannot_hold_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production condition: 605MB free, child needs ~1.05GB."""

    spawned = _no_spawn(monkeypatch)
    monkeypatch.setattr(
        service, "_cgroup_anon_headroom_bytes", lambda: 605 * 1024 * 1024
    )
    health: list = []
    monkeypatch.setattr(
        service.coverage_reconciliation,
        "record_funding_navigation_health",
        lambda **kw: health.append(kw),
    )

    assert service._refresh_funding_navigation(force=True) is False
    assert not spawned, "no child may be spawned into an over-committed cgroup"
    assert not health, "a brief deferral is not a generation failure"


def test_a_build_proceeds_during_a_memory_trough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Troughs are real: headroom reached 2,028MB while workers cycled."""

    spawned: list = []

    class _Result:
        timed_out = False
        returncode = 1  # stop after the spawn; this test only asserts it ran
        stdout = ""
        stderr = "stopped"

    def _run_worker(cmd, **_kwargs):
        spawned.append(cmd)
        return _Result()

    monkeypatch.setattr(service, "_run_worker", _run_worker)
    monkeypatch.setattr(
        service, "_cgroup_anon_headroom_bytes", lambda: 2_028 * 1024 * 1024
    )
    monkeypatch.setattr(
        service.coverage_reconciliation,
        "record_funding_navigation_health",
        lambda **_kw: None,
    )

    service._refresh_funding_navigation(force=True)

    assert spawned, "a trough with ample headroom must allow the build"
    assert "funding_navigation_worker.py" in " ".join(str(x) for x in spawned[0])


def test_an_unbounded_cgroup_never_gates_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A developer machine has no cgroup limit and must not be blocked."""

    spawned: list = []

    class _Result:
        timed_out = False
        returncode = 1
        stdout = ""
        stderr = "stopped"

    monkeypatch.setattr(
        service, "_run_worker", lambda cmd, **_k: (spawned.append(cmd), _Result())[1]
    )
    monkeypatch.setattr(service, "_cgroup_anon_headroom_bytes", lambda: None)
    monkeypatch.setattr(
        service.coverage_reconciliation,
        "record_funding_navigation_health",
        lambda **_kw: None,
    )

    service._refresh_funding_navigation(force=True)

    assert spawned, "an unmeasurable cgroup must not block the build"


def test_a_persistent_deferral_is_surfaced_to_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferring forever would silently age the navigation generation."""

    _no_spawn(monkeypatch)
    monkeypatch.setattr(service, "_cgroup_anon_headroom_bytes", lambda: 100 * 1024 * 1024)
    health: list = []
    monkeypatch.setattr(
        service.coverage_reconciliation,
        "record_funding_navigation_health",
        lambda **kw: health.append(kw),
    )

    clock = {"t": 1_000.0}
    monkeypatch.setattr(service.time, "monotonic", lambda: clock["t"])

    service._refresh_funding_navigation(force=True)
    assert not health, "the first deferral is normal"

    # Past the alert threshold with no trough at all.
    clock["t"] += service.FUNDING_NAVIGATION_DEFERRAL_ALERT_SECONDS + 60
    monkeypatch.setattr(service, "_FUNDING_NAVIGATION_RETRY_AFTER", 0.0)
    service._refresh_funding_navigation(force=True)

    assert health, "a build that can never fit must be reported"
    assert health[-1]["ok"] is False
    assert "headroom" in health[-1]["detail"]
    assert "previous valid snapshot retained" in health[-1]["detail"]


def test_headroom_excludes_reclaimable_page_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """memory.current includes page cache; only anon causes an OOM kill."""

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.max").write_text("4294967296\n")
    # 2GB anon, plus a large page cache that must NOT count against us.
    (cgroup / "memory.stat").write_text("anon 2147483648\nfile 1610612736\nslab 7312560\n")

    monkeypatch.setattr(service, "Path", lambda p: cgroup / str(p).rsplit("/", 1)[-1])

    headroom = service._cgroup_anon_headroom_bytes()

    assert headroom == 2147483648, (
        "page cache is reclaimable and must not be treated as used"
    )
