"""Application navigation and shared page layout."""

from nicegui import context, ui, app as _napp
from app.ui import session
from app.ui.footer import footer as _footer
from app.ui.theme import apply_theme
from app.ui.components.api_box import api_box


NAV_ITEMS = [
    ("Datasets",        "/ui/datasets"),
    ("Models",          "/ui/models"),
    ("Model Runs",      "/ui/model-runs"),
    ("Benchmarks",      "/ui/benchmarks"),
    ("AI Blackboard",   "/ui/blackboard"),
    ("Run Workflow",        "/ui/run_workflow/1"),
    ("Run Workflow Simple", "/ui/trigger"),
]


def header(current: str = "", api: str | None = None, method: str = "GET") -> None:
    """Render shared navigation, session controls, and the page footer.

    Args:
        current: Path identifying the active navigation item.
        api: Optional API path relative to the server root.
        method: HTTP method shown in the page's API command.
    """
    apply_theme()
    with ui.header().classes("epi-header"):
        ui.label("EPISERVE").classes("text-lg font-bold tracking-wide")
        with ui.row().classes("epi-nav"):
            for label, path in NAV_ITEMS:
                active = current == path or (
                    "/run_workflow" in path and current.startswith("/ui/run_workflow")
                )
                ui.link(label, path).classes(
                    "epi-nav-link " +
                    ("epi-nav-link-active" if active else "")
                )

            # User details dialog
            with ui.dialog() as details_dialog, ui.card().classes("w-full max-w-lg p-6"):
                ui.label("User Details").classes("text-base font-semibold text-gray-800 mb-3")
                detail_labels = {}
                for key, title in (("name", "Name"), ("email", "Email"), ("rights", "Rights")):
                    with ui.row().classes("items-baseline gap-2 w-full"):
                        ui.label(title).classes("text-xs uppercase tracking-wide text-gray-500 w-16")
                        detail_labels[key] = ui.label("").classes("text-sm text-gray-800")
                ui.label("Token").classes("text-xs uppercase tracking-wide text-gray-500 mt-3")
                with ui.row().classes("items-center gap-2 w-full"):
                    token_label = ui.label("").classes(
                        "font-mono text-sm bg-gray-100 rounded px-3 py-2 break-all flex-1"
                    )
                    def _copy_token():
                        """Copy the current session token to the clipboard."""
                        ui.run_javascript(f"navigator.clipboard.writeText({repr(_napp.storage.user.get('token', ''))})")
                        ui.notify("Copied to clipboard", position="top", type="positive")
                    ui.button(icon="content_copy", on_click=_copy_token).props("flat round dense").classes("text-gray-500")
                ui.button("Close", on_click=details_dialog.close).classes("mt-4 self-end").props("flat")

            def _open_details_dialog():
                """Populate and open the current user details dialog."""
                user = session.current_user()
                detail_labels["name"].set_text(user.name if user else "")
                detail_labels["email"].set_text(user.email if user else "")
                detail_labels["rights"].set_text(", ".join(sorted(user.rights)) if user and user.rights else "none")
                token_label.set_text(_napp.storage.user.get("token", ""))
                details_dialog.open()

            # Avatar button + dropdown menu
            with ui.element("div").classes("relative"):
                with ui.button(icon="account_circle").props("flat round").classes("text-gray-600 hover:text-blue-700"):
                    with ui.menu().classes("mt-1"):
                        ui.menu_item("User Details", on_click=_open_details_dialog)
                        ui.separator()
                        ui.menu_item("Logout", on_click=lambda: (
                            _napp.storage.user.clear(),
                            ui.navigate.to("/login"),
                        ))

    if api:
        with ui.row().classes("epi-api-row"):
            api_box(f"{base_url()}{api}", method)
    _footer()


def base_url() -> str:
    """Return the server base URL from the current browser request.

    Returns:
        The request base URL, or a relative root when no request is available.
    """
    request = context.client.request
    return str(request.base_url) if request is not None else "/"
