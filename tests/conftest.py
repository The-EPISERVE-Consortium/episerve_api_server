"""Test configuration.

Allow insecure default secrets so importing ``app.main`` (which now fails
closed on a shipped-default SESSION_SECRET) works in the test suite.
"""

import os

os.environ.setdefault("ALLOW_INSECURE_DEFAULT_SECRETS", "1")
