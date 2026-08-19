"""The add-position handler must close the dialog it is actually bound to.

`dialog.close()` shipped where the variable is `positionDialog`. The member saw
"Can't find variable: dialog" and could not add a position at all.

`node --check` passed it, because that validates syntax and says nothing about
scope -- and a flat "is this name declared anywhere?" check would have passed it
too, since `dialog` IS declared, just in a different function. Catching that
class of bug statically needs real scope analysis; what actually catches it is
exercising the handler in a browser, which is done during the page audit.

So this file pins the specific regression rather than pretending to be a
general guard.
"""

from __future__ import annotations

import re

from spreadboard import server


def test_the_add_position_handler_closes_the_dialog_it_belongs_to() -> None:
    source = server.render_account_script()
    index = source.index("request('/api/positions',payloadFromForm(form))")
    tail = source[index:index + 200]

    assert "positionDialog.close()" in tail
    assert re.search(r"(?<![A-Za-z$_])dialog\.close\(\)", tail) is None


def test_each_mutation_closes_its_own_dialog() -> None:
    """Edit and the row actions have their own dialogs; do not cross them."""
    source = server.render_account_script()

    edit = source.index("/edit`,payloadFromForm(form))")
    assert "editDialog.close()" in source[edit:edit + 200]

    action = source.index("/${suffix}`,Object.fromEntries(new FormData(form)))")
    assert "actionDialog.close()" in source[action:action + 200]
