"""The three heaviest collector children run one at a time.

Each already serialised against ITSELF, but nothing stopped them overlapping
EACH OTHER. Measured peak RSS in the collector: market_evidence 1,487MB,
live_route_index 1,122MB, funding_navigation 993MB. Together that is 3,602MB
inside a 4,096MB cgroup, leaving under 500MB for a service that peaks at 690MB
alone -- which is why the kernel spent the day killing children. Summed over
every collector process the peaks reach 6,549MB against the same 4,096MB: the
container survives only because they usually miss each other.
"""

from __future__ import annotations

import threading

import pytest

from scripts import run_spreadboard_service as service


@pytest.fixture(autouse=True)
def _fresh_slot(monkeypatch):
    monkeypatch.setattr(service, "_HEAVY_CHILD_SLOT", threading.Semaphore(1))
    monkeypatch.setattr(service, "HEAVY_CHILD_SLOT_WAIT_SECONDS", 0.05)
    yield


def _fake_run(monkeypatch, record: list[str]):
    def _unslotted(command, **_kw):
        record.append(str(command[-1]))
        return service.WorkerResult(
            returncode=0, stdout="", stderr="", timed_out=False
        )

    monkeypatch.setattr(service, "_run_worker_unslotted", _unslotted)


@pytest.mark.parametrize(
    "script",
    [
        "scripts/market_evidence_worker.py",
        "scripts/live_route_index_worker.py",
        "scripts/funding_navigation_worker.py",
    ],
)
def test_each_heavy_child_takes_the_slot(monkeypatch, script: str) -> None:
    record: list[str] = []
    _fake_run(monkeypatch, record)

    service._HEAVY_CHILD_SLOT.acquire()
    try:
        result = service._run_worker(["python", script], timeout=1.0)
    finally:
        service._HEAVY_CHILD_SLOT.release()

    assert result.stderr == "heavy_child_slot_busy"
    assert record == [], f"{script} spawned while a sibling held the slot"


def test_a_light_worker_never_waits(monkeypatch) -> None:
    """Only the memory-heavy three are serialised; the rest keep their cadence."""

    record: list[str] = []
    _fake_run(monkeypatch, record)

    service._HEAVY_CHILD_SLOT.acquire()
    try:
        result = service._run_worker(["python", "scripts/fast_quote_worker.py"], timeout=1.0)
    finally:
        service._HEAVY_CHILD_SLOT.release()

    assert result.returncode == 0
    assert record == ["scripts/fast_quote_worker.py"]


def test_the_slot_is_released_after_a_heavy_child_finishes(monkeypatch) -> None:
    record: list[str] = []
    _fake_run(monkeypatch, record)

    for _ in range(3):
        assert service._run_worker(
            ["python", "scripts/live_route_index_worker.py"], timeout=1.0
        ).returncode == 0

    assert len(record) == 3, "the slot must not be stranded between runs"


def test_a_crashing_heavy_child_never_strands_the_slot(monkeypatch) -> None:
    def _boom(command, **_kw):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(service, "_run_worker_unslotted", _boom)

    with pytest.raises(RuntimeError):
        service._run_worker(["python", "scripts/market_evidence_worker.py"], timeout=1.0)

    assert service._HEAVY_CHILD_SLOT.acquire(blocking=False), "the slot leaked"
    service._HEAVY_CHILD_SLOT.release()
