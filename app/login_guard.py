"""A lock against guessing passwords: too many failed logins and the login waits.

Failures are counted in memory (per server process, over a sliding window) under two keys: the IP address (at most
5 failed logins per 15 minutes, whatever accounts they were for) and the email (at most 30, which stops guessing
spread over many machines). A key at its limit blocks the login until its oldest failure leaves the window; a
blocked login is refused before any password is checked. A successful login clears the counter of that email but not
that of the IP, so a valid account cannot be used to keep the counter down. A restart of the server resets
everything; a second replica would count separately.
"""

import logging
import math
import time

from app import users

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 15 * 60
LIMIT_IP = 5
LIMIT_EMAIL = 30
MAX_KEYS = 10_000  # bounds the memory when someone invents many addresses or IPs

_failures: dict[tuple, list[float]] = {}
_clock = time.monotonic


class Locked(Exception):
    """Too many failed logins; ``retry_after`` is the number of seconds to wait."""

    def __init__(self, retry_after: int):
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


def client_ip(forwarded_for: str | None, peer: str | None) -> str:
    """The address the gateway saw. Behind it, ``X-Forwarded-For`` ends with the address the gateway appended;
    the entries before it come from the client and cannot be trusted."""
    last = [part.strip() for part in (forwarded_for or "").split(",") if part.strip()]
    return last[-1] if last else (peer or "unknown")


def _keys(email: str, ip: str) -> list[tuple[tuple, int]]:
    return [(("ip", ip), LIMIT_IP), (("email", email), LIMIT_EMAIL)]


def _recent(key: tuple, now: float) -> list[float]:
    stamps = [t for t in _failures.get(key, []) if now - t < WINDOW_SECONDS]
    if stamps:
        _failures[key] = stamps
    else:
        _failures.pop(key, None)
    return stamps


def retry_after(email: str, ip: str) -> int:
    """Seconds until a login from this address and IP may be tried again; 0 if it may be tried now."""
    now = _clock()
    wait = 0.0
    for key, limit in _keys(email, ip):
        stamps = _recent(key, now)
        if len(stamps) >= limit:
            wait = max(wait, stamps[len(stamps) - limit] + WINDOW_SECONDS - now)
    return math.ceil(wait) if wait > 0 else 0


def record_failure(email: str, ip: str) -> None:
    now = _clock()
    for key, limit in _keys(email, ip):
        _recent(key, now)
        _failures.setdefault(key, []).append(now)
        if len(_failures[key]) == limit:
            logger.warning("login locked: %s after %d failed attempts", key, limit)
    while len(_failures) > MAX_KEYS:
        del _failures[next(iter(_failures))]


def record_success(email: str) -> None:
    _failures.pop(("email", email), None)


def authenticate(email: str, password: str, ip: str) -> users.User | None:
    """The user for a correct email and password, None for a wrong one; raises Locked while the login is blocked."""
    email = (email or "").strip().lower()
    wait = retry_after(email, ip)
    if wait:
        raise Locked(wait)
    user = users.authenticate(email, password)
    if user is None:
        logger.warning("login failed for %r from %s", email[:100], ip)
        record_failure(email, ip)
    else:
        record_success(email)
    return user
