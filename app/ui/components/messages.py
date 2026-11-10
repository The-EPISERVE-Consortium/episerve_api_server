"""Shared user-facing status messages."""

from nicegui import ui


def error_label(msg: str):
    """Render an inline error message using the shared theme.

    Args:
        msg: Error message to display.
    """
    ui.label(f"⚠ {msg}").classes("epi-error")
