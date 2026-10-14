"""Load secrets and refuse the placeholders that ship in the repository."""

import os

# Values that appear in this repository as placeholders. A deployed service
# must never use them: they are public, so anything signed or derived from
# them can be forged by anyone.
INSECURE_DEFAULTS = frozenset({"episerve-dev-secret-change-me"})

_TRUTHY = {"1", "true", "yes", "on"}


def _allow_insecure() -> bool:
    """Whether insecure defaults are explicitly permitted (local dev / tests)."""
    return os.environ.get("ALLOW_INSECURE_DEFAULT_SECRETS", "").strip().lower() in _TRUTHY


def require_secret(name: str, dev_default: str = "") -> str:
    """Return environment variable ``name`` or fail loudly.

    A value that is unset, blank, or equal to a shipped default placeholder is
    rejected with a RuntimeError, so a misconfigured deployment fails at startup
    instead of running with a publicly known secret. Set
    ``ALLOW_INSECURE_DEFAULT_SECRETS=1`` (local development and tests only) to
    fall back to ``dev_default``.

    Args:
        name: Environment variable to read.
        dev_default: Value returned only when insecure defaults are allowed and
            the variable is unset or blank.

    Returns:
        The configured secret.

    Raises:
        RuntimeError: If the secret is missing or still a known default and
            insecure defaults are not allowed.
    """
    value = (os.environ.get(name) or "").strip()
    if value and value not in INSECURE_DEFAULTS:
        return value
    if _allow_insecure():
        return value or dev_default
    raise RuntimeError(
        f"{name} is unset or still a shipped default. Generate a strong random value, "
        f'for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`, '
        f"and set it in the deployment. For local development or tests only, set "
        f"ALLOW_INSECURE_DEFAULT_SECRETS=1."
    )
