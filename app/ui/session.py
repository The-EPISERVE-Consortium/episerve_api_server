"""The logged-in user of a browser session (the personal token is kept in the session storage)."""

from nicegui import app as _napp, ui

from app import users


def current_user() -> users.User | None:
    """Return the user identified by the token in browser session storage.

    Returns:
        The authenticated user, or None if the session token is missing or invalid.
    """
    return users.verify_token(_napp.storage.user.get("token"))


def can(right: str | None = None) -> bool:
    """Check whether the session grants access to a feature.

    Args:
        right: Optional feature permission to check.

    Returns:
        Whether the user is authenticated and has the requested permission.
    """
    user = current_user()
    return user is not None and user.can(right)


def executor() -> str | None:
    """Return the identity recorded in model-run provenance.

    Returns:
        The authenticated user's pseudonym, or None for an anonymous session.
    """
    user = current_user()
    return users.pseudonym(user) if user else None


def require_login(right: str | None = None) -> bool:
    """Check access and redirect unauthenticated or unauthorized visitors.

    Args:
        right: Optional feature permission required for the current page.

    Returns:
        Whether the visitor may continue rendering the protected page.
    """
    user = current_user()
    if user is None:
        ui.navigate.to("/login")
        return False
    if not user.can(right):
        ui.notify(f"You do not have access to this page (needs the '{right}' right).", type="negative")
        ui.navigate.to("/ui")
        return False
    return True
