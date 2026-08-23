"""An empty snapshot must announce itself as empty.

`ready` was `bool(WARM_QUERY)`, and a payload dict carrying zero groups is
still truthy. So for the ~150 seconds a deploy takes to warm, the bot reported
itself live while holding nothing, the readiness gate let every lookup through,
and each one came back "no parsed routes right now".

A member reads that as "this token is not listed". It is the wrong answer to a
different question, and it is worse than the honest "still warming" that the
gate exists to produce.
"""

from __future__ import annotations

import pytest

from spreadboard import telegram_queries


@pytest.fixture(autouse=True)
def _clean():
    telegram_queries.reset_payload()
    yield
    telegram_queries.reset_payload()


def test_a_payload_with_no_tokens_is_not_ready() -> None:
    telegram_queries.replace_payload({"groups": []})

    status = telegram_queries.payload_status()

    assert status["ready"] is False
    assert status["token_count"] == 0


def test_a_payload_carrying_tokens_is_ready() -> None:
    telegram_queries.replace_payload(
        {"groups": [{"token": "GUA", "routes": [{"token": "GUA"}]}]}
    )

    status = telegram_queries.payload_status()

    assert status["ready"] is True
    assert status["token_count"] == 1
    assert status["route_count"] == 1


def test_an_empty_install_is_reported_as_not_ready_rather_than_hidden() -> None:
    """The board may legitimately hold nothing, and that must be sayable.

    Refusing the empty install would keep serving a stale answer forever, which
    is a worse lie than an honest gap. Readiness -- not the install -- is what
    stops an empty snapshot being answered as though it were data.
    """
    telegram_queries.replace_payload(
        {"groups": [{"token": "GUA", "routes": [{"token": "GUA"}]}]}
    )

    telegram_queries.replace_payload({"groups": []})

    status = telegram_queries.payload_status()
    assert status["token_count"] == 0
    assert status["ready"] is False


def test_a_transient_warming_generation_cannot_erase_the_last_complete_snapshot() -> None:
    """Cache admission control is not an authoritative empty market.

    ``api_market_spreads`` deliberately answers with this short-lived payload
    when another thread owns the expensive grouping slot.  Installing it made
    every Telegram data command stay in ``warming`` long after the website had
    recovered.
    """
    complete = {
        "groups": [{"token": "GUA", "routes": [{"token": "GUA"}]}],
    }
    telegram_queries.replace_payload(complete)

    active = telegram_queries.replace_payload(
        {"status": "warming", "groups": [], "rows": [], "pagination": {}}
    )

    assert active is complete
    assert telegram_queries.client_visible_payload() is complete
    status = telegram_queries.payload_status()
    assert status["ready"] is True
    assert status["token_count"] == 1


def test_an_incomplete_empty_build_cannot_erase_a_populated_source_snapshot() -> None:
    complete = {
        "groups": [{"token": "GUA", "routes": [{"token": "GUA"}]}],
    }
    telegram_queries.replace_payload(complete)

    active = telegram_queries.replace_payload(
        {
            "groups": [],
            "source_health": {"canonical_api": {"row_count": 28_028}},
        }
    )

    assert active is complete
    assert telegram_queries.payload_status()["ready"] is True


def test_a_partial_warming_funding_refresh_cannot_erase_complete_lanes() -> None:
    complete = {
        "groups": [{"token": "GUA", "routes": [{"token": "GUA"}]}],
    }
    telegram_queries.replace_funding_payloads([complete])

    active = telegram_queries.replace_funding_payloads(
        [complete, {"status": "warming", "groups": []}]
    )

    assert active["groups"][0]["token"] == "GUA"
    status = telegram_queries.payload_status()
    assert status["funding_token_count"] == 1
    assert status["funding_route_count"] == 1
