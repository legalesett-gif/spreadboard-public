"""The beginner page must teach the product's actual evidence boundaries.

The old copy compressed funding and convergence into one vague basis-farm idea,
called the always-on canonical collector a "local source", and described the
whole member product as read-only even though journals, alerts and billing are
real mutations.  Beginners act on these words, so copy accuracy is functional.
"""

from spreadboard import server


def test_learn_page_separates_opportunities_and_names_live_source_truth() -> None:
    html = server.render_learn_page()

    assert "Funding opportunity" in html
    assert "Spread opportunity" in html
    assert "can offset or add to" in html
    assert "local source" not in html
    assert "canonical public-API collector" in html
    assert "zero verified DEX rows does not prove" in html


def test_learn_page_teaches_route_friction_and_scopes_read_only_claim() -> None:
    html = server.render_learn_page()

    assert "buy-low venue" in html
    assert "withdrawal and deposit rails" in html
    assert "Journal, alert, account and billing changes stay inside SpreadBoard" in html
    assert 'href="/methodology"' in html
    assert 'href="/status"' in html
    assert 'href="/guide"' in html
