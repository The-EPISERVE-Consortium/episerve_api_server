"""Sending mail through AgentMail (https://docs.agentmail.to).

``send_mail`` is the only entry point, so the provider can be swapped later. Without an API key or inbox
nothing is sent and the call returns False; a failed send is logged and also returns False.
"""

import logging
import sys

import requests

from app.config import settings

logger = logging.getLogger(__name__)


def configured() -> bool:
    return bool(settings.agentmail_api_key and settings.agentmail_inbox)


def send_mail(to: str | list[str], subject: str, text: str, html: str | None = None) -> bool:
    """Send a mail from the configured inbox; True if AgentMail accepted it."""
    if not configured():
        logger.warning("mail not sent: AGENTMAIL_API_KEY or AGENTMAIL_INBOX is not set")
        return False
    body = {"to": to, "subject": subject, "text": text}
    if html:
        body["html"] = html
    try:
        response = requests.post(
            f"{settings.agentmail_url.rstrip('/')}/v0/inboxes/{settings.agentmail_inbox}/messages/send",
            json=body, headers={"Authorization": f"Bearer {settings.agentmail_api_key}"}, timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("mail to %s could not be sent", to)
        return False
    return True


if __name__ == "__main__":
    # python -m app.mailer <to> <subject> <text>: a quick check that sending works
    if len(sys.argv) != 4:
        sys.exit("usage: python -m app.mailer <to> <subject> <text>")
    sent = send_mail(sys.argv[1], sys.argv[2], sys.argv[3])
    print("sent" if sent else "NOT sent (see the log)", file=sys.stderr)
    sys.exit(0 if sent else 1)
