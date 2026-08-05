"""The membership pages say what you get, not an essay about it.

The pricing page carried fourteen paragraphs of prose; a member deciding
whether to pay reads the ticks and the price.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from spreadboard import accounts, crypto_billing, server


@pytest.fixture(autouse=True)
def _signed_out() -> None:
    accounts.set_current_user(None)


def _visible_prose(html: str) -> list[str]:
    """Paragraphs a reader has to wade through.

    Feature ticks are deliberately one line each and are the concise format, so
    they do not count; this measures the running prose that was the problem.
    """
    body = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", body, flags=re.S)
    cleaned = [" ".join(re.sub(r"<[^>]+>", " ", item).split()) for item in paragraphs]
    return [line for line in cleaned if len(line) > 45]


def test_the_pricing_page_is_short() -> None:
    html = server.render_pricing_page()
    prose = _visible_prose(html)

    # It used to run to eighteen blocks of copy.
    assert len(prose) <= 8, "\n".join(prose)
    assert max((len(line) for line in prose), default=0) <= 220


def test_the_pricing_page_still_says_the_essentials() -> None:
    html = server.render_pricing_page()

    assert "Every spread, live." in html
    assert "What you get" in html
    assert "Why membership" in html
    # Every feature tick is present.
    for feature in server.MEMBERSHIP_FEATURES:
        assert feature.split(" — ")[0][:30] in html


def test_the_terms_come_from_the_prices_actually_charged() -> None:
    """Written out by hand they drift the first time a price changes."""
    terms = server.membership_terms()

    assert [term["days"] for term in terms] == sorted(crypto_billing.PERIODS)
    for term in terms:
        assert term["total"] == pytest.approx(crypto_billing.PERIODS[term["days"]] / 100.0)
    # The shortest term is the reference, so it can never show a saving.
    assert terms[0]["saving_pct"] == 0
    # A longer term must not cost more per month than the shortest.
    assert all(term["per_month"] <= terms[0]["per_month"] + 1e-9 for term in terms)


def test_every_term_is_offered_on_both_pages() -> None:
    pricing = server.render_pricing_page()
    subscription = server.render_subscription_page()

    for term in server.membership_terms():
        assert term["label"] in pricing
        assert term["label"] in subscription


def test_the_subscription_page_keeps_its_consent_and_checkout() -> None:
    """Concise must not mean dropping what is legally required or functional."""
    html = server.render_subscription_page()

    assert "data-subscription-consent" in html
    assert "statutory cancellation right" in html
    assert 'href="/terms"' in html and 'href="/refunds"' in html
    assert "data-billing-error" in html
    assert "account-session" in html


def test_the_subscription_page_leads_with_the_plan_facts() -> None:
    html = server.render_subscription_page()

    assert "Monthly price" in html
    assert "Billing cycle" in html
    prose = _visible_prose(html)
    assert len(prose) <= 6, "\n".join(prose)


def test_a_renewal_date_reads_as_a_date() -> None:
    assert server.fmt_renewal_date("2026-08-18T00:00:00+00:00") == "18 August 2026"
    assert server.fmt_renewal_date(None) == "—"
    assert server.fmt_renewal_date("not a date") == "—"
