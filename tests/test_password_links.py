"""Setting a password from a one-time link.

There was no recovery path at all: no reset, no forgot-password, and no mail
sender. A member who lost their password was locked out permanently, and a new
member could only be created by an admin typing a password -- which means the
admin knows it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spreadboard import accounts


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    accounts.create_user(
        email="anatolij@example.com",
        display_name="Anatolij",
        password="a-long-enough-password",
        db_path=path,
    )
    return path


def _user_id(db: Path) -> int:
    return [u for u in accounts.list_users(db_path=db) if u["email"] == "anatolij@example.com"][0]["id"]


def test_a_link_lets_someone_set_their_own_password(db: Path) -> None:
    user_id = _user_id(db)
    token = accounts.create_password_token(user_id, db_path=db)

    assert accounts.password_token_status(token, db_path=db)["display_name"] == "Anatolij"
    assert accounts.consume_password_token(token, "brand-new-password-1", db_path=db) is not None

    # The new password works, the old one does not.
    assert accounts.login("anatolij@example.com", "brand-new-password-1", db_path=db)
    with pytest.raises(ValueError):
        accounts.login("anatolij@example.com", "a-long-enough-password", db_path=db)


def test_a_link_works_exactly_once(db: Path) -> None:
    token = accounts.create_password_token(_user_id(db), db_path=db)

    assert accounts.consume_password_token(token, "first-password-here", db_path=db) is not None
    assert accounts.consume_password_token(token, "second-password-here", db_path=db) is None
    assert accounts.password_token_status(token, db_path=db) is None


def test_minting_a_new_link_retires_the_old_one(db: Path) -> None:
    user_id = _user_id(db)
    first = accounts.create_password_token(user_id, db_path=db)
    second = accounts.create_password_token(user_id, db_path=db)

    assert accounts.password_token_status(first, db_path=db) is None
    assert accounts.password_token_status(second, db_path=db) is not None


def test_an_expired_link_is_refused(db: Path) -> None:
    import sqlite3

    user_id = _user_id(db)
    token = accounts.create_password_token(user_id, db_path=db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE password_tokens SET expires_at = ? WHERE user_id = ?",
            ("2000-01-01T00:00:00+00:00", user_id),
        )
        connection.commit()
    finally:
        connection.close()

    assert accounts.password_token_status(token, db_path=db) is None
    assert accounts.consume_password_token(token, "another-password-here", db_path=db) is None


def test_spending_a_link_ends_every_existing_session(db: Path) -> None:
    """If the link was needed because the password leaked, old sessions must go."""
    user_id = _user_id(db)
    _user, token_value = accounts.login(
        "anatolij@example.com", "a-long-enough-password", db_path=db
    )
    assert accounts.user_for_session(token_value, db) is not None

    accounts.consume_password_token(
        accounts.create_password_token(user_id, db_path=db), "replacement-password", db_path=db
    )

    assert accounts.user_for_session(token_value, db) is None


def test_a_short_password_is_refused_before_anything_is_written(db: Path) -> None:
    user_id = _user_id(db)
    token = accounts.create_password_token(user_id, db_path=db)

    with pytest.raises(ValueError):
        accounts.consume_password_token(token, "short", db_path=db)

    # The link survives a rejected attempt, so the person can try again.
    assert accounts.password_token_status(token, db_path=db) is not None


def test_a_garbage_token_is_simply_invalid(db: Path) -> None:
    assert accounts.password_token_status("nonsense", db_path=db) is None
    assert accounts.consume_password_token("nonsense", "a-valid-password-x", db_path=db) is None
    assert accounts.consume_password_token("", "a-valid-password-x", db_path=db) is None


def test_the_raw_token_is_never_stored(db: Path) -> None:
    import sqlite3

    token = accounts.create_password_token(_user_id(db), db_path=db)
    connection = sqlite3.connect(db)
    try:
        stored = [r[0] for r in connection.execute("SELECT token_hash FROM password_tokens")]
    finally:
        connection.close()

    assert stored and token not in stored


def test_the_page_and_endpoints_are_reachable_without_a_session() -> None:
    """The person using the link cannot sign in yet -- that is the point."""
    import inspect

    from spreadboard import server

    gate = inspect.getsource(server.SpreadBoardHandler._authorize)
    assert '"/set-password"' in gate
    assert '"/api/set-password"' in gate
