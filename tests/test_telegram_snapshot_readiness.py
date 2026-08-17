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
