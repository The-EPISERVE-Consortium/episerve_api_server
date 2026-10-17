"""The logged-in user of a browser session (the personal token is kept in the session storage)."""

from nicegui import app as _napp

from app import users


def current_user() -> users.User | None:
    return users.verify_token(_napp.storage.user.get("token"))


def can(right: str | None = None) -> bool:
    """Whether a user is logged in and (if a right is named) has it."""
    user = current_user()
    return user is not None and user.can(right)
