"""Session-backed selections for the model-run workflow."""

from nicegui import app as _napp


def workflow_state() -> dict:
    """Return persistent workflow selections, initializing missing fields.

    Returns:
        The mutable workflow selections stored in the current browser session.
    """
    if "tv2" not in _napp.storage.user:
        _napp.storage.user["tv2"] = {
            "datasets": [],
            "model": None,
            "sql": {},
            "config": None,
            "filenames": {},
            "benchmark": None,
        }
    _napp.storage.user["tv2"].setdefault("benchmark", None)
    return _napp.storage.user["tv2"]
