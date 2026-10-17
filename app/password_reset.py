"""Forgotten passwords: a mailed one-hour link that sets a new password."""

import logging
import time

from app import mailer, users
from app.config import settings

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 10
_COOLDOWN_SECONDS = 60
_last_request: dict[str, float] = {}


def request_reset(email: str) -> None:
    """Mail a reset link if the email belongs to an active user; says nothing either way.

    At most one mail per address and minute, so the form cannot be used to flood someone's inbox.
    """
    email = (email or "").strip().lower()
    now = time.monotonic()
    if not email or now - _last_request.get(email, -_COOLDOWN_SECONDS) < _COOLDOWN_SECONDS:
        return
    _last_request[email] = now
    user = users.get_by_email(email)
    if user is None:
        return
    link = f"{settings.public_url}/reset-password?token={users.issue_reset_token(user)}"
    mailer.send_mail(
        user.email, "EPISERVE: reset your password",
        f"Hello {user.name},\n\nuse this link within one hour to choose a new password:\n\n{link}\n\n"
        "If you did not ask for this, you can ignore this mail; your password stays as it is.\n",
    )


def complete_reset(token: str, new_password: str) -> str | None:
    """Set the new password; None on success, otherwise the reason as a sentence for the user."""
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        return f"The password needs at least {MIN_PASSWORD_LENGTH} characters."
    user = users.verify_reset_token(token)
    if user is None:
        return "This link is invalid or has expired. Please ask for a new one."
    users.set_password(user.email, new_password)
    return None
