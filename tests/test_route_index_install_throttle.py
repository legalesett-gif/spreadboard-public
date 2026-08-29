"""The web role must not re-parse the 300MB structural artifact on every publish.

Each restore decodes ~300MB into ~0.66GB of row dicts and glibc does not return
all of it. While the collector rebuilds it can republish every few minutes --
four installs in 25 minutes was measured -- which ratcheted the web role from
2.06GB to 3.33GB in 73 minutes and back toward its 3.5GB cgroup ceiling.

Live prices come from the process-shared overlay, not from this artifact, so
spacing structural reinstalls costs freshness of structure only, never of price.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from scripts import run_spreadboard_service as service


class _Watcher:
    """Only the throttle decision, isolated from the watcher's other work."""

    def __init__(self, interval: float) -> None:
        self.last_route_index_install_at: float | None = None
        self.min_route_index_install_interval_seconds = interval

    def due(self, changed: bool, now: float) -> bool:
        install_due = changed and (
            self.last_route_index_install_at is None
            or now - self.last_route_index_install_at
            >= self.min_route_index_install_interval_seconds
        )
        if install_due:
            self.last_route_index_install_at = now
        return install_due


def test_the_first_publication_is_never_throttled() -> None:
    w = _Watcher(600.0)
    assert w.due(True, 100.0) is True, "a cold web role must install immediately"


def test_rapid_republication_is_throttled() -> None:
    """The measured production pattern: four publishes in 25 minutes."""

    w = _Watcher(600.0)
    assert w.due(True, 0.0) is True
    installs = sum(w.due(True, t) for t in (180.0, 360.0, 540.0))
    assert installs == 0, "three republishes inside the window must not reinstall"


def test_the_next_publication_after_the_window_installs() -> None:
    w = _Watcher(600.0)
    w.due(True, 0.0)
    assert w.due(True, 300.0) is False
    assert w.due(True, 601.0) is True, "structure must not be starved indefinitely"


def test_an_unchanged_pointer_never_installs() -> None:
    w = _Watcher(600.0)
    assert w.due(False, 5_000.0) is False


def test_the_interval_is_configurable_and_defaults_sanely() -> None:
    source = Path(service.__file__).read_text(encoding="utf-8")
    assert "SPREADBOARD_ROUTE_INDEX_INSTALL_MIN_INTERVAL_SECONDS" in source, (
        "the throttle must be tunable without a code change"
    )


def test_throttling_does_not_skip_the_rest_of_the_watcher() -> None:
    """A throttled cycle must still run health notification and recovery work."""

    source = inspect.getsource(service.SharedArtifactWatcher.check_once)
    throttle_index = source.index("install_due")
    # No bare `return` may sit between the throttle decision and the end of the
    # install block; an early return would skip operator alerts entirely.
    install_block = source[throttle_index : source.index("funding_catalog_signature")]
    assert "\n            return\n" not in install_block
    assert "\n        return\n" not in install_block
