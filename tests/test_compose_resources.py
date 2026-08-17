"""The production compose file is the authority on runtime limits.

Time went into a setting that never applied. `collector.env` carried
SPREADBOARD_WS_BOOKS=120 while the compose `environment:` block set 96, and
Compose resolves `environment:` over `env_file:`. The value was raised, the
service restarted, and nothing changed, because the number being read was never
the number being edited.

These tests pin the facts that made that mistake expensive: which file wins, and
that the collector's CPU ceiling reflects the four vCPUs actually present.

The compose file is parsed here rather than with PyYAML, which is not a
dependency of this project. A test that skips itself guards nothing, and the
subset of YAML involved -- two indent levels of `key: value` under `services:`
-- is small enough to read directly.
"""

from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "compose.production.yml"

# The Droplet the production stack runs on.
HOST_VCPUS = 4.0


def _services() -> dict[str, dict[str, str]]:
    """Map service name to its scalar `key: value` settings.

    Nested blocks (lists, mappings) are skipped: nothing here needs them, and
    guessing at their shape is how a hand-rolled parser starts lying.
    """
    services: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    in_services = False

    for raw in COMPOSE.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^[A-Za-z_][\w-]*:", raw):
            in_services = raw.split(":", 1)[0] == "services"
            current = None
            continue
        if not in_services:
            continue
        service = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", raw)
        if service:
            current = {}
            services[service.group(1)] = current
            continue
        setting = re.match(r"^    ([A-Za-z_][\w-]*):[ \t]+(\S.*?)\s*$", raw)
        if setting and current is not None:
            current[setting.group(1)] = setting.group(2)
    return services


SERVICES = _services()


def test_the_parser_found_the_real_file() -> None:
    """Guard the guard: a parser that silently finds nothing passes everything."""
    assert {"app", "collector"} <= set(SERVICES)
    assert SERVICES["collector"].get("cpus")
    assert SERVICES["app"].get("cpu_shares")


def test_no_service_ceiling_exceeds_the_host() -> None:
    """A ceiling above the host's core count is a number that cannot happen."""
    for name, service in SERVICES.items():
        if "cpus" not in service:
            continue
        assert float(service["cpus"]) <= HOST_VCPUS, f"{name} exceeds {HOST_VCPUS} vCPUs"


def test_the_collector_ceiling_stays_inside_its_memory_budget() -> None:
    """The collector is memory-bound, and CPU buys memory it does not have.

    A 2.0 ceiling starved its websocket subscriptions, so it must exceed that.
    But at 3.2 the cgroup sat at 3.98GiB of anonymous memory against a 4GiB cap
    and stalled against that limit 1827 times. Raising this past 2.4 without
    also raising mem_limit -- which the 8GB host cannot fund -- walks back into
    the OOM that previously killed every scan.
    """
    cpus = float(SERVICES["collector"]["cpus"])
    assert cpus > 2.0, "2.0 starved the websocket book worker"
    assert cpus <= 2.4, "above 2.4 the collector exhausts its 4GiB budget"


def test_subscriber_http_still_outranks_background_collection() -> None:
    """Raising the ceiling is only safe because the share stays lopsided.

    Ceilings are oversubscribed on purpose: each service may burst into idle
    capacity. What protects a member loading a page is the weight, not the cap.
    """
    app_share = int(SERVICES["app"]["cpu_shares"])
    collector_share = int(SERVICES["collector"]["cpu_shares"])
    assert app_share >= collector_share * 4


def test_memory_limits_do_not_grow_further() -> None:
    """Memory is not CPU: over-committing it gets processes killed.

    The ceilings already sum above the 8GB the Droplet has, and the collector
    runs close enough to its own 4g cap that an earlier OOM took every scan
    down with it. Over-committing ceilings is normal -- they are not
    reservations -- but this particular total is as far as it may go, because
    the host has no slack left to absorb two services peaking together.
    """

    def megabytes(value: str) -> float:
        text = value.strip().lower()
        if text.endswith("g"):
            return float(text[:-1]) * 1024
        if text.endswith("m"):
            return float(text[:-1])
        raise AssertionError(f"unhandled mem_limit unit: {value}")

    total = sum(
        megabytes(service["mem_limit"])
        for service in SERVICES.values()
        if service.get("mem_limit")
    )
    assert total <= 8640, f"mem_limits total {total}MB exceeds the agreed ceiling"
