"""Copyable API commands for the current browser session."""

from nicegui import ui, app as _napp


def api_command(url: str, method: str, token: str) -> str:
    """Build an authenticated curl command for the corresponding API endpoint.

    Args:
        url: Absolute endpoint URL.
        method: HTTP method to invoke.
        token: Bearer token for the current browser session.

    Returns:
        A curl command, using run.json as the body for non-GET requests.
    """
    if method == "GET":
        return f'curl -H "Authorization: Bearer {token}" {url}'
    return (f'curl -X {method} -H "Authorization: Bearer {token}" -H "Content-Type: application/json" '
            f"-d @run.json {url}")


def api_box(url: str, method: str = "GET") -> None:
    """Render an API command and a button to copy it with the session token.

    Args:
        url: Absolute endpoint URL shown to the user.
        method: HTTP method shown in the command.
    """
    shown = f"curl {url}" if method == "GET" else f"curl -X {method} {url}"
    with ui.element("div").classes("epi-api-box"):
        ui.label("API").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1")
        with ui.row().classes("items-center gap-2"):
            ui.label(shown).classes("font-mono text-xs text-gray-700")
            def _copy(u=url, m=method):
                """Copy the API command with the current session token to the clipboard.

                Args:
                    u: Endpoint URL captured when the component was rendered.
                    m: HTTP method captured when the component was rendered.
                """
                command = api_command(u, m, _napp.storage.user.get("token", ""))
                ui.run_javascript(f"navigator.clipboard.writeText({repr(command)})")
                ui.notify("Copied with token", position="top", type="positive")
            ui.button(icon="content_copy", on_click=_copy).props("flat round dense").classes("text-gray-400 hover:text-gray-700")
