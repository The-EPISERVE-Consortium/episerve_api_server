import json
import logging
import duckdb
import pandas as pd
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from nicegui import context, ui, run, app as _napp
from starlette.requests import Request

from app import login_guard, password_reset, users
from app.benchmarks import actions as bactions
from app.benchmarks.errors import NotFound as BenchmarkNotFound
from app.benchmarks.workflow import apply_benchmark, clear_benchmark, overlay_config, workflow_problems
from app.clients import ckan as ckan_client
from app.clients import blackboard as blackboard_client
from app.config import settings
from app.dataset_preview import (
    FILTER_OPERATORS, MAX_FILTERS, PAGE_SIZES, PreviewFilter, PreviewOptions,
    load_dataset, query_dataset, resolve_component,
)

from app.model_run_preview import (
    MAX_PREVIEW_ROWS, fetch_component_preview, fetch_run_metadata,
    find_run_components, resolve_model_run,
)
from app.sql_validation import validate_transformation_sql
from app.ui import session
from app.ui.footer import footer as _footer
from app.ui.theme import apply_theme

logger = logging.getLogger(__name__)


NAV_ITEMS = [
    ("Datasets",        "/ui/datasets"),
    ("Models",          "/ui/models"),
    ("Model Runs",      "/ui/model-runs"),
    ("Benchmarks",      "/ui/benchmarks"),
    ("AI Blackboard",   "/ui/blackboard"),
    ("Run Workflow",        "/ui/run_workflow/1"),
    ("Run Workflow Simple", "/ui/trigger"),
]


def _header(current: str = "", api: str | None = None, method: str = "GET"):
    """The top bar; ``api`` (a path below the server root) adds the API box for what the page shows."""
    apply_theme()
    with ui.header().classes("bg-white text-gray-800 border-b border-gray-200 px-8 py-3 flex items-center justify-between shadow-sm"):
        ui.label("EPISERVE").classes("text-lg font-bold tracking-wide")
        with ui.row().classes("gap-8 items-center"):
            for label, path in NAV_ITEMS:
                active = current == path or (
                    "/run_workflow" in path and current.startswith("/ui/run_workflow")
                )
                ui.link(label, path).classes(
                    "text-sm no-underline font-medium " +
                    ("text-primary border-b-2 border-primary pb-0.5" if active else "text-[#1e3a5f] hover:text-primary")
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
                        ui.run_javascript(f"navigator.clipboard.writeText({repr(_napp.storage.user.get('token', ''))})")
                        ui.notify("Copied to clipboard", position="top", type="positive")
                    ui.button(icon="content_copy", on_click=_copy_token).props("flat round dense").classes("text-gray-500")
                ui.button("Close", on_click=details_dialog.close).classes("mt-4 self-end").props("flat")

            def _open_details_dialog():
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
        with ui.row().classes("w-full justify-end px-8 pt-4"):
            _api_box(f"{_base_url()}{api}", method)
    _footer()


def _error_label(msg: str):
    ui.label(f"⚠ {msg}").classes("text-red-600 text-sm mt-2")


def _api_command(url: str, method: str, token: str) -> str:
    """The curl command the API box copies: the bearer token, and for POST a JSON file as the body."""
    if method == "GET":
        return f'curl -H "Authorization: Bearer {token}" {url}'
    return (f'curl -X {method} -H "Authorization: Bearer {token}" -H "Content-Type: application/json" '
            f"-d @run.json {url}")


def _api_box(url: str, method: str = "GET"):
    """Render the gray API hint box with a copy button that includes the auth token."""
    shown = f"curl {url}" if method == "GET" else f"curl -X {method} {url}"
    with ui.element("div").classes("bg-gray-50 border border-gray-200 rounded-lg px-4 py-2 shrink-0"):
        ui.label("API").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1")
        with ui.row().classes("items-center gap-2"):
            ui.label(shown).classes("font-mono text-xs text-gray-700")
            def _copy(u=url, m=method):
                command = _api_command(u, m, _napp.storage.user.get("token", ""))
                ui.run_javascript(f"navigator.clipboard.writeText({repr(command)})")
                ui.notify("Copied with token", position="top", type="positive")
            ui.button(icon="content_copy", on_click=_copy).props("flat round dense").classes("text-gray-400 hover:text-gray-700")


def _base_url() -> str:
    request = context.client.request
    return str(request.base_url) if request is not None else "/"


def _executor() -> str | None:
    """The pseudonym of the logged-in user, recorded as the one who executed a run."""
    user = session.current_user()
    return users.pseudonym(user) if user else None


def _require_login(right: str | None = None) -> bool:
    """True for a logged-in user with ``right``; otherwise redirects (to the login, or home when the right is missing)."""
    user = session.current_user()
    if user is None:
        ui.navigate.to("/login")
        return False
    if not user.can(right):
        ui.notify(f"You do not have access to this page (needs the '{right}' right).", type="negative")
        ui.navigate.to("/ui")
        return False
    return True


# ─── Trigger v2 helpers ────────────────────────────────────────────────────────

_TV2_STEPS = [
    (1, "Datasets",      "Choose input datasets"),
    (2, "Transforms",    "SQL per dataset"),
    (3, "Model",         "Select model"),
    (4, "Configuration", "JSON parameters"),
    (5, "Review & Run",  "Validate and run"),
]


def _tv2_state() -> dict:
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


def _tv2_stepper(current_step: int):
    with ui.row().classes("w-full items-center gap-0"):
        for i, (n, title, desc) in enumerate(_TV2_STEPS):
            active = n == current_step
            done   = n < current_step
            clickable = not active
            row_cls = "items-center gap-3 shrink-0 " + ("cursor-pointer" if clickable else "")
            with ui.row().classes(row_cls).on("click", lambda _, step=n: ui.navigate.to(f"/ui/run_workflow/{step}") if step != current_step else None):
                circle_cls = (
                    "w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold shrink-0 "
                    + ("bg-blue-700 text-white" if (active or done) else "border-2 border-gray-300 text-gray-400")
                )
                with ui.element("div").classes(circle_cls):
                    if done:
                        ui.icon("check").classes("text-sm")
                    else:
                        ui.label(str(n))
                with ui.column().classes("gap-0"):
                    ui.label(title).classes(
                        "text-sm font-semibold "
                        + ("text-blue-700" if active else "text-gray-700 hover:text-blue-600" if done else "text-gray-400 hover:text-blue-400")
                    )
                    ui.label(desc).classes("text-xs text-gray-400")
            if i < len(_TV2_STEPS) - 1:
                ui.element("div").classes(
                    "flex-1 h-px mx-4 " + ("bg-blue-300" if done else "bg-gray-200")
                )


def _tv2_summary(state: dict, hint: str = ""):
    DOT = ["bg-blue-500", "bg-green-500", "bg-purple-500", "bg-orange-500", "bg-red-500", "bg-teal-500"]
    with ui.element("div").classes("w-72 shrink-0 border border-gray-200 rounded-xl p-5 bg-white"):
        ui.label("Run Summary").classes("text-base font-bold text-gray-900")
        ui.label("Your selections will appear here.").classes("text-xs text-gray-400 mb-3")

        with ui.row().classes("w-full items-start gap-3 py-3 border-b border-gray-100"):
            ui.icon("storage").classes("text-blue-600 mt-0.5 shrink-0")
            with ui.column().classes("flex-1 gap-1 min-w-0"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Datasets").classes("text-sm font-semibold text-gray-800")
                    n_ds = len(state.get("datasets", []))
                    if n_ds:
                        with ui.element("div").classes("bg-green-100 text-green-700 text-xs px-2 py-0.5 rounded-full font-medium shrink-0"):
                            ui.label(f"{n_ds} selected")
                for i, ds in enumerate(state.get("datasets", [])):
                    with ui.row().classes("items-center gap-2"):
                        ui.element("div").classes(f"w-2 h-2 rounded-full shrink-0 {DOT[i % len(DOT)]}")
                        ui.label(ds["name"]).classes("text-xs text-gray-600 truncate")
                if not state.get("datasets"):
                    ui.label("None selected").classes("text-xs text-gray-400")

        bench = state.get("benchmark")
        if bench:
            with ui.row().classes("w-full items-start gap-3 py-3 border-b border-gray-100"):
                ui.icon("flag").classes("text-blue-600 mt-0.5 shrink-0")
                ui.label(f"Benchmark: {bench['name']} (data commit {bench['data_commit'][:12]})").classes(
                    "text-xs text-gray-600 flex-1 min-w-0")

        with ui.row().classes("w-full items-start gap-3 py-3 border-b border-gray-100"):
            ui.icon("view_in_ar").classes("text-blue-600 mt-0.5 shrink-0")
            with ui.column().classes("flex-1 gap-0"):
                ui.label("Model").classes("text-sm font-semibold text-gray-800")
                m = state.get("model")
                if m:
                    ui.label(f"{m['name']} ({m['docker_tag']})").classes("text-xs text-gray-600")
                else:
                    ui.label("Not selected yet").classes("text-xs text-gray-400")

        with ui.row().classes("w-full items-start gap-3 py-3"):
            ui.icon("tune").classes("text-blue-600 mt-0.5 shrink-0")
            with ui.column().classes("flex-1 gap-0"):
                ui.label("Configuration").classes("text-sm font-semibold text-gray-800")
                cfg = (state.get("config") or "").strip()
                try:
                    parsed = json.loads(cfg)
                    keys = list(parsed.keys())
                    summary = ", ".join(f"{k}: {parsed[k]}" for k in keys[:2]) if keys else "Configured"
                    ui.label(summary).classes("text-xs text-gray-600")
                except Exception:
                    ui.label("Not configured yet").classes("text-xs text-gray-400")

    if hint:
        with ui.row().classes("w-72 shrink-0 items-center gap-3 p-4 bg-blue-50 rounded-xl border border-blue-100 mt-3"):
            ui.icon("info_outline").classes("text-blue-500 shrink-0")
            ui.label(hint).classes("text-sm text-blue-700")


def _tv2_footer():
    with ui.row().classes("w-full justify-center items-center gap-2 py-6 mt-4 border-t border-gray-100"):
        ui.icon("lock").classes("text-gray-400 text-sm")
        ui.label("All runs are logged and reproducible.").classes("text-xs text-gray-400")


def _login_styles():
    ui.add_head_html('''<style>
        body { margin: 0; }
        .login-bg {
            min-height: 100vh; width: 100%;
            background: url("https://episerve.zib.de/img/hero.png") center/cover no-repeat;
            display: flex; align-items: center; justify-content: center;
        }
        .login-card {
            background: rgba(20, 24, 33, 0.82);
            backdrop-filter: blur(6px);
            border-radius: 12px;
            padding: 48px 40px 40px;
            width: 100%; max-width: 400px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.45);
            display: flex; flex-direction: column; gap: 0;
        }
        .login-card .q-field__control { background: #f3f4f6 !important; border-radius: 6px; }
        .login-card .q-field__native, .login-card .q-field__prefix { color: #111 !important; }
        .login-card .q-field--outlined .q-field__control:before { border: none !important; }
        .login-btn { background: #29abe2 !important; color: #fff !important; font-weight: 700;
                     letter-spacing: .08em; height: 44px; font-size: 15px; border-radius: 6px; }
    </style>''')


# ─── Page registrations ────────────────────────────────────────────────────────

def register_pages():

    @ui.page("/login")
    def login_page():
        if session.current_user() is not None:
            ui.navigate.to("/ui")
            return

        _login_styles()

        def do_login():
            request = context.client.request
            ip = login_guard.client_ip(request.headers.get("x-forwarded-for") if request else None,
                                       request.client.host if request and request.client else None)
            try:
                user = login_guard.authenticate(email_inp.value or "", password_inp.value or "", ip)
            except login_guard.Locked as locked:
                error.set_text(f"Too many failed attempts. Try again in {max(1, round(locked.retry_after / 60))} minutes.")
                return
            if user:
                _napp.storage.user["token"] = users.issue_token(user)
                ui.navigate.to("/ui")
            else:
                error.set_text("Invalid email or password")

        with ui.element("div").classes("login-bg"):
            with ui.element("div").classes("login-card"):
                ui.label("EPISERVE").classes("text-2xl font-bold text-white text-center w-full mb-8").style("display:block")
                email_inp = ui.input("Email").props('outlined dense prefix-icon="mail"').classes("w-full mb-4")
                password_inp = ui.input("Password").props('outlined dense type=password prefix-icon="lock"').classes("w-full mb-2")
                error = ui.label("").classes("text-red-400 text-sm mb-1").style("min-height:1.25rem")
                ui.button("LOGIN", on_click=do_login).classes("w-full login-btn mt-3")
                ui.link("Forgot your password?", "/forgot-password").classes("text-sm text-gray-300 text-center mt-4")
        _footer()

    @ui.page("/forgot-password")
    def forgot_password_page():
        _login_styles()

        async def send():
            email = email_inp.value or ""
            await run.io_bound(password_reset.request_reset, email)
            message.set_text("If this email belongs to an account, a mail with a reset link is on its way.")
            send_btn.disable()

        with ui.element("div").classes("login-bg"):
            with ui.element("div").classes("login-card"):
                ui.label("EPISERVE").classes("text-2xl font-bold text-white text-center w-full mb-4").style("display:block")
                ui.label("Enter your email address and we send you a link to choose a new password.").classes(
                    "text-sm text-gray-300 mb-4")
                email_inp = ui.input("Email").props('outlined dense prefix-icon="mail"').classes("w-full mb-2")
                message = ui.label("").classes("text-green-300 text-sm mb-1").style("min-height:1.25rem")
                send_btn = ui.button("SEND LINK", on_click=send).classes("w-full login-btn mt-3")
                ui.link("Back to login", "/login").classes("text-sm text-gray-300 text-center mt-4")
        _footer()

    @ui.page("/reset-password")
    def reset_password_page(token: str = ""):
        _login_styles()

        async def save():
            if (password_inp.value or "") != (repeat_inp.value or ""):
                error.set_text("The two passwords differ.")
                return
            problem = await run.io_bound(password_reset.complete_reset, token, password_inp.value or "")
            if problem:
                error.set_text(problem)
                return
            ui.notify("Password changed. Please log in.", type="positive")
            ui.navigate.to("/login")

        with ui.element("div").classes("login-bg"):
            with ui.element("div").classes("login-card"):
                ui.label("EPISERVE").classes("text-2xl font-bold text-white text-center w-full mb-4").style("display:block")
                ui.label("Choose a new password.").classes("text-sm text-gray-300 mb-4")
                password_inp = ui.input("New password").props('outlined dense type=password prefix-icon="lock"').classes("w-full mb-4")
                repeat_inp = ui.input("Repeat password").props('outlined dense type=password prefix-icon="lock"').classes("w-full mb-2")
                error = ui.label("").classes("text-red-400 text-sm mb-1").style("min-height:1.25rem")
                ui.button("SAVE PASSWORD", on_click=save).classes("w-full login-btn mt-3")
                ui.link("Back to login", "/login").classes("text-sm text-gray-300 text-center mt-4")
        _footer()

    @ui.page("/ui")
    def _root():
        ui.navigate.to("/ui/datasets")

    @ui.page("/ui/datasets")
    def datasets_processed_lab(request: Request):
        """Render catalog browsing and structured dataset preview controls.

        Args:
            request: Incoming request used to construct the API link.
        """
        from datetime import datetime

        _header("/ui/datasets", api="datasets")

        def _fmt_date(dt_str: str) -> tuple:
            """Format a catalog timestamp for display.

            Args:
                dt_str: Catalog timestamp.

            Returns:
                Display date and relative age.
            """
            if not dt_str:
                return "—", ""
            try:
                s = dt_str.strip()
                dt = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(s[:len(fmt) - fmt.count("%") * 1], fmt)
                        break
                    except ValueError:
                        pass
                if dt is None:
                    dt = datetime.strptime(s[:10], "%Y-%m-%d")
                days = (datetime.now() - dt).days
                rel = "Today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"
                return dt.strftime("%b %-d, %Y"), rel
            except Exception:
                return dt_str, ""

        try:
            raw_rows = ckan_client.list_processed_datasets()
        except Exception as e:
            with ui.column().classes("px-8 py-6"):
                _error_label(f"Could not load datasets: {e}")
            return

        all_rows = []
        for r in raw_rows:
            date_display, rel_time = _fmt_date(r.get("last_modified", ""))
            created_display, created_rel = _fmt_date(r.get("metadata_created", ""))
            qid = r.get("qid", "")
            versions_url = f"{settings.doip_url.rstrip('/')}/doip/versions/{qid}?include_sizes=true" if qid else ""
            all_rows.append({**r,
                "_date_display": date_display, "_rel_time": rel_time,
                "_created_display": created_display, "_created_rel": created_rel,
                "_versions_url": versions_url,
            })

        all_types = ["All Types", "Dataset"]
        filtered_rows: list = list(all_rows)
        current_search = [""]
        current_type   = ["All Types"]
        current_sort   = ["Recently Updated"]

        current_dataset = [None]
        current_name = [""]
        preview_busy = [False]
        current_page = [1]
        matching_rows = [0]
        applied_options = [PreviewOptions()]

        def apply_filters():
            """Refresh catalog rows after search, type, or sorting changes."""
            s  = current_search[0].lower()
            t  = current_type[0]
            rs = [
                r for r in all_rows
                if (not s or s in r["name"].lower() or s in r.get("description", "").lower())
                and (t == "All Types" or t == "Dataset")
            ]
            if current_sort[0] == "Recently Updated":
                rs.sort(key=lambda r: r.get("last_modified", ""), reverse=True)
            else:
                rs.sort(key=lambda r: r["name"].lower())
            filtered_rows.clear()
            filtered_rows.extend(rs)
            count_lbl.refresh()
            ds_table.refresh()

        @ui.refreshable
        def count_lbl():
            """Display the number of catalog datasets matching the controls."""
            n = len(filtered_rows)
            ui.label(f"Showing {n} dataset{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        preview_controls_row = [None]
        preview_controls = {}
        filter_controls = []
        filters_container = [None]
        data_container_ref = [None]

        def add_preview_filter():
            """Append a removable condition row for the currently loaded schema."""
            if len(filter_controls) >= MAX_FILTERS:
                ui.notify(f"Use at most {MAX_FILTERS} filters.")
                return
            names = current_dataset[0].table.column_names if current_dataset[0] is not None else []
            with filters_container[0]:
                with ui.row().classes("w-full items-end gap-2") as row:
                    controls = {
                        "column": ui.select(names, label="Filter column", clearable=True).classes("min-w-40"),
                        "operator": ui.select(list(FILTER_OPERATORS), value="=", label="Condition").classes("w-32"),
                        "value": ui.input(label="Filter value").classes("min-w-40"),
                    }

                    def remove_filter():
                        """Remove this condition row from the pending filter choices."""
                        filter_controls.remove(controls)
                        row.delete()

                    ui.button("Remove filter", icon="delete_outline", on_click=remove_filter).props("flat")
            filter_controls.append(controls)

        def render_preview(df, container):
            """Display a result page and update navigation controls.

            Args:
                df: Queried result page.
                container: NiceGUI container for the result table.
            """
            container.clear()
            with container:
                dataset = current_dataset[0]
                ui.label(f"{current_name[0]} — {matching_rows[0]:,} matching rows").classes("text-sm text-gray-500 mb-2")
                if dataset.table.num_rows < dataset.source_rows:
                    ui.label(
                        f"Filters and sorting cover the first {dataset.table.num_rows:,} of "
                        f"{dataset.source_rows:,} source rows. Download the component for the full dataset."
                    ).classes("text-sm text-gray-500")
                cols = [{"name": f"c{i}", "label": c, "field": f"c{i}", "align": "left"} for i, c in enumerate(df.columns)]
                # Use server-generated keys to support arbitrary schema names and duplicate values.
                display = df.astype(str)
                display.columns = [f"c{i}" for i in range(len(df.columns))]
                rows = display.to_dict("records")
                for i, row in enumerate(rows):
                    row["_preview_row"] = i
                ui.table(
                    columns=cols,
                    rows=rows,
                    row_key="_preview_row",
                ).classes("w-full")
                if df.empty:
                    ui.label("No rows match these filters.").classes("text-sm text-gray-500")
            size = applied_options[0].page_size
            pages = max(1, (matching_rows[0] + size - 1) // size)
            preview_controls["page_label"].set_text(f"Page {current_page[0]} of {pages}")
            preview_controls["previous"].set_enabled(current_page[0] > 1)
            preview_controls["next"].set_enabled(current_page[0] < pages)

        async def update_preview(page: int = 1, use_applied: bool = False):
            """Query the loaded sample using validated UI choices.

            Args:
                page: Requested result page.
                use_applied: Reuse the last applied filters for pagination.
            """
            if current_dataset[0] is None or preview_busy[0]:
                return
            preview_busy[0] = True
            try:
                if use_applied:
                    from dataclasses import replace
                    options = replace(applied_options[0], page=page)
                else:
                    options = PreviewOptions(
                        columns=tuple(preview_controls["columns"].value or []),
                        filters=tuple(
                            PreviewFilter(
                                column=controls["column"].value,
                                operator=controls["operator"].value,
                                value=controls["value"].value or "",
                            )
                            for controls in filter_controls if controls["column"].value is not None
                        ),
                        sort_column=preview_controls["sort"].value,
                        direction=preview_controls["direction"].value,
                        page=page,
                        page_size=preview_controls["size"].value,
                    )
                response = await run.io_bound(query_dataset, current_dataset[0], options)
                if response is None:
                    return
                result, total = response
                current_page[0], matching_rows[0] = page, total
                applied_options[0] = options
                render_preview(result, data_container_ref[0])
            except Exception:
                ui.notify("Could not apply the preview controls. Check the selected columns and filter value.", type="negative")
            finally:
                preview_busy[0] = False

        async def apply_preview():
            """Apply the current controls starting from the first page."""
            await update_preview()

        async def previous_page():
            """Display the previous page using the last applied controls."""
            if current_page[0] > 1:
                await update_preview(current_page[0] - 1, use_applied=True)

        async def next_page():
            """Display the next page using the last applied controls."""
            if current_page[0] * applied_options[0].page_size < matching_rows[0]:
                await update_preview(current_page[0] + 1, use_applied=True)

        @ui.refreshable
        def ds_table():
            """Render selectable catalog rows and their server-resolved previews."""
            if not filtered_rows:
                ui.label("No datasets match your search.").classes("text-sm text-gray-400 py-8 text-center w-full")
                return

            tbl = ui.table(
                columns=[
                    {"name": "name",    "label": "Name",     "field": "name",          "align": "left", "sortable": True},
                    {"name": "qid",     "label": "QID",      "field": "qid",           "align": "left"},
                    {"name": "updated",  "label": "Updated",  "field": "last_modified",    "align": "left", "sortable": True},
                    {"name": "created",  "label": "Created",  "field": "metadata_created", "align": "left", "sortable": True},
                    {"name": "type",     "label": "Type",     "field": "name",              "align": "left"},
                    {"name": "actions", "label": "",         "field": "qid",           "align": "right"},
                ],
                rows=filtered_rows,
                row_key="qid",
                selection="single",
                pagination={"rowsPerPage": 10, "sortBy": "last_modified", "descending": True},
            ).classes("w-full")

            tbl.add_slot("body-cell-name", r'''
                <q-td :props="props" style="max-width:320px">
                    <div class="font-semibold text-gray-800 text-sm">{{ props.row.name }}</div>
                    <div class="text-xs text-gray-500 mt-0.5" style="white-space:normal;line-height:1.3">{{ props.row.description }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-qid", r'''
                <q-td :props="props">
                    <span class="font-mono text-xs text-gray-600">{{ props.row.qid }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-updated", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._date_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._rel_time }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-created", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._created_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._created_rel }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-type", r'''
                <q-td :props="props">
                    <span :class="{
                        'bg-orange-100 text-orange-700': props.row.additional_type === 'incidence',
                        'bg-blue-100 text-blue-700':    props.row.additional_type === 'wastewater',
                        'bg-teal-100 text-teal-700':    props.row.additional_type === 'surveillance',
                        'bg-green-100 text-green-700':  props.row.additional_type === 'climate',
                        'bg-gray-100 text-gray-600':   !props.row.additional_type,
                    }" class="px-2 py-0.5 rounded-full text-xs font-medium">{{ props.row.additional_type || 'Dataset' }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                    <q-btn flat round dense icon="more_vert" size="sm" class="text-gray-400">
                        <q-menu>
                            <q-list dense>
                                <q-item clickable v-close-popup :href="'https://data.episerve.zib.de/dataset/' + props.row.qid.toLowerCase()" target="_blank">
                                    <q-item-section>Show in DataHub</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup :href="props.row.doip_url" target="_blank">
                                    <q-item-section>Show Metadata</q-item-section>
                                </q-item>
                                <q-item v-if="props.row._versions_url" clickable v-close-popup :href="props.row._versions_url" target="_blank">
                                    <q-item-section>Show all versions</q-item-section>
                                </q-item>
                                <q-item v-for="c in props.row.components" :key="c.name" clickable v-close-popup :href="c.url" target="_blank">
                                    <q-item-section>Download {{ c.name }}</q-item-section>
                                </q-item>
                            </q-list>
                        </q-menu>
                    </q-btn>
                </q-td>''')

            async def on_selection(e):
                """Load a component resolved from a selected catalog identifier.

                Args:
                    e: Browser selection event; only its dataset identifier is used.
                """
                if preview_busy[0]:
                    ui.notify("Wait for the current preview to finish before selecting another dataset.")
                    return
                selected = e.args.get("rows", []) if e.args.get("added", True) else []
                sr = preview_controls_row[0]
                dc = data_container_ref[0]
                if sr:
                    sr.set_visibility(False)
                if dc:
                    dc.clear()
                current_dataset[0] = None
                if not selected:
                    return
                preview_busy[0] = True
                try:
                    if len(selected) != 1 or not isinstance(selected[0], dict):
                        raise ValueError("Select one known dataset.")
                    component = resolve_component(all_rows, selected[0].get("qid"))
                    current_name[0] = component["name"]
                    if dc:
                        with dc:
                            ui.spinner(size="lg")
                    dataset = await run.io_bound(load_dataset, component["url"], settings.doip_url)
                    if dataset is None:
                        return
                    current_dataset[0] = dataset
                    names = dataset.table.column_names
                    preview_controls["columns"].set_options(names, value=list(names))
                    preview_controls["sort"].set_options(names, value=names[0])
                    filters_container[0].clear()
                    filter_controls.clear()
                    add_preview_filter()
                    preview_controls["direction"].value = "ASC"
                    preview_controls["size"].value = 100
                    if sr:
                        sr.set_visibility(True)
                except Exception:
                    if dc:
                        dc.clear()
                        with dc:
                            _error_label("Could not preview this component. It must be a valid Parquet file on the configured DOIP service within the preview size limits.")
                    return
                finally:
                    preview_busy[0] = False
                await update_preview()

            tbl.on("selection", on_selection)

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Datasets").classes("text-3xl font-bold text-gray-900")
                    ui.label("Browse, explore and download available datasets.").classes("text-sm text-gray-500 mt-1")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("flex-1 border border-gray-200 rounded-lg px-3 py-2 items-center gap-2 bg-white"):
                    ui.icon("search").classes("text-gray-400 shrink-0")
                    ui.input(
                        placeholder="Search datasets...",
                        on_change=lambda e: (current_search.__setitem__(0, e.value), apply_filters()),
                    ).props("borderless dense").classes("flex-1 text-sm")
                ui.select(
                    options=all_types,
                    value="All Types",
                    on_change=lambda e: (current_type.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")
                ui.select(
                    options=["Recently Updated", "Name A–Z"],
                    value="Recently Updated",
                    on_change=lambda e: (current_sort.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")

            count_lbl()

            with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-hidden"):
                ds_table()

            with ui.column().classes("w-full gap-3") as controls_row:
                with ui.row().classes("w-full items-end gap-2"):
                    preview_controls["columns"] = ui.select([], multiple=True, label="Columns").classes("min-w-48")
                ui.label("Match all filters (AND).").classes("text-sm text-gray-500")
                filters_container[0] = ui.column().classes("w-full gap-2")
                add_preview_filter()
                ui.button("Add filter", icon="add", on_click=add_preview_filter).props("flat")
                with ui.row().classes("w-full items-end gap-2"):
                    preview_controls["sort"] = ui.select([], label="Sort column").classes("min-w-40")
                    preview_controls["direction"] = ui.select({"ASC": "Ascending", "DESC": "Descending"}, value="ASC", label="Order")
                    preview_controls["size"] = ui.select(list(PAGE_SIZES), value=100, label="Rows per page")
                    ui.button("Apply", icon="filter_alt", on_click=apply_preview).classes("bg-blue-700 text-white")
                with ui.row().classes("items-center gap-2"):
                    preview_controls["previous"] = ui.button("Previous", on_click=previous_page)
                    preview_controls["page_label"] = ui.label("")
                    preview_controls["next"] = ui.button("Next", on_click=next_page)
            controls_row.set_visibility(False)
            preview_controls_row[0] = controls_row

            _dc = ui.column().classes("w-full")
            data_container_ref[0] = _dc

    @ui.page("/ui/models")
    def models(request: Request):
        _header("/ui/models", api="models")

        try:
            raw_rows = ckan_client.list_models()
        except Exception as e:
            with ui.column().classes("px-8 py-6"):
                _error_label(f"Could not load models: {e}")
            return

        def _fmt_date_model(dt_str: str) -> tuple:
            if not dt_str:
                return "—", ""
            try:
                from datetime import datetime as _dt
                import re
                s = dt_str.strip().rstrip("Z")
                # truncate sub-second part to 6 digits so %f works
                s = re.sub(r"(\.\d{6})\d+", r"\1", s)
                for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        d = _dt.strptime(s, fmt)
                        days = (_dt.now() - d).days
                        rel = "Today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"
                        return d.strftime("%b %-d, %Y"), rel
                    except ValueError:
                        pass
            except Exception:
                pass
            return dt_str, ""

        all_rows = []
        for r in raw_rows:
            created_display, created_rel = _fmt_date_model(r.get("docker_image_created", ""))
            all_rows.append({**r, "_created_display": created_display, "_created_rel": created_rel})

        filtered_rows: list = list(all_rows)
        current_search = [""]
        current_sort   = ["Name A–Z"]

        def apply_filters():
            s = current_search[0].lower()
            rs = [r for r in all_rows if not s or s in r["name"].lower() or s in r.get("description", "").lower() or s in r.get("qid", "").lower()]
            if current_sort[0] == "Name A–Z":
                rs.sort(key=lambda r: r["name"].lower())
            filtered_rows.clear()
            filtered_rows.extend(rs)
            count_lbl.refresh()
            model_table.refresh()

        @ui.refreshable
        def count_lbl():
            n = len(filtered_rows)
            ui.label(f"Showing {n} model{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        @ui.refreshable
        def model_table():
            if not filtered_rows:
                ui.label("No models match your search.").classes("text-sm text-gray-400 py-8 text-center w-full")
                return

            tbl = ui.table(
                columns=[
                    {"name": "name",          "label": "Name",          "field": "name",                 "align": "left", "sortable": True},
                    {"name": "qid",           "label": "QID",           "field": "qid",                  "align": "left"},
                    {"name": "docker_tag",    "label": "Tag",           "field": "docker_tag",           "align": "left", "sortable": True},
                    {"name": "docker_image",  "label": "Image",         "field": "docker_image",         "align": "left"},
                    {"name": "image_created", "label": "Image Created", "field": "docker_image_created", "align": "left", "sortable": True},
                    {"name": "actions",       "label": "",              "field": "name",                 "align": "right"},
                ],
                rows=filtered_rows,
                row_key="name",
                pagination={"rowsPerPage": 10},
            ).classes("w-full")

            tbl.add_slot("body-cell-name", r'''
                <q-td :props="props" style="max-width:320px">
                    <div class="font-semibold text-gray-800 text-sm">{{ props.row.name }}</div>
                    <div class="text-xs text-gray-500 mt-0.5" style="white-space:normal;line-height:1.3">{{ props.row.description }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-qid", r'''
                <q-td :props="props">
                    <span class="font-mono text-xs text-gray-600">{{ props.row.qid || '—' }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-docker_tag", r'''
                <q-td :props="props">
                    <span class="bg-blue-100 text-blue-700 text-xs px-2 py-0.5 rounded font-mono">{{ props.row.docker_tag }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-docker_image", r'''
                <q-td :props="props">
                    <span class="font-mono text-xs text-gray-600">{{ props.row.docker_image }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-image_created", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._created_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._created_rel }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                    <q-btn flat round dense icon="more_vert" size="sm" class="text-gray-400">
                        <q-menu>
                            <q-list dense>
                                <q-item clickable v-close-popup :href="'https://data.episerve.zib.de/dataset/' + props.row.qid.toLowerCase()" target="_blank">
                                    <q-item-section>Show in ModelHub</q-item-section>
                                </q-item>
                                <q-item v-if="props.row.doip_url" clickable v-close-popup :href="props.row.doip_url" target="_blank">
                                    <q-item-section>Show Metadata</q-item-section>
                                </q-item>
                                <q-item v-if="props.row.git_repo" clickable v-close-popup :href="props.row.git_repo" target="_blank">
                                    <q-item-section>Go to GitHub repo</q-item-section>
                                </q-item>
                            </q-list>
                        </q-menu>
                    </q-btn>
                </q-td>''')

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Models").classes("text-3xl font-bold text-gray-900")
                    ui.label("Browse available forecast models.").classes("text-sm text-gray-500 mt-1")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("flex-1 border border-gray-200 rounded-lg px-3 py-2 items-center gap-2 bg-white"):
                    ui.icon("search").classes("text-gray-400 shrink-0")
                    ui.input(
                        placeholder="Search by name, description or QID...",
                        on_change=lambda e: (current_search.__setitem__(0, e.value), apply_filters()),
                    ).props("borderless dense").classes("flex-1 text-sm")
                ui.select(
                    options=["Name A–Z"],
                    value="Name A–Z",
                    on_change=lambda e: (current_sort.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")

            count_lbl()

            with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-hidden"):
                model_table()

    @ui.page("/ui/model-runs")
    def model_runs(request: Request):
        """Render model runs using validated, bounded component previews.

        Args:
            request: Incoming request used to construct the API link.
        """
        from datetime import datetime
        _header("/ui/model-runs", api="model-runs")

        def _fmt_date(dt_str: str) -> tuple:
            if not dt_str:
                return "—", ""
            try:
                s = dt_str.strip()
                dt = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(s[:19], fmt)
                        break
                    except ValueError:
                        pass
                if dt is None:
                    dt = datetime.strptime(s[:10], "%Y-%m-%d")
                days = (datetime.now() - dt).days
                rel = "Today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"
                return dt.strftime("%b %-d, %Y  %H:%M"), rel
            except Exception:
                return dt_str, ""

        def _status_cls(status: str) -> str:
            s = (status or "").lower()
            if s in ("completed", "success"):   return "bg-green-100 text-green-700"
            if s in ("failed", "error"):        return "bg-red-100 text-red-700"
            if s in ("running", "in_progress"): return "bg-blue-100 text-blue-700"
            return "bg-gray-100 text-gray-600"

        try:
            raw_rows = ckan_client.list_model_runs()
        except Exception as e:
            with ui.column().classes("px-8 py-6"):
                _error_label(f"Could not load model runs: {e}")
            return

        try:
            dataset_map = {ds["qid"]: ds["name"] for ds in ckan_client.list_processed_datasets()}
        except Exception:
            dataset_map = {}

        def _resolve_inputs(qids: list) -> list[str]:
            return [dataset_map.get(q) or q for q in qids]

        all_rows = []
        for r in raw_rows:
            date_display, rel_time = _fmt_date(r.get("run_timestamp", ""))
            status = r.get("status", "") or (
                "completed" if r.get("output_files") else ""
            )
            log_url = next((u for u in r.get("output_files", []) if "run.log" in u), "")
            if not log_url and r.get("qid"):
                log_url = f"{settings.doip_url.rstrip('/')}/doip/retrieve/{r['qid']}/output/run.log"
            all_rows.append({**r,
                "status": status,
                "_date_display": date_display, "_rel_time": rel_time,
                "_status_cls": _status_cls(status),
                "_input_names": _resolve_inputs(r.get("input_dataset_qids", [])),
                "_log_url": log_url,
            })

        filtered_rows: list = list(all_rows)
        current_search = [""]
        current_sort   = ["Recently Run"]

        def apply_filters():
            s = current_search[0].lower()
            rs = [r for r in all_rows if not s or s in r.get("model_name", "").lower() or s in r.get("qid", "").lower()]
            if current_sort[0] == "Recently Run":
                rs.sort(key=lambda r: r.get("run_timestamp", ""), reverse=True)
            else:
                rs.sort(key=lambda r: r.get("model_name", "").lower())
            filtered_rows.clear()
            filtered_rows.extend(rs)
            count_lbl.refresh()
            runs_table.refresh()

        @ui.refreshable
        def count_lbl():
            n = len(filtered_rows)
            ui.label(f"Showing {n} run{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        @ui.refreshable
        def runs_table():
            if not filtered_rows:
                ui.label("No model runs match your search.").classes("text-sm text-gray-400 py-8 text-center w-full")
                return

            tbl = ui.table(
                columns=[
                    {"name": "model_name",    "label": "Model",    "field": "model_name",    "align": "left", "sortable": True},
                    {"name": "qid",           "label": "QID",      "field": "qid",           "align": "left"},
                    {"name": "input_datasets","label": "Datasets", "field": "_input_names",  "align": "left"},
                    {"name": "run_timestamp", "label": "Run",      "field": "run_timestamp", "align": "left", "sortable": True},
                    {"name": "executed_by",    "label": "Executed by", "field": "executed_by",  "align": "left"},
                    {"name": "status",        "label": "Status",   "field": "status",        "align": "left"},
                    {"name": "actions",       "label": "",         "field": "qid",           "align": "right"},
                ],
                rows=filtered_rows,
                row_key="qid",
                selection="single",
                pagination={"rowsPerPage": 10, "sortBy": "run_timestamp", "descending": True},
            ).classes("w-full")

            tbl.add_slot("body-cell-model_name", r'''
                <q-td :props="props">
                    <div class="font-semibold text-gray-800 text-sm">{{ props.row.model_name }}</div>
                    <span class="bg-blue-100 text-blue-700 text-xs px-2 py-0.5 rounded font-mono">{{ props.row.docker_tag }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-qid", r'''
                <q-td :props="props">
                    <span class="font-mono text-xs text-gray-600">{{ props.row.qid }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-input_datasets", r'''
                <q-td :props="props">
                    <div v-if="props.row._input_names && props.row._input_names.length">
                        <div v-for="name in props.row._input_names" :key="name" class="text-xs text-gray-700 leading-snug">{{ name }}</div>
                    </div>
                    <span v-else class="text-xs text-gray-400">—</span>
                </q-td>''')

            tbl.add_slot("body-cell-run_timestamp", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._date_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._rel_time }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-status", r'''
                <q-td :props="props">
                    <span v-if="props.row.status" :class="'px-2 py-0.5 rounded-full text-xs font-medium ' + props.row._status_cls">{{ props.row.status }}</span>
                    <span v-else class="text-xs text-gray-400">—</span>
                </q-td>''')

            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                    <q-btn flat round dense icon="more_vert" size="sm" class="text-gray-400">
                        <q-menu>
                            <q-list dense>
                                <q-item clickable v-close-popup :href="'https://data.episerve.zib.de/dataset/' + props.row.qid.toLowerCase()" target="_blank">
                                    <q-item-section>Show in DataHub</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup :href="props.row.doip_url" target="_blank">
                                    <q-item-section>Show Metadata</q-item-section>
                                </q-item>
                                <q-item v-if="props.row._log_url" clickable v-close-popup :href="props.row._log_url" target="_blank">
                                    <q-item-section>Show log</q-item-section>
                                </q-item>
                            </q-list>
                        </q-menu>
                    </q-btn>
                </q-td>''')

            async def on_selection(e):
                """Resolve a selected run and preview its saved components safely.

                Args:
                    e: Browser selection event; only its run identifier is used.
                """
                preview_generation[0] += 1
                generation = preview_generation[0]
                selected = e.args.get("rows", []) if e.args.get("added", True) else []
                preview_container[0].clear()
                if not selected:
                    return
                try:
                    if len(selected) != 1 or not isinstance(selected[0], dict):
                        raise ValueError("Select one known model run.")
                    row = resolve_model_run(all_rows, selected[0].get("qid"))
                except ValueError:
                    with preview_container[0]:
                        _error_label("Select one known model run.")
                    return

                dc = preview_container[0]
                with dc:
                    with ui.row().classes("items-center gap-2"):
                        ui.spinner(size="sm")
                        ui.label("Fetching metadata…").classes("text-sm text-gray-400")

                def _preview_card(dc, icon: str, label: str, df):
                    """Display a safely loaded component and its source row count.

                    Args:
                        dc: Parent container for the card.
                        icon: Card icon name.
                        label: Component label identifying saved or original input.
                        df: Bounded preview dataframe.
                    """
                    total = df.attrs.get("total_rows")
                    if total is None:
                        row_lbl = f"{len(df):,} rows"
                    elif total > len(df):
                        row_lbl = f"{len(df):,} of {total:,} rows"
                    else:
                        row_lbl = f"{total:,} rows"
                    with dc:
                        with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-hidden"):
                            with ui.row().classes("px-5 py-3 border-b border-gray-100 items-center gap-2"):
                                ui.icon(icon).classes("text-blue-600")
                                ui.label(label).classes("text-sm font-semibold text-gray-800")
                                ui.label(f"{row_lbl} × {len(df.columns)} columns").classes("text-xs text-gray-400 ml-2")
                            cols = [{"name": c, "label": c, "field": c, "align": "left", "sortable": True} for c in df.columns]
                            ui.table(
                                columns=cols,
                                rows=df.astype(str).to_dict("records"),
                                row_key=df.columns[0],
                                pagination={"rowsPerPage": 10},
                            ).classes("w-full")

                try:
                    meta = await run.io_bound(fetch_run_metadata, row["qid"], settings.doip_url)
                    if generation != preview_generation[0] or meta is None:
                        return
                    components = find_run_components(meta, row["qid"], settings.doip_url)
                except Exception:
                    if generation != preview_generation[0]:
                        return
                    dc.clear()
                    with dc:
                        _error_label("Could not load valid model-run metadata and approved component URLs.")
                    return

                dc.clear()
                if not any(not component.is_input and "predictions" in component.label.lower() for component in components):
                    with dc:
                        ui.label("No predictions component found in metadata.").classes("text-sm text-gray-400 italic")
                    return

                base = settings.doip_url.rstrip("/")

                with dc:
                    with ui.row().classes("items-center gap-2"):
                        ui.spinner(size="sm")
                        ui.label("Loading files…").classes("text-sm text-gray-400")

                cur_limit = [500]
                cards_container = [None]

                async def load_previews(limit: int):
                    """Load the same validated components for initial and resized previews.

                    Args:
                        limit: Maximum displayed rows per component.

                    Returns:
                        Component labels and safely loaded dataframes.
                    """
                    results = []
                    for component in components:
                        if generation != preview_generation[0]:
                            return []
                        try:
                            frame = await run.io_bound(fetch_component_preview, component, base, limit)
                            if generation != preview_generation[0]:
                                return []
                            if frame is not None:
                                results.append((component.label, frame))
                        except Exception:
                            if generation != preview_generation[0]:
                                return []
                            with dc:
                                _error_label(f"Could not preview {component.label} within the size and format limits.")
                    return results

                loaded = await load_previews(cur_limit[0])
                if generation != preview_generation[0]:
                    return

                if not any("predictions" in cid.lower() for cid, _ in loaded):
                    dc.clear()
                    with dc:
                        ui.label("No predictions could be loaded.").classes("text-sm text-gray-400 italic")
                    return

                # Build col_map: "filename: column" -> (df, col)
                col_map: dict = {}
                for cid, df in loaded:
                    for col in df.columns:
                        col_map[f"{cid}: {col}"] = (df, col)

                opt_labels = list(col_map.keys())

                # Default x to x_auto_converted if present, else first column
                x_auto_key = next((k for k in opt_labels if k.endswith(": x_auto_converted")), None)
                cur_x = [x_auto_key or (opt_labels[0] if opt_labels else "")]

                def _companion_label_col(x_key: str) -> str | None:
                    """When x is x_auto_converted, return the first non-auto column of the same file."""
                    if not x_key or not x_key.endswith(": x_auto_converted"):
                        return None
                    prefix = x_key[: x_key.rfind(": x_auto_converted")]
                    return next((k for k in opt_labels if k.startswith(f"{prefix}: ") and not k.endswith(": x_auto_converted")), None)

                cur_xlabel = [_companion_label_col(cur_x[0])]

                # Default y to first non-x column of the predictions file
                pred_cid = next((cid for cid, _ in loaded if "predictions" in cid.lower()), "")
                pred_y_opts = [k for k in opt_labels if k.startswith(f"{pred_cid}: ") and not k.endswith(": x_auto_converted")]
                cur_ys = [[pred_y_opts[0]] if pred_y_opts else ([opt_labels[0]] if opt_labels else [])]

                dc.clear()
                with dc:
                    provenance = meta.get("provenance", {})
                    entries = provenance.get("prov:used", []) if isinstance(provenance, dict) else []
                    recorded_queries = [
                        entry.get("schema:query") for entry in entries[:20]
                        if isinstance(entry, dict) and isinstance(entry.get("schema:query"), str)
                    ] if isinstance(entries, list) else []
                    if recorded_queries:
                        with ui.expansion("Recorded transformations (provenance)").classes("w-full"):
                            for recorded_query in recorded_queries:
                                ui.code(recorded_query[:4096], language="sql").classes("w-full")
                    with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white p-5"):
                        ui.label("Chart").classes("text-base font-bold text-gray-900 mb-1")

                        chart = ui.echart({
                            "tooltip": {"trigger": "axis"},
                            "legend": {"top": 24},
                            "xAxis":   {"type": "value"},
                            "yAxis":   {"type": "value"},
                            "series":  [],
                            "dataZoom": [{"type": "inside"}, {"type": "slider", "height": 20}],
                            "grid": {"left": "3%", "right": "4%", "bottom": "15%", "top": "60px", "containLabel": True},
                        }).classes("w-full h-72")

                        def update_chart():
                            xl = cur_x[0]
                            yl_list = cur_ys[0]
                            if not xl or xl not in col_map or not yl_list:
                                return
                            x_df_ref, x_col_name = col_map[xl]

                            # Detect numeric x → value axis with [x, y] pairs (allows different-length series)
                            try:
                                pd.to_numeric(x_df_ref[x_col_name])
                                numeric_x = True
                            except (ValueError, TypeError):
                                numeric_x = False

                            series = []
                            xlabel_key = cur_xlabel[0]
                            use_labels = (
                                numeric_x
                                and xlabel_key
                                and xlabel_key in col_map
                                and xlabel_key != xl
                            )

                            if use_labels:
                                # Build a unified position → label mapping from the label column
                                lbl_df, lbl_col = col_map[xlabel_key]
                                x_pos_vals = pd.to_numeric(x_df_ref[x_col_name], errors="coerce").tolist()
                                lbl_vals = lbl_df[lbl_col].astype(str).tolist()
                                pos_to_label = {int(p): l for p, l in zip(x_pos_vals, lbl_vals) if not pd.isna(p)}

                                # Collect all x positions across all series
                                all_positions: set[int] = set(pos_to_label.keys())
                                for yl in yl_list:
                                    if yl not in col_map:
                                        continue
                                    y_df, y_col = col_map[yl]
                                    x_src = y_df if x_col_name in y_df.columns else x_df_ref
                                    for p in pd.to_numeric(x_src[x_col_name], errors="coerce").tolist():
                                        if not pd.isna(p):
                                            all_positions.add(int(p))

                                sorted_pos = sorted(all_positions)
                                categories = [pos_to_label.get(p, str(p)) for p in sorted_pos]
                                pos_to_idx = {p: i for i, p in enumerate(sorted_pos)}

                                for yl in yl_list:
                                    if yl not in col_map:
                                        continue
                                    y_df, y_col = col_map[yl]
                                    x_src = y_df if x_col_name in y_df.columns else x_df_ref
                                    x_positions = pd.to_numeric(x_src[x_col_name], errors="coerce").tolist()
                                    y_vals = y_df[y_col].tolist()
                                    sparse = [None] * len(categories)
                                    for p, v in zip(x_positions, y_vals):
                                        if pd.isna(p):
                                            continue
                                        idx = pos_to_idx.get(int(p))
                                        if idx is not None:
                                            sparse[idx] = None if str(v) in ("nan", "None", "") else v
                                    series.append({"name": yl, "type": "line", "smooth": True, "data": sparse, "connectNulls": False})
                                chart.options["xAxis"] = {"type": "category", "data": categories}

                            elif numeric_x:
                                for yl in yl_list:
                                    if yl not in col_map:
                                        continue
                                    y_df, y_col = col_map[yl]
                                    x_src = y_df if x_col_name in y_df.columns else x_df_ref
                                    x_vals = pd.to_numeric(x_src[x_col_name], errors="coerce").tolist()
                                    y_vals = y_df[y_col].tolist()
                                    n = min(len(x_vals), len(y_vals))
                                    data = [
                                        [x_vals[i], None if str(y_vals[i]) in ("nan", "None", "") else y_vals[i]]
                                        for i in range(n)
                                    ]
                                    series.append({"name": yl, "type": "line", "smooth": True, "data": data})
                                chart.options["xAxis"] = {"type": "value"}
                            else:
                                # Category axis: positional, truncate to shortest series
                                x_vals = x_df_ref[x_col_name].astype(str).tolist()
                                for yl in yl_list:
                                    if yl not in col_map:
                                        continue
                                    y_df, y_col = col_map[yl]
                                    y_vals = y_df[y_col].tolist()
                                    n = min(len(x_vals), len(y_vals))
                                    data = [None if str(y_vals[i]) in ("nan", "None", "") else y_vals[i] for i in range(n)]
                                    series.append({"name": yl, "type": "line", "smooth": True, "data": data})
                                chart.options["xAxis"] = {"type": "category", "data": x_vals}
                            chart.options["series"] = series
                            chart.update()

                        def on_x(e):
                            cur_x[0] = e.value
                            cur_xlabel[0] = _companion_label_col(e.value)
                            if xlabel_sel:
                                xlabel_sel.value = cur_xlabel[0]
                            update_chart()
                        def on_xlabel(e): cur_xlabel[0] = e.value; update_chart()
                        def on_y(e): cur_ys[0] = e.value if isinstance(e.value, list) else [e.value]; update_chart()
                        xlabel_sel = None

                        async def on_limit(e):
                            """Reload approved components with a bounded display row limit.

                            Args:
                                e: Row-limit control change event.
                            """
                            try:
                                v = int(e.value or 0)
                            except (ValueError, TypeError, OverflowError):
                                return
                            if not 1 <= v <= MAX_PREVIEW_ROWS or v == cur_limit[0] or generation != preview_generation[0]:
                                return
                            cur_limit[0] = v
                            new_loaded = await load_previews(v)
                            if generation != preview_generation[0] or v != cur_limit[0] or cards_container[0] is None:
                                return
                            loaded.clear()
                            loaded.extend(new_loaded)
                            col_map.clear()
                            for cid2, df2 in loaded:
                                for col in df2.columns:
                                    col_map[f"{cid2}: {col}"] = (df2, col)
                            cards_container[0].clear()
                            with cards_container[0]:
                                for cid2, df2 in loaded:
                                    icon2 = "table_chart" if "predictions" in cid2.lower() else "input"
                                    _preview_card(cards_container[0], icon2, cid2, df2)
                            update_chart()

                        with ui.row().classes("gap-3 mb-4 items-end flex-wrap"):
                            x_sel = ui.select(opt_labels, value=cur_x[0], label="X axis", on_change=on_x).classes("min-w-48")
                            y_sel = ui.select(opt_labels, value=cur_ys[0], label="Y axis (multi)", multiple=True, on_change=on_y).classes("min-w-48")
                            xlabel_sel = ui.select(
                                [None] + opt_labels,
                                value=cur_xlabel[0],
                                label="X labels",
                                on_change=on_xlabel,
                            ).classes("min-w-48")
                            y_sel.add_slot("option", r'''
                                <q-item v-bind="props.itemProps">
                                    <q-item-section side>
                                        <q-checkbox :model-value="props.selected" @update:model-value="props.toggleOption(props.opt)" />
                                    </q-item-section>
                                    <q-item-section>
                                        <q-item-label>{{ props.opt.label ?? props.opt }}</q-item-label>
                                    </q-item-section>
                                </q-item>
                            ''')
                            ui.number(label="Max rows", value=cur_limit[0], min=1, max=MAX_PREVIEW_ROWS, step=100,
                                      on_change=on_limit).classes("w-28")

                        update_chart()

                    cards_div = ui.element("div").classes("w-full flex flex-col gap-4")
                    cards_container[0] = cards_div
                    with cards_div:
                        for cid, df in loaded:
                            icon = "table_chart" if "predictions" in cid.lower() else "input"
                            _preview_card(cards_div, icon, cid, df)

            tbl.on("selection", on_selection)

        preview_container = [None]
        preview_generation = [0]

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Model Runs").classes("text-3xl font-bold text-gray-900")
                    ui.label("Browse past model run results.").classes("text-sm text-gray-500 mt-1")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("flex-1 border border-gray-200 rounded-lg px-3 py-2 items-center gap-2 bg-white"):
                    ui.icon("search").classes("text-gray-400 shrink-0")
                    ui.input(
                        placeholder="Search by model or QID...",
                        on_change=lambda e: (current_search.__setitem__(0, e.value), apply_filters()),
                    ).props("borderless dense").classes("flex-1 text-sm")
                ui.select(
                    options=["Recently Run", "Model A–Z"],
                    value="Recently Run",
                    on_change=lambda e: (current_sort.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")

            count_lbl()

            with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-hidden"):
                runs_table()

            preview_container[0] = ui.column().classes("w-full")

    from app.ui.leaderboard_page import register_leaderboard_page

    register_leaderboard_page()

    from app.ui.benchmarks_page import register_benchmarks_page

    register_benchmarks_page()

    @ui.page("/ui/blackboard")
    def blackboard(request: Request):
        from datetime import datetime

        if not _require_login("blackboard"):
            return
        _header("/ui/blackboard")

        def _fmt_date(dt) -> tuple:
            if not dt:
                return "—", ""
            try:
                s = str(dt).strip()
                d = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        d = datetime.strptime(s[:19], fmt)
                        break
                    except ValueError:
                        pass
                if d is None:
                    d = datetime.strptime(s[:10], "%Y-%m-%d")
                # Calendar-day difference, not elapsed 24h periods -- otherwise
                # anything <24h old (e.g. yesterday evening) shows as "Today".
                days = (datetime.now().date() - d.date()).days
                if days <= 0:
                    rel = "Today"
                elif days == 1:
                    rel = "Yesterday"
                else:
                    rel = f"{days} days ago"
                return d.strftime("%b %-d, %Y  %H:%M"), rel
            except Exception:
                return str(dt), ""

        def _state_cls(state: str) -> str:
            s = (state or "").lower()
            if s == "waiting":                       return "bg-blue-100 text-blue-700"
            if s == "dispatching_run":               return "bg-orange-100 text-orange-700"
            if s == "running":                       return "bg-amber-100 text-amber-700"
            if s == "waiting_for_next_periodic_run": return "bg-purple-100 text-purple-700"
            if s == "resolved":                      return "bg-green-100 text-green-700"
            if s == "failed":                        return "bg-red-100 text-red-700"
            return "bg-gray-100 text-gray-600"

        try:
            raw_rows = blackboard_client.list_task_runs()
        except Exception as e:
            with ui.column().classes("px-8 py-6"):
                _error_label(f"Could not load blackboard: {e}")
            return

        all_rows = []
        for r in raw_rows:
            date_display, rel_time = _fmt_date(r.get("created_at"))
            state_change_display, state_change_rel = _fmt_date(r.get("last_state_change"))
            periodic_last_display, periodic_last_rel = _fmt_date(r.get("periodic_last_triggered_at"))
            interval = r.get("periodic_interval_minutes")
            if interval:
                schedule_display = f"periodic · every {interval}min"
            elif r.get("post_type") == "run_me":
                schedule_display = "once"
            else:
                schedule_display = "—"
            fid = r.get("triggered_flow_run_id")
            prefect_url = (
                f"{settings.prefect_ui_url}/runs/flow-run/{fid}"
                if fid and settings.prefect_ui_url else ""
            )
            all_rows.append({**r,
                "_date_display": date_display, "_rel_time": rel_time,
                "_state_change_display": state_change_display, "_state_change_rel": state_change_rel,
                "_periodic_last_display": periodic_last_display, "_periodic_last_rel": periodic_last_rel,
                "_schedule_display": schedule_display,
                "_state_cls": _state_cls(r.get("state", "")),
                "_has_trace": bool(r.get("has_trace")),
                "_prefect_url": prefect_url,
            })

        filtered_rows: list = list(all_rows)
        current_search = [""]
        current_state = ["All States"]
        current_sort   = ["Newest First"]

        def apply_filters():
            s = current_search[0].lower()
            st = current_state[0]
            rs = [
                r for r in all_rows
                if (not s
                    or s in (r.get("topic") or "").lower()
                    or s in (r.get("finding") or "").lower()
                    or s in (r.get("prompt") or "").lower())
                and (st == "All States" or r.get("state") == st)
            ]
            if current_sort[0] == "Newest First":
                rs.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
            else:
                rs.sort(key=lambda r: (r.get("topic") or "").lower())
            filtered_rows.clear()
            filtered_rows.extend(rs)
            count_lbl.refresh()
            bb_table.refresh()

        @ui.refreshable
        def count_lbl():
            n = len(filtered_rows)
            ui.label(f"Showing {n} row{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        detail_dialog = ui.dialog()
        detail_title = [None]
        detail_finding_raw = [None]
        detail_finding_md = [None]
        detail_trace_row = [None]
        detail_trace_iframe = [None]
        current_finding_text = [""]

        def toggle_finding_view(e):
            if e.value:
                detail_finding_md[0].set_content(current_finding_text[0])
                detail_finding_raw[0].set_visibility(False)
                detail_finding_md[0].set_visibility(True)
            else:
                detail_finding_raw[0].set_visibility(True)
                detail_finding_md[0].set_visibility(False)

        detail_prompt_section = [None]
        detail_prompt_label = [None]
        detail_finding_section = [None]

        with detail_dialog, ui.card().classes("w-full max-w-5xl p-6 gap-3"):
            detail_title[0] = ui.label("").classes("text-lg font-bold text-gray-900")

            # Each section opens independently -- see open_detail's `focus`
            # arg and the table's promptClick/findingClick/traceClick handlers.
            detail_prompt_section[0] = ui.column().classes("w-full gap-1")
            with detail_prompt_section[0]:
                ui.label("Prompt").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mt-2")
                with ui.element("div").classes("w-full max-h-64 overflow-auto bg-gray-50 border border-gray-200 rounded-lg p-3"):
                    detail_prompt_label[0] = ui.label("").classes("whitespace-pre-wrap font-mono text-xs text-gray-800")

            detail_finding_section[0] = ui.column().classes("w-full gap-1")
            with detail_finding_section[0]:
                with ui.row().classes("w-full items-center justify-between mt-2"):
                    ui.label("Finding").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide")
                    ui.switch("Render as Markdown", on_change=toggle_finding_view).props("dense").classes("text-xs")
                with ui.element("div").classes("w-full max-h-64 overflow-auto bg-gray-50 border border-gray-200 rounded-lg p-3"):
                    detail_finding_raw[0] = ui.label("").classes("whitespace-pre-wrap font-mono text-xs text-gray-800")
                    # sanitize=True (default) runs the rendered HTML through
                    # client-side DOMPurify before insertion -- safe even though
                    # `finding` is agent-produced, not hand-authored, content.
                    detail_finding_md[0] = ui.markdown("").classes("text-sm")
                    detail_finding_md[0].set_visibility(False)

            detail_trace_row[0] = ui.column().classes("w-full gap-1")
            with detail_trace_row[0]:
                ui.label("Trace").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mt-2")
                # Rendered, not dumped as raw markup -- trace.html is a
                # self-contained interactive viewer (pi-trace-extension), so
                # showing its source as text would defeat the point of it.
                # Sandboxed with scripts allowed but same-origin/top-navigation
                # denied: the viewer's own JS (collapsible tree, tabs) still
                # works, but it can't reach this app's cookies/storage or
                # navigate the parent page -- this is agent-produced content,
                # not fully trusted.
                detail_trace_iframe[0] = ui.element("iframe").props('sandbox="allow-scripts"').classes(
                    "w-full border border-gray-200 rounded-lg"
                ).style("height: 70vh;")
            ui.button("Close", on_click=detail_dialog.close).classes("mt-2 self-end").props("flat")

        def open_detail(row: dict, focus: str = "finding"):
            """Open the detail dialog showing only the section that was clicked.

            Args:
                row: The clicked row.
                focus: 'prompt', 'finding', or 'trace' -- shows only that
                    section, hides the other two. Does nothing for
                    focus='trace' if the row has no trace.
            """
            if focus == "trace" and not row.get("_has_trace"):
                return

            title_type = row.get("topic") or row.get("post_type") or ""
            detail_title[0].set_text(f"#{row.get('id')} · {title_type}")

            detail_prompt_section[0].set_visibility(focus == "prompt")
            detail_finding_section[0].set_visibility(focus == "finding")
            detail_trace_row[0].set_visibility(focus == "trace")

            if focus == "prompt":
                detail_prompt_label[0].set_text(row.get("prompt") or "(empty)")
                detail_trace_iframe[0].props('src="about:blank"')
            elif focus == "finding":
                current_finding_text[0] = row.get("finding") or "(empty)"
                detail_finding_raw[0].set_text(current_finding_text[0])
                if detail_finding_md[0].visible:
                    detail_finding_md[0].set_content(current_finding_text[0])
                detail_trace_iframe[0].props('src="about:blank"')
            else:
                # A normal iframe `src` navigation -- the browser fetches this
                # over plain HTTP, independent of NiceGUI's websocket RPC
                # channel, so a multi-megabyte trace.html doesn't hit the
                # websocket's per-message size limit (see get_trace_html).
                detail_trace_iframe[0].props(f'src="/ui/blackboard/trace/{row.get("id")}"')

            detail_dialog.open()

        add_task_dialog = ui.dialog()
        add_task_error = [None]

        def toggle_interval_visibility(e):
            add_task_interval[0].set_visibility(e.value == "periodic")

        with add_task_dialog, ui.card().classes("w-full max-w-xl p-6 gap-3"):
            ui.label("Add AI Task to Blackboard").classes("text-lg font-bold text-gray-900")
            ui.label(
                "Inserts a post_type='run_me' row -- a task that isn't a follow-up to "
                "any finding. The orchestrator picks it up on its next poll."
            ).classes("text-xs text-gray-500")
            add_topic = ui.input(
                label="Topic (optional)",
                placeholder="e.g. code-analysis-report",
            ).classes("w-full").props("outlined dense")
            add_task_prompt = ui.textarea(
                label="Prompt",
                placeholder='e.g. Clone <repo-url>, analyse it and write a report to /output/report.pdf.',
            ).classes("w-full").props("outlined dense")
            add_task_schedule = ui.select(
                options=["once", "periodic"],
                value="once",
                label="Schedule",
                on_change=toggle_interval_visibility,
            ).classes("w-full").props("outlined dense options-dense")
            add_task_interval = [ui.number(
                label="Interval (minutes)",
                min=1,
                value=1440,
            ).classes("w-full").props("outlined dense")]
            add_task_interval[0].set_visibility(False)
            add_task_error[0] = ui.label("").classes("text-xs text-red-600")

            def submit_add_task():
                prompt_text = (add_task_prompt.value or "").strip()
                if not prompt_text:
                    add_task_error[0].set_text("Prompt is required.")
                    return
                interval = int(add_task_interval[0].value) if add_task_schedule.value == "periodic" else None
                topic = (add_topic.value or "").strip() or None
                try:
                    new_id = blackboard_client.insert_initial_task(prompt_text, interval, topic)
                except Exception as e:
                    add_task_error[0].set_text(f"Could not add task: {e}")
                    return
                ui.notify(f"Added task #{new_id}", type="positive")
                add_task_dialog.close()
                ui.navigate.to("/ui/blackboard")

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Cancel", on_click=add_task_dialog.close).props("flat")
                ui.button("Add Task", on_click=submit_add_task).props("unelevated color=primary")

        @ui.refreshable
        def bb_table():
            if not filtered_rows:
                ui.label("No blackboard rows match your search.").classes("text-sm text-gray-400 py-8 text-center w-full")
                return

            tbl = ui.table(
                columns=[
                    {"name": "actions",     "label": "",                    "field": "id",                         "align": "right"},
                    {"name": "id",          "label": "ID",                  "field": "id",                         "align": "left"},
                    {"name": "post_type",   "label": "Post Type",           "field": "post_type",                  "align": "left", "sortable": True},
                    {"name": "state",       "label": "State",               "field": "state",                      "align": "left"},
                    {"name": "topic",   "label": "Topic",               "field": "topic",                  "align": "left", "sortable": True},
                    {"name": "schedule",    "label": "Schedule",            "field": "_schedule_display",          "align": "left"},
                    {"name": "trace",       "label": "Trace",               "field": "_has_trace",                 "align": "left"},
                    {"name": "created",     "label": "Created",             "field": "created_at",                 "align": "left", "sortable": True},
                    {"name": "changed",     "label": "Last State Change",   "field": "last_state_change",          "align": "left"},
                    {"name": "periodic_last", "label": "Periodic Last Run", "field": "periodic_last_triggered_at", "align": "left"},
                ],
                rows=filtered_rows,
                row_key="id",
                pagination={"rowsPerPage": 15, "sortBy": "created_at", "descending": True},
            ).classes("w-full")

            tbl.add_slot("body-cell-post_type", r'''
                <q-td :props="props">
                    <span class="text-xs px-2 py-0.5 rounded-full font-medium"
                          :class="props.row.post_type === 'run_me' ? 'bg-indigo-100 text-indigo-700' : 'bg-teal-100 text-teal-700'">
                        {{ props.row.post_type }}
                    </span>
                </q-td>''')

            tbl.add_slot("body-cell-state", r'''
                <q-td :props="props">
                    <span :class="'px-2 py-0.5 rounded-full text-xs font-medium ' + props.row._state_cls">{{ props.row.state }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-trace", r'''
                <q-td :props="props">
                    <span v-if="props.row._has_trace"
                          class="bg-blue-100 text-blue-700 text-xs px-2 py-0.5 rounded-full font-medium cursor-pointer hover:bg-blue-200"
                          @click="$parent.$emit('traceClick', props.row)">available</span>
                    <span v-else class="text-xs text-gray-400">—</span>
                </q-td>''')

            tbl.add_slot("body-cell-created", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._date_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._rel_time }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-changed", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._state_change_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._state_change_rel }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-periodic_last", r'''
                <q-td :props="props">
                    <div v-if="props.row.periodic_last_triggered_at" class="text-sm text-gray-700">{{ props.row._periodic_last_display }}</div>
                    <div v-if="props.row.periodic_last_triggered_at" class="text-xs text-gray-400">{{ props.row._periodic_last_rel }}</div>
                    <span v-else class="text-xs text-gray-400">—</span>
                </q-td>''')

            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                    <q-btn flat round dense icon="more_vert" size="sm" class="text-gray-400">
                        <q-menu>
                            <q-list dense>
                                <q-item clickable v-close-popup @click="$parent.$emit('promptClick', props.row)">
                                    <q-item-section>Show prompt</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup @click="$parent.$emit('findingClick', props.row)">
                                    <q-item-section>Show finding</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup
                                        :href="props.row._prefect_url || undefined"
                                        :target="props.row._prefect_url ? '_blank' : undefined"
                                        @click="!props.row._prefect_url && $parent.$emit('prefectClick', props.row)">
                                    <q-item-section>Open Prefect run</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup @click="$parent.$emit('setWaitingClick', props.row)">
                                    <q-item-section>Set to waiting (new)</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup @click="$parent.$emit('setDismissedClick', props.row)">
                                    <q-item-section>Dismiss</q-item-section>
                                </q-item>
                            </q-list>
                        </q-menu>
                    </q-btn>
                </q-td>''')

            def on_prompt_click(e):
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    open_detail(row, focus="prompt")

            def on_finding_click(e):
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    open_detail(row, focus="finding")

            def on_trace_click(e):
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    open_detail(row, focus="trace")

            def on_prefect_click(e):
                # Only fires for rows with no triggered run -- rows that have
                # one open the Prefect UI directly via the menu item's own
                # href (see the body-cell-actions slot).
                ui.notify("No Prefect run recorded for this row yet.", type="info")

            def _set_state(row: dict, state: str):
                try:
                    blackboard_client.set_state(row["id"], state)
                except Exception as e:
                    ui.notify(f"Could not update task_runs.id={row['id']}: {e}", type="negative")
                    return
                ui.notify(f"task_runs.id={row['id']} set to '{state}'", type="positive")
                ui.navigate.to("/ui/blackboard")

            def on_set_waiting_click(e):
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    _set_state(row, "waiting")

            def on_set_dismissed_click(e):
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    _set_state(row, "dismissed")

            tbl.on("promptClick", on_prompt_click)
            tbl.on("findingClick", on_finding_click)
            tbl.on("traceClick", on_trace_click)
            tbl.on("prefectClick", on_prefect_click)
            tbl.on("setWaitingClick", on_set_waiting_click)
            tbl.on("setDismissedClick", on_set_dismissed_click)

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("AI Blackboard").classes("text-3xl font-bold text-gray-900")
                    ui.label("Seeded/recurring tasks and results published by one-shot agent runs, and the follow-up runs triggered from them.").classes("text-sm text-gray-500 mt-1")
                with ui.row().classes("items-center gap-2"):
                    def run_orchestrator_now():
                        from app.clients import prefect as prefect_client
                        try:
                            result = prefect_client.trigger_orchestrator_run()
                        except Exception as e:
                            ui.notify(f"Could not trigger orchestrator run: {e}", type="negative")
                            return
                        ui.notify(f"Orchestrator run triggered (flow run {result['prefect_flow_run_id']})", type="positive")

                    ui.button(
                        "Run Orchestrator",
                        icon="bolt",
                        on_click=run_orchestrator_now,
                    ).props(
                        f"unelevated color=primary no-caps {'disabled' if not settings.prefect_api_url else ''}"
                    ).tooltip("Trigger an out-of-cycle blackboard-orchestrator run instead of waiting for the hourly poll.")
                    ui.button("Add AI task to Blackboard", icon="add", on_click=add_task_dialog.open).props("unelevated color=primary no-caps")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("flex-1 border border-gray-200 rounded-lg px-3 py-2 items-center gap-2 bg-white"):
                    ui.icon("search").classes("text-gray-400 shrink-0")
                    ui.input(
                        placeholder="Search by topic, prompt, or finding content...",
                        on_change=lambda e: (current_search.__setitem__(0, e.value), apply_filters()),
                    ).props("borderless dense").classes("flex-1 text-sm")
                ui.select(
                    options=["All States", "waiting", "dispatching_run", "running",
                             "waiting_for_next_periodic_run", "resolved", "failed", "dismissed"],
                    value="All States",
                    on_change=lambda e: (current_state.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")
                ui.select(
                    options=["Newest First", "Topic A–Z"],
                    value="Newest First",
                    on_change=lambda e: (current_sort.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")

            count_lbl()

            with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-x-auto"):
                bb_table()

            # ---- Routing Rules: topic -> follow-up prompt template ----
            # Read by workflow-prefect__ai-blackboard-orchestrator's
            # orchestrator to chain a published finding into a new run. The
            # `blackboard` DB user has no DELETE here, so a rule is retired
            # by toggling `enabled`, never removed.
            try:
                rr_rows = blackboard_client.list_routing_rules()
                rr_load_error = None
            except Exception as e:
                rr_rows = []
                rr_load_error = str(e)

            rr_dialog = ui.dialog()
            rr_error = [None]

            with rr_dialog, ui.card().classes("w-full max-w-2xl p-6 gap-3"):
                rr_dialog_title = ui.label("").classes("text-lg font-bold text-gray-900")
                ui.label(
                    "Maps the topic of a published finding to the prompt the orchestrator "
                    "triggers next. Placeholders: $finding, $prompt, $id, $topic "
                    "($$ for a literal $). No other logic -- the whole finding is substituted as-is."
                ).classes("text-xs text-gray-500")
                rr_topic = ui.input(
                    label="Topic",
                    placeholder="e.g. code-analysis-report",
                ).classes("w-full").props("outlined dense")
                rr_template = ui.textarea(
                    label="Prompt template",
                    placeholder="A report (task_runs.id=$id) found:\n\n$finding\n\nClone the repo, verify each finding, fix real bugs, open a PR.",
                ).classes("w-full").props("outlined dense autogrow")
                rr_enabled = ui.switch("Enabled", value=True)
                rr_error[0] = ui.label("").classes("text-xs text-red-600")

                def submit_rr():
                    tt = (rr_topic.value or "").strip()
                    tmpl = (rr_template.value or "").strip()
                    if not tt or not tmpl:
                        rr_error[0].set_text("Topic and Prompt template are both required.")
                        return
                    try:
                        blackboard_client.upsert_routing_rule(tt, tmpl, bool(rr_enabled.value))
                    except Exception as e:
                        rr_error[0].set_text(f"Could not save: {e}")
                        return
                    ui.notify(f"Saved routing rule '{tt}'", type="positive")
                    rr_dialog.close()
                    ui.navigate.to("/ui/blackboard")

                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("Cancel", on_click=rr_dialog.close).props("flat")
                    ui.button("Save", on_click=submit_rr).props("unelevated color=primary")

            def open_rr_dialog(rule: dict | None):
                rr_error[0].set_text("")
                if rule:
                    rr_dialog_title.set_text(f"Edit routing rule · {rule['topic']}")
                    rr_topic.set_value(rule["topic"])
                    rr_topic.props(add="readonly")
                    rr_template.set_value(rule["prompt_template"])
                    rr_enabled.set_value(bool(rule["enabled"]))
                else:
                    rr_dialog_title.set_text("Add routing rule")
                    rr_topic.set_value("")
                    rr_topic.props(remove="readonly")
                    rr_template.set_value("")
                    rr_enabled.set_value(True)
                rr_dialog.open()

            ui.separator().classes("mt-4")
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Routing Rules").classes("text-2xl font-bold text-gray-900")
                    ui.label(
                        "topic → the follow-up prompt the orchestrator triggers when a "
                        "someone_take_over row with that topic is published."
                    ).classes("text-sm text-gray-500 mt-1")
                ui.button("Add routing rule", icon="add", on_click=lambda: open_rr_dialog(None)).props("unelevated color=primary no-caps")

            if rr_load_error:
                if "routing_rules" in rr_load_error and ("doesn't exist" in rr_load_error or "does not exist" in rr_load_error or "1146" in rr_load_error):
                    _error_label("The routing_rules table doesn't exist yet — create it "
                                 "on the blackboard database as the MariaDB root user "
                                 "(id, topic UNIQUE, prompt_template, enabled, created_at, updated_at).")
                else:
                    _error_label(f"Could not load routing rules: {rr_load_error}")
            elif not rr_rows:
                ui.label("No routing rules yet. Add one to chain a finding into a follow-up run.").classes("text-sm text-gray-400 py-6 text-center w-full")
            else:
                with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-x-auto"):
                    rr_tbl = ui.table(
                        columns=[
                            {"name": "actions",         "label": "",                "field": "id",              "align": "right"},
                            {"name": "topic",       "label": "Topic",           "field": "topic",       "align": "left"},
                            {"name": "enabled",         "label": "Enabled",         "field": "enabled",         "align": "left"},
                            {"name": "prompt_template", "label": "Prompt Template", "field": "prompt_template", "align": "left"},
                            {"name": "updated_at",      "label": "Updated",         "field": "updated_at",      "align": "left"},
                        ],
                        rows=list(rr_rows),
                        row_key="id",
                        pagination={"rowsPerPage": 10},
                    ).classes("w-full")

                    rr_tbl.add_slot("body-cell-enabled", r'''
                        <q-td :props="props">
                            <span class="text-xs px-2 py-0.5 rounded-full font-medium"
                                  :class="props.row.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'">
                                {{ props.row.enabled ? 'enabled' : 'disabled' }}
                            </span>
                        </q-td>''')

                    rr_tbl.add_slot("body-cell-prompt_template", r'''
                        <q-td :props="props">
                            <div class="max-w-xl whitespace-pre-wrap font-mono text-xs text-gray-600"
                                 style="display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;">{{ props.row.prompt_template }}</div>
                        </q-td>''')

                    rr_tbl.add_slot("body-cell-actions", r'''
                        <q-td :props="props">
                            <q-btn flat round dense icon="more_vert" size="sm" class="text-gray-400">
                                <q-menu>
                                    <q-list dense>
                                        <q-item clickable v-close-popup @click="$parent.$emit('rrEdit', props.row)">
                                            <q-item-section>Edit</q-item-section>
                                        </q-item>
                                        <q-item clickable v-close-popup @click="$parent.$emit('rrToggle', props.row)">
                                            <q-item-section>{{ props.row.enabled ? 'Disable' : 'Enable' }}</q-item-section>
                                        </q-item>
                                    </q-list>
                                </q-menu>
                            </q-btn>
                        </q-td>''')

                    def on_rr_edit(e):
                        row = e.args[0] if isinstance(e.args, list) else e.args
                        if row:
                            open_rr_dialog(row)

                    def on_rr_toggle(e):
                        row = e.args[0] if isinstance(e.args, list) else e.args
                        if not row:
                            return
                        try:
                            blackboard_client.set_routing_rule_enabled(row["id"], not row["enabled"])
                        except Exception as ex:
                            ui.notify(f"Could not update rule: {ex}", type="negative")
                            return
                        ui.notify("Routing rule updated", type="positive")
                        ui.navigate.to("/ui/blackboard")

                    rr_tbl.on("rrEdit", on_rr_edit)
                    rr_tbl.on("rrToggle", on_rr_toggle)

    @_napp.get("/ui/blackboard/trace/{row_id}")
    def blackboard_trace(row_id: int):
        # Plain HTTP GET, not a @ui.page -- this is what the trace iframe's
        # `src` points at (see open_detail above). Session-cookie auth still
        # applies: NiceGUI's storage middleware is installed on this same
        # app regardless of route type, so app.storage.user is available
        # here exactly as it is inside a @ui.page.
        if not session.can("blackboard"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        trace_html = blackboard_client.get_trace_html(row_id)
        if trace_html is None:
            raise HTTPException(status_code=404, detail="No trace for this row")
        return HTMLResponse(content=trace_html)

    @ui.page("/ui/trigger")
    def trigger():
        if not _require_login("runs"):
            return
        _header("/ui/trigger", api="model-runs", method="POST")

        class _StepCtx:
            def __init__(self, row, panel, status_lbl):
                self._row        = row
                self._panel      = panel
                self.status_lbl  = status_lbl
            def __exit__(self, *a):
                self._panel.__exit__(*a)
                self._row.__exit__(*a)

        def _step_row(n: int, title: str, description: str, is_last: bool = False):
            row = ui.row().classes("w-full gap-6 items-stretch")
            row.__enter__()
            with ui.column().classes("w-48 shrink-0 gap-0"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(str(n)).classes(
                        "bg-blue-700 text-white rounded-full w-7 h-7 flex items-center "
                        "justify-center text-sm font-bold shrink-0"
                    )
                    ui.label(title).classes("text-sm font-semibold text-gray-800")
                ui.label(description).classes("text-xs text-gray-500 ml-10 mt-1")
                status_lbl = ui.label("").classes("ml-10 mt-2 text-xs text-gray-400")
                if not is_last:
                    ui.element("div").classes("ml-3 border-l-2 border-gray-200 flex-1 mt-2")
            panel = ui.column().classes("flex-1 border border-gray-200 rounded-lg p-4 min-w-0 gap-3")
            panel.__enter__()
            return _StepCtx(row, panel, status_lbl), status_lbl

        with ui.column().classes("p-8 w-full gap-6"):
            with ui.column().classes("gap-1 mb-2"):
                ui.label("Run a forecast model").classes("text-2xl font-bold text-gray-900")
                ui.label(
                    "Complete each step below, then click Pre-flight Check to review and submit."
                ).classes("text-sm text-gray-500")

            ctx1, dataset_status = _step_row(1, "Input Datasets", "Select the dataset(s)\nto use as input.")
            try:
                dataset_rows = ckan_client.list_processed_datasets()
                with ui.row().classes("w-full items-center gap-2 pb-2 border-b border-gray-100"):
                    ui.icon("search").classes("text-gray-400 text-lg")
                    dataset_filter = ui.input(placeholder="Filter datasets...").classes("flex-1 text-sm").props("borderless dense")
                dataset_table = ui.table(
                    columns=[
                        {"name": "name",        "label": "Name",        "field": "name",        "align": "left", "sortable": True},
                        {"name": "qid",         "label": "QID",         "field": "qid",         "align": "left", "sortable": True},
                        {"name": "description", "label": "Description", "field": "description", "align": "left"},
                    ],
                    rows=dataset_rows,
                    row_key="qid",
                    selection="multiple",
                    pagination={"rowsPerPage": 5},
                ).classes("w-full")
                dataset_filter.bind_value(dataset_table, "filter")
            except Exception as e:
                _error_label(f"Could not load datasets: {e}")
                dataset_table = None
            ctx1.__exit__(None, None, None)

            ctx2, model_status = _step_row(2, "Model", "Select the model\nto run.")
            try:
                model_rows = ckan_client.list_models()
                with ui.row().classes("w-full items-center gap-2 pb-2 border-b border-gray-100"):
                    ui.icon("search").classes("text-gray-400 text-lg")
                    model_filter = ui.input(placeholder="Filter by name...").classes("flex-1 text-sm").props("borderless dense")
                model_table = ui.table(
                    columns=[
                        {"name": "name",        "label": "Name",        "field": "name",        "align": "left", "sortable": True},
                        {"name": "docker_tag",  "label": "Tag",         "field": "docker_tag",  "align": "left", "sortable": True},
                        {"name": "description", "label": "Description", "field": "description", "align": "left"},
                    ],
                    rows=model_rows,
                    row_key="name",
                    selection="single",
                    pagination={"rowsPerPage": 5},
                ).classes("w-full")
                model_table.add_slot('body-cell-docker_tag', r'''
                    <q-td :props="props">
                        <span class="bg-blue-100 text-blue-700 px-2 py-0.5 rounded text-xs font-mono">{{ props.row.docker_tag }}</span>
                    </q-td>
                ''')
                model_filter.bind_value(model_table, "filter")

                def on_model_selection(e):
                    if model_table.selected:
                        m = model_table.selected[0]
                        model_status.set_text(f"✓ {m['name']} ({m['docker_tag']})")
                        model_status.classes(remove="text-gray-400", add="text-green-600")
                    else:
                        model_status.set_text("")
                        model_status.classes(remove="text-green-600", add="text-gray-400")

                model_table.on("selection", on_model_selection)
            except Exception as e:
                _error_label(f"Could not load models: {e}")
                model_table = None
            ctx2.__exit__(None, None, None)

            def _has_sql(val: str) -> bool:
                return any(
                    line.strip() and not line.strip().startswith("--")
                    for line in val.splitlines()
                )

            ctx3, sql_status = _step_row(3, "Dataset Transformation", "SQL applied to each\nselected dataset before\nit is passed to the\nmodel-runner.")
            sql_status.set_text("Optional")
            dataset_sql_inputs = {}
            sql_container = ui.column().classes("w-full gap-4")
            sql_hint = ui.label("Select a dataset in step 1 to define a SQL transformation.").classes("text-sm text-gray-400 italic")

            def _make_verify(inp):
                def verify():
                    sql = inp.value.strip()
                    if not sql:
                        ui.notify("Enter a SQL query to verify", type="warning", position="top")
                        return
                    try:
                        validate_transformation_sql(sql)
                        ui.notify("SQL syntax is valid", type="positive", position="top")
                    except duckdb.ParserException as ex:
                        ui.notify(f"Syntax error: {ex}", type="negative", position="top")
                    except ValueError as ex:
                        ui.notify(str(ex), type="negative", position="top")
                    except duckdb.Error as ex:
                        ui.notify(f"Could not parse SQL: {ex}", type="negative", position="top")
                return verify

            def _make_format_sql(inp):
                def fmt():
                    try:
                        import sqlparse
                        inp.value = sqlparse.format(inp.value, reindent=True, keyword_case='upper')
                    except ImportError:
                        ui.notify("sqlparse not available", type="warning", position="top")
                    except Exception as ex:
                        ui.notify(f"Format error: {ex}", type="negative", position="top")
                return fmt

            def rebuild_sql_inputs(e):
                selected = dataset_table.selected if dataset_table else []
                current_qids = {row["qid"] for row in selected}
                for qid in list(dataset_sql_inputs):
                    if qid not in current_qids:
                        del dataset_sql_inputs[qid]
                sql_container.clear()
                with sql_container:
                    for row in selected:
                        qid = row["qid"]
                        existing = dataset_sql_inputs.get(qid)
                        prev = existing.value if existing else ""
                        with ui.column().classes("w-full gap-2"):
                            ui.label(row["name"]).classes("text-sm font-medium text-gray-700")
                            inp = ui.codemirror(
                                value=prev if prev else "-- SELECT * FROM df WHERE column = 'value'",
                                language="sql",
                            ).classes("w-full text-sm rounded border border-gray-200").style("height: 120px")
                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("Format SQL", icon="auto_fix_high", on_click=_make_format_sql(inp)).props("flat dense").classes("text-xs text-gray-500")
                                ui.button("Verify SQL", icon="check", on_click=_make_verify(inp)).classes("bg-blue-700 text-white text-xs")
                        dataset_sql_inputs[qid] = inp
                n = len(selected)
                sql_hint.set_visibility(n == 0)
                if n:
                    dataset_status.set_text(f"{n} selected")
                    dataset_status.classes(remove="text-gray-400", add="bg-green-100 text-green-700 px-2 py-0.5 rounded-full font-medium")
                else:
                    dataset_status.set_text("")
                    dataset_status.classes(remove="bg-green-100 text-green-700 px-2 py-0.5 rounded-full font-medium", add="text-gray-400")
                filled = sum(1 for inp in dataset_sql_inputs.values() if _has_sql(inp.value))
                if filled:
                    sql_status.set_text(f"✓ {filled} transformation active")
                    sql_status.classes(remove="text-gray-400", add="text-green-600")
                else:
                    sql_status.set_text("Optional")
                    sql_status.classes(remove="text-green-600", add="text-gray-400")

            if dataset_table:
                dataset_table.on("selection", rebuild_sql_inputs)
            ctx3.__exit__(None, None, None)

            ctx4, _ = _step_row(4, "Config (JSON)", "Provide configuration\nin JSON format.", is_last=True)

            def format_json():
                try:
                    val = json.loads(config_input.value)
                    config_input.value = json.dumps(val, indent=2)
                except json.JSONDecodeError as e:
                    ui.notify(f"Invalid JSON: {e}", type="negative", position="top")

            with ui.row().classes("w-full justify-end items-center gap-1"):
                ui.button(icon="close", on_click=lambda: config_input.set_value("")).props("flat round dense").classes("text-gray-400 text-xs")
                ui.button("Format JSON", icon="auto_fix_high", on_click=format_json).props("flat dense").classes("text-xs text-gray-500")
            config_input = ui.codemirror(
                value='{\n  "horizon_weeks": 4,\n  "n_reference_weeks": 4\n}',
                language="json",
            ).classes("w-full font-mono")
            ctx4.__exit__(None, None, None)

            result_label = ui.label("").classes("text-sm text-gray-500")

            filename_inputs = []

            with ui.dialog() as preflight_dialog:
                with ui.card().classes("w-full min-w-[32rem] p-6"):
                    ui.label("Pre-flight Check").classes("text-lg font-semibold mb-4")
                    with ui.column().classes("gap-3 w-full"):
                        with ui.column().classes("gap-1 border border-gray-200 rounded p-3 w-full"):
                            ui.label("Input Datasets").classes("text-xs text-gray-500 uppercase tracking-wide mb-1")
                            inputs_container = ui.column().classes("w-full gap-2")
                        with ui.column().classes("gap-1 border border-gray-200 rounded p-3 w-full"):
                            ui.label("Model").classes("text-xs text-gray-500 uppercase tracking-wide")
                            model_summary = ui.label("").classes("text-sm text-gray-800")
                        with ui.column().classes("gap-1 border border-gray-200 rounded p-3 w-full"):
                            ui.label("Config").classes("text-xs text-gray-500 uppercase tracking-wide")
                            config_summary = ui.label("").classes("text-sm text-gray-800 font-mono whitespace-pre-wrap")
                    with ui.expansion("Prefect Payload").classes("w-full mt-4 text-xs text-gray-500"):
                        payload_label = ui.label("").classes("font-mono text-xs whitespace-pre-wrap text-gray-700")
                    with ui.row().classes("mt-4 gap-2 justify-end w-full"):
                        ui.button("Cancel", on_click=preflight_dialog.close).classes("text-gray-600")
                        confirm_btn = ui.button("Trigger Run", icon="play_arrow").classes("bg-blue-700 text-white").props(f"{'disabled' if not settings.prefect_api_url else ''}")

            def do_submit():
                m = model_table.selected[0]
                try:
                    config = json.loads(config_input.value)
                except json.JSONDecodeError as e:
                    ui.notify(f"Invalid JSON: {e}", type="negative", position="top")
                    return
                filenames = [inp.value.strip() for _, inp, _sql in filename_inputs]
                if len(filenames) != len(set(filenames)):
                    ui.notify("All target filenames must be unique", type="negative", position="top")
                    return
                preflight_dialog.close()
                input_data_files        = [[dp, inp.value.strip()] for dp, inp, _ in filename_inputs]
                data_transformation_sql = [sql if _has_sql(sql) else "" for _, _, sql in filename_inputs]
                from app.clients import prefect as prefect_client
                try:
                    result = prefect_client.trigger_model_run(
                        input_data_files=input_data_files,
                        model_image=m["docker_image"],
                        model_tag=m["docker_tag"],
                        config_json=json.dumps(config),
                        data_transformation_sql=data_transformation_sql,
                        executed_by=_executor(),
                    )
                    result_label.set_text(f"Triggered: {result['prefect_flow_run_id']} ({result['status']})")
                    ui.notify("Model run triggered", type="positive", position="top")
                except Exception as e:
                    ui.notify(f"Prefect error: {e}", type="negative", position="top")

            confirm_btn.on("click", do_submit)

            def open_preflight():
                if not dataset_table or not dataset_table.selected:
                    ui.notify("Select at least one input dataset", type="warning", position="top")
                    return
                if not model_table or not model_table.selected:
                    ui.notify("Select a model", type="warning", position="top")
                    return
                try:
                    json.loads(config_input.value)
                except json.JSONDecodeError as e:
                    ui.notify(f"Invalid JSON: {e}", type="negative", position="top")
                    return
                m = model_table.selected[0]

                def update_payload():
                    payload = {
                        "parameters": {
                            "input_data_files":        [[dp, inp.value.strip()] for dp, inp, _ in filename_inputs],
                            "model_image":             m["docker_image"],
                            "model_tag":               m["docker_tag"],
                            "config_json":             config_input.value.strip(),
                            "data_transformation_sql": [sql if _has_sql(sql) else "" for _, _, sql in filename_inputs],
                        }
                    }
                    payload_label.set_text(json.dumps(payload, indent=2))

                filename_inputs.clear()
                inputs_container.clear()
                with inputs_container:
                    for idx, row in enumerate(dataset_table.selected):
                        qid = row["qid"]
                        original_name = row["data_path"].split("/")[-1] if row.get("data_path") else ""
                        suffix = "." + original_name.rsplit(".", 1)[-1] if "." in original_name else ""
                        default_name = f"input{'' if idx == 0 else idx + 1}{suffix}"
                        sql_val = dataset_sql_inputs[qid].value.strip() if qid in dataset_sql_inputs else ""
                        with ui.column().classes("w-full gap-3 border border-gray-200 rounded p-3"):
                            ui.label(row["name"]).classes("text-xs text-gray-500 uppercase tracking-wide font-semibold")
                            with ui.column().classes("gap-0"):
                                ui.label("Original filename").classes("text-xs text-gray-400")
                                ui.label(original_name).classes("text-sm font-mono text-gray-700")
                            with ui.column().classes("gap-0 w-full"):
                                ui.label("New filename").classes("text-xs text-gray-400")
                                ui.label("(filename the model-runner will see)").style("font-size: 0.65rem").classes("text-red-400 -mt-1")
                                with ui.element("div").classes("w-full"):
                                    inp = ui.input(value=default_name, on_change=update_payload).classes("w-full font-mono text-sm").style("background-color: #f0fdf4")
                            with ui.column().classes("gap-0"):
                                ui.label("Transform (SQL)").classes("text-xs text-gray-400")
                                ui.label(sql_val if _has_sql(sql_val) else "— none —").classes("text-sm font-mono text-gray-600")
                        filename_inputs.append((row["data_path"], inp, sql_val))

                model_summary.set_text(f"{m['name']} ({m['docker_tag']})")
                config_summary.set_text(config_input.value.strip())
                update_payload()
                preflight_dialog.open()

            if not settings.prefect_api_url:
                ui.label(
                    "⚠ PREFECT_API_URL is not configured. Set it in .env to enable triggering runs. "
                    "You can do the pre-flight check but will not be able to submit to Prefect."
                ).classes("text-orange-600 text-sm")
            with ui.row().classes("w-full items-center gap-4 mt-2"):
                ui.button("Pre-flight Check", icon="checklist", on_click=open_preflight).classes("bg-blue-700 text-white")
                ui.label("Validate all steps before running the model.").classes("text-sm text-gray-500")
            result_label

    # ─── Trigger v2 wizard (5 steps) ─────────────────────────────────────────

    @ui.page("/ui/run_workflow")
    def _tv2_redirect():
        if not _require_login("runs"):
            return
        ui.navigate.to("/ui/run_workflow/1")

    # Step 1 — Datasets ───────────────────────────────────────────────────────

    @ui.page("/ui/run_workflow/1")
    def trigger_v2_step1(benchmark: int | None = None):
        if not _require_login("runs"):
            return
        state = _tv2_state()
        try:
            all_ds = ckan_client.list_processed_datasets()
        except Exception:
            all_ds = []

        selected_qids: set = set(d["qid"] for d in state.get("datasets", []))
        visible: list = list(all_ds)

        def toggle(qid: str):
            if qid in selected_qids:
                selected_qids.discard(qid)
            else:
                selected_qids.add(qid)
            state["datasets"] = [r for r in all_ds if r["qid"] in selected_qids]
            clear_benchmark(state)
            _napp.storage.user["tv2"] = state
            dataset_cards.refresh()
            bottom_bar.refresh()
            run_summary.refresh()

        def clear_all():
            selected_qids.clear()
            state["datasets"] = []
            clear_benchmark(state)
            _napp.storage.user["tv2"] = state
            dataset_cards.refresh()
            bottom_bar.refresh()
            run_summary.refresh()

        try:
            finals = bactions.list_final()
        except Exception:
            logger.exception("could not list the final benchmarks")
            finals = []
        active = (state.get("benchmark") or {}).get("id") or benchmark
        chosen_benchmark: dict = {"id": active if any(b["id"] == active for b in finals) else None}

        def use_benchmark():
            benchmark_id = chosen_benchmark["id"]
            if benchmark_id is None:
                ui.notify("Choose a benchmark first", type="warning", position="top")
                return
            try:
                spec = bactions.run_spec(benchmark_id)
                row = bactions.get(benchmark_id)
            except BenchmarkNotFound as exc:
                ui.notify(str(exc), type="negative", position="top")
                return
            except Exception:
                logger.exception("could not apply benchmark %s", benchmark_id)
                ui.notify("Could not apply the benchmark", type="negative", position="top")
                return
            ds = next((r for r in all_ds if r["qid"] == spec["dataset_qid"]), None)
            if ds is None:
                ui.notify("The benchmark's dataset is not available", type="negative", position="top")
                return
            try:
                apply_benchmark(state, spec, ds, row)
            except Exception:
                logger.exception("could not apply benchmark %s", benchmark_id)
                ui.notify("Could not apply the benchmark", type="negative", position="top")
                return
            selected_qids.clear()
            selected_qids.add(ds["qid"])
            _napp.storage.user["tv2"] = state
            dataset_cards.refresh()
            bottom_bar.refresh()
            run_summary.refresh()
            ui.notify(f"Benchmark {row['name']} applied", type="positive", position="top")

        def do_filter(v: str):
            vl = v.lower()
            visible.clear()
            visible.extend(
                r for r in all_ds
                if not vl or vl in r["name"].lower() or vl in r.get("description", "").lower()
            )
            dataset_cards.refresh()

        def on_continue():
            if not selected_qids:
                ui.notify("Select at least one dataset", type="warning", position="top")
                return
            ui.navigate.to("/ui/run_workflow/2")

        @ui.refreshable
        def dataset_cards():
            if not visible:
                ui.label(
                    "No datasets match your filter." if all_ds else "No datasets available."
                ).classes("text-sm text-gray-400 py-8 text-center w-full")
                return
            for row in visible:
                qid = row["qid"]
                sel = qid in selected_qids
                card_cls = (
                    "w-full border rounded-xl p-4 cursor-pointer flex items-center gap-4 mb-2 "
                    + ("border-blue-500 bg-blue-50" if sel else "border-gray-200 bg-white hover:border-gray-300")
                )
                with ui.element("div").classes(card_cls).on("click", lambda _, q=qid: toggle(q)):
                    if sel:
                        with ui.element("div").classes("w-5 h-5 bg-blue-600 rounded flex items-center justify-center shrink-0"):
                            ui.icon("check").classes("text-white").style("font-size: 14px")
                    else:
                        ui.element("div").classes("w-5 h-5 border-2 border-gray-300 rounded shrink-0")
                    with ui.column().classes("flex-1 gap-0 min-w-0"):
                        ui.label(row["name"]).classes("text-sm font-semibold text-gray-800")
                        desc = (row.get("description") or "")[:100]
                        if desc:
                            ui.label(desc).classes("text-xs text-gray-500 mt-0.5")
                    with ui.element("div").classes("bg-gray-100 text-gray-500 text-xs px-2 py-0.5 rounded font-mono shrink-0"):
                        ui.label(row.get("qid", ""))

        @ui.refreshable
        def bottom_bar():
            n = len(selected_qids)
            with ui.row().classes("w-full items-center justify-between pt-3 border-t border-gray-100 mt-2"):
                if n:
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("check_circle").classes("text-green-500")
                        ui.label(f"{n} dataset{'s' if n != 1 else ''} selected").classes("text-sm font-medium text-gray-700")
                else:
                    ui.label("No datasets selected").classes("text-sm text-gray-400")
                if n:
                    ui.button("Clear selection", on_click=clear_all).props("flat dense no-caps").classes("text-sm text-blue-600")

        @ui.refreshable
        def run_summary():
            _tv2_summary(state, hint="" if selected_qids else "Select datasets to continue.")

        _header("/ui/run_workflow/1", api="model-runs", method="POST")
        with ui.column().classes("px-8 py-6 w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("text-3xl font-bold text-gray-900")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            _tv2_stepper(1)
            if finals:
                with ui.element("div").classes("w-full border border-gray-200 rounded-xl p-5 bg-white"):
                    ui.label("Start from a benchmark").classes("text-lg font-bold text-gray-900")
                    ui.label(
                        "Prefills the dataset at the benchmark's data commit, the transformation and the horizon."
                    ).classes("text-sm text-gray-500 mt-1")
                    with ui.row().classes("w-full items-center gap-3 mt-2"):
                        ui.select(
                            {b["id"]: b["name"] for b in finals}, label="Start from a benchmark",
                            value=chosen_benchmark["id"],
                            on_change=lambda e: chosen_benchmark.update(id=e.value),
                        ).classes("w-80").props("dense outlined")
                        ui.button("Use benchmark", on_click=use_benchmark).props("no-caps").classes(
                            "bg-blue-700 text-white rounded-lg")
            with ui.row().classes("w-full gap-6 items-start"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.row().classes("w-full items-start justify-between mb-4"):
                        with ui.column().classes("gap-0"):
                            ui.label("1. Select Input Datasets").classes("text-lg font-bold text-gray-900")
                            ui.label("Choose one or more datasets to use as input.").classes("text-sm text-gray-500 mt-1")
                        with ui.row().classes("border border-gray-200 rounded-lg px-3 py-1.5 items-center gap-2 shrink-0"):
                            ui.icon("search").classes("text-gray-400")
                            ui.input(
                                placeholder="Search datasets...",
                                on_change=lambda e: do_filter(e.value),
                            ).props("borderless dense").classes("w-44 text-sm")
                    dataset_cards()
                    bottom_bar()
                with ui.column().classes("shrink-0 gap-3"):
                    run_summary()
                    with ui.row().classes("w-full justify-end"):
                        ui.button(
                            "Continue to Transformations", icon="arrow_forward", on_click=on_continue,
                        ).classes("bg-blue-700 text-white px-8 rounded-lg").props("no-caps")
        _tv2_footer()

    # Step 2 — Dataset Transformations ────────────────────────────────────────

    @ui.page("/ui/run_workflow/2")
    def trigger_v2_step2():
        if not _require_login("runs"):
            return
        state = _tv2_state()
        if not state.get("datasets"):
            ui.navigate.to("/ui/run_workflow/1")
            return

        datasets = state.get("datasets", [])
        sql_refs: dict = {}

        def make_verify(inp):
            def verify():
                sql = inp.value.strip()
                if not sql:
                    ui.notify("Enter a SQL query to verify", type="warning", position="top")
                    return
                try:
                    validate_transformation_sql(sql)
                    ui.notify("SQL syntax is valid", type="positive", position="top")
                except duckdb.ParserException as ex:
                    ui.notify(f"Syntax error: {ex}", type="negative", position="top")
                except ValueError as ex:
                    ui.notify(str(ex), type="negative", position="top")
                except duckdb.Error as ex:
                    ui.notify(f"Could not parse SQL: {ex}", type="negative", position="top")
            return verify

        def make_format_sql(inp):
            def fmt():
                try:
                    import sqlparse
                    inp.value = sqlparse.format(inp.value, reindent=True, keyword_case="upper")
                except ImportError:
                    ui.notify("sqlparse not available", type="warning", position="top")
                except Exception as ex:
                    ui.notify(f"Format error: {ex}", type="negative", position="top")
            return fmt

        def on_continue():
            state["sql"] = {qid: inp.value for qid, inp in sql_refs.items()}
            _napp.storage.user["tv2"] = state
            ui.navigate.to("/ui/run_workflow/3")

        _header("/ui/run_workflow/2", api="model-runs", method="POST")
        with ui.column().classes("px-8 py-6 w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("text-3xl font-bold text-gray-900")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            _tv2_stepper(2)
            with ui.row().classes("w-full gap-6 items-start"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.column().classes("gap-0 mb-4"):
                        ui.label("2. Dataset Transformations").classes("text-lg font-bold text-gray-900")
                        ui.label(
                            "Optionally define a SQL transformation for each selected dataset. "
                            "Leave the default comment to skip."
                        ).classes("text-sm text-gray-500 mt-1")
                    for row in datasets:
                        qid = row["qid"]
                        prev = state.get("sql", {}).get(qid, "")
                        with ui.column().classes("w-full gap-2 mb-5"):
                            ui.label(row["name"]).classes("text-sm font-semibold text-gray-700")
                            inp = ui.codemirror(
                                value=prev if prev else "-- SELECT * FROM df WHERE column = 'value'",
                                language="sql",
                            ).classes("w-full text-sm rounded border border-gray-200").style("height: 120px")
                            sql_refs[qid] = inp
                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button(
                                    "Format SQL", icon="auto_fix_high", on_click=make_format_sql(inp),
                                ).props("flat dense no-caps").classes("text-xs text-gray-500")
                                ui.button(
                                    "Verify SQL", icon="check", on_click=make_verify(inp),
                                ).classes("bg-blue-700 text-white text-xs").props("no-caps dense")
                with ui.column().classes("shrink-0 gap-3"):
                    _tv2_summary(state)
                    with ui.row().classes("w-full justify-between items-center"):
                        ui.button("Back to Datasets", icon="arrow_back", on_click=lambda: ui.navigate.to("/ui/run_workflow/1")).classes("!bg-green-600 text-white px-8 rounded-lg").props("no-caps")
                        ui.button(
                            "Continue to Model", icon="arrow_forward", on_click=on_continue,
                        ).classes("bg-blue-700 text-white px-8 rounded-lg").props("no-caps")
        _tv2_footer()

    # Step 3 — Model ──────────────────────────────────────────────────────────

    @ui.page("/ui/run_workflow/3")
    def trigger_v2_step3():
        if not _require_login("runs"):
            return
        state = _tv2_state()
        if not state.get("datasets"):
            ui.navigate.to("/ui/run_workflow/1")
            return

        try:
            all_models = ckan_client.list_models()
        except Exception:
            all_models = []

        sel_name: list = [state["model"]["name"] if state.get("model") else ""]
        visible_models: list = list(all_models)

        def select_model(name: str):
            if sel_name[0] == name:
                sel_name[0] = ""
                state["model"] = None
            else:
                sel_name[0] = name
                state["model"] = next((m for m in all_models if m["name"] == name), None)
            state["config"] = None
            _napp.storage.user["tv2"] = state
            model_cards.refresh()
            run_summary.refresh()

        def do_filter(v: str):
            vl = v.lower()
            visible_models.clear()
            visible_models.extend(
                m for m in all_models
                if not vl or vl in m["name"].lower() or vl in m.get("description", "").lower()
            )
            model_cards.refresh()

        def on_continue():
            if not sel_name[0]:
                ui.notify("Select a model", type="warning", position="top")
                return
            ui.navigate.to("/ui/run_workflow/4")

        @ui.refreshable
        def model_cards():
            if not visible_models:
                ui.label("No models available.").classes("text-sm text-gray-400 py-8 text-center w-full")
                return
            for m in visible_models:
                sel = sel_name[0] == m["name"]
                card_cls = (
                    "w-full border rounded-xl p-4 cursor-pointer flex items-center gap-4 mb-2 "
                    + ("border-blue-500 bg-blue-50" if sel else "border-gray-200 bg-white hover:border-gray-300")
                )
                with ui.element("div").classes(card_cls).on("click", lambda _, n=m["name"]: select_model(n)):
                    with ui.column().classes("flex-1 gap-0 min-w-0"):
                        ui.label(m["name"]).classes("text-sm font-semibold text-gray-800")
                        desc = (m.get("description") or "")[:100]
                        if desc:
                            ui.label(desc).classes("text-xs text-gray-500 mt-0.5")
                    with ui.element("div").classes("bg-blue-100 text-blue-700 text-xs px-2 py-0.5 rounded font-mono shrink-0"):
                        ui.label(m.get("docker_tag", ""))
                    if sel:
                        with ui.element("div").classes("w-5 h-5 bg-blue-600 rounded-full flex items-center justify-center shrink-0"):
                            ui.icon("check").classes("text-white").style("font-size: 14px")
                    else:
                        ui.element("div").classes("w-5 h-5 border-2 border-gray-300 rounded-full shrink-0")

        @ui.refreshable
        def run_summary():
            _tv2_summary(state, hint="" if sel_name[0] else "Select a model to continue.")

        _header("/ui/run_workflow/3", api="model-runs", method="POST")
        with ui.column().classes("px-8 py-6 w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("text-3xl font-bold text-gray-900")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            _tv2_stepper(3)
            with ui.row().classes("w-full gap-6 items-start"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.row().classes("w-full items-start justify-between mb-4"):
                        with ui.column().classes("gap-0"):
                            ui.label("3. Select Model").classes("text-lg font-bold text-gray-900")
                            ui.label("Choose the forecasting model to run.").classes("text-sm text-gray-500 mt-1")
                        with ui.row().classes("border border-gray-200 rounded-lg px-3 py-1.5 items-center gap-2 shrink-0"):
                            ui.icon("search").classes("text-gray-400")
                            ui.input(
                                placeholder="Filter models...",
                                on_change=lambda e: do_filter(e.value),
                            ).props("borderless dense").classes("w-44 text-sm")
                    model_cards()
                with ui.column().classes("shrink-0 gap-3"):
                    run_summary()
                    with ui.row().classes("w-full justify-between items-center"):
                        ui.button("Back to Transformations", icon="arrow_back", on_click=lambda: ui.navigate.to("/ui/run_workflow/2")).classes("!bg-green-600 text-white px-8 rounded-lg").props("no-caps")
                        ui.button(
                            "Continue to Configuration", icon="arrow_forward", on_click=on_continue,
                        ).classes("bg-blue-700 text-white px-8 rounded-lg").props("no-caps")
        _tv2_footer()

    # Step 4 — Configuration ──────────────────────────────────────────────────

    @ui.page("/ui/run_workflow/4")
    def trigger_v2_step4():
        if not _require_login("runs"):
            return
        state = _tv2_state()
        if not state.get("datasets"):
            ui.navigate.to("/ui/run_workflow/1")
            return
        if not state.get("model"):
            ui.navigate.to("/ui/run_workflow/3")
            return

        def format_json():
            try:
                config_inp.value = json.dumps(json.loads(config_inp.value), indent=2)
            except json.JSONDecodeError as e:
                ui.notify(f"Invalid JSON: {e}", type="negative", position="top")

        def on_continue():
            state["config"] = config_inp.value
            _napp.storage.user["tv2"] = state
            ui.navigate.to("/ui/run_workflow/5")

        _header("/ui/run_workflow/4", api="model-runs", method="POST")
        with ui.column().classes("px-8 py-6 w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("text-3xl font-bold text-gray-900")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            _tv2_stepper(4)
            with ui.row().classes("w-full gap-6 items-start"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.row().classes("w-full items-start justify-between mb-3"):
                        with ui.column().classes("gap-0"):
                            ui.label("4. Model Configuration (JSON)").classes("text-lg font-bold text-gray-900")
                            ui.label("Provide configuration parameters for the model.").classes("text-sm text-gray-500 mt-1")
                        with ui.row().classes("gap-1 shrink-0"):
                            ui.button(icon="close", on_click=lambda: config_inp.set_value("")).props("flat round dense").classes("text-gray-400")
                            ui.button("Format", icon="auto_fix_high", on_click=format_json).props("flat dense no-caps").classes("text-xs text-gray-500")
                    if state.get("config") is None:
                        params = (state["model"] or {}).get("additional_properties", [])
                        state["config"] = json.dumps(
                            overlay_config(
                                {p["name"]: p.get("value") for p in params},
                                state.get("benchmark"), {p["name"] for p in params},
                            ), indent=2
                        ) if params else "{}"
                    config_inp = ui.codemirror(
                        value=state["config"],
                        language="json",
                    ).classes("w-full font-mono")
                with ui.column().classes("shrink-0 gap-3"):
                    _tv2_summary(state)
                    with ui.row().classes("w-full justify-between items-center"):
                        ui.button("Back to Model", icon="arrow_back", on_click=lambda: ui.navigate.to("/ui/run_workflow/3")).classes("!bg-green-600 text-white px-8 rounded-lg").props("no-caps")
                        ui.button(
                            "Continue to Review", icon="arrow_forward", on_click=on_continue,
                        ).classes("bg-blue-700 text-white px-8 rounded-lg").props("no-caps")
        _tv2_footer()

    # Step 5 — Review & Run ───────────────────────────────────────────────────

    @ui.page("/ui/run_workflow/5")
    def trigger_v2_step5():
        if not _require_login("runs"):
            return
        state = _tv2_state()
        if not state.get("datasets"):
            ui.navigate.to("/ui/run_workflow/1")
            return
        if not state.get("model"):
            ui.navigate.to("/ui/run_workflow/3")
            return

        datasets = state.get("datasets", [])
        model    = state["model"]
        filename_refs: list = []  # (data_path, inp_widget, sql_val)

        def _has_sql(val: str) -> bool:
            return any(line.strip() and not line.strip().startswith("--") for line in val.splitlines())

        def build_payload() -> dict:
            try:
                config = json.loads(state.get("config", "{}"))
            except Exception:
                config = {}
            return {
                "parameters": {
                    "input_data_files":        [[dp, inp.value.strip()] for dp, inp, _ in filename_refs],
                    "model_image":             model["docker_image"],
                    "model_tag":               model["docker_tag"],
                    "config_json":             json.dumps(config),
                    "data_transformation_sql": [sql if _has_sql(sql) else "" for _, _, sql in filename_refs],
                }
            }

        def update_payload():
            payload_lbl.set_text(json.dumps(build_payload(), indent=2))

        def do_submit():
            filenames = [inp.value.strip() for _, inp, _ in filename_refs]
            if len(filenames) != len(set(filenames)):
                ui.notify("All target filenames must be unique", type="negative", position="top")
                return
            try:
                config = json.loads(state.get("config", "{}"))
            except json.JSONDecodeError as e:
                ui.notify(f"Invalid config JSON: {e}", type="negative", position="top")
                return
            from app.model_config import config_problems
            problems = config_problems(config, model.get("additional_properties") or [])
            if problems:
                ui.notify("Invalid model config: " + "; ".join(problems), type="negative", position="top")
                return
            from app.clients import prefect as prefect_client
            try:
                p = build_payload()["parameters"]
                result = prefect_client.trigger_model_run(
                    input_data_files=p["input_data_files"],
                    model_image=p["model_image"],
                    model_tag=p["model_tag"],
                    config_json=p["config_json"],
                    data_transformation_sql=p["data_transformation_sql"],
                    executed_by=_executor(),
                )
                result_lbl.set_text(f"Triggered: {result['prefect_flow_run_id']} ({result['status']})")
                result_lbl.classes(remove="text-gray-500", add="text-green-600 font-medium")
                if state.get("benchmark"):
                    with result_box:
                        ui.link("Open the leaderboard", f"/ui/benchmarks/{state['benchmark']['id']}")
                        ui.label("Add the run there once it has finished.").classes("text-sm text-gray-500")
                ui.notify("Model run triggered", type="positive", position="top")
            except Exception as exc:
                ui.notify(f"Prefect error: {exc}", type="negative", position="top")

        _header("/ui/run_workflow/5", api="model-runs", method="POST")
        with ui.column().classes("px-8 py-6 w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("text-3xl font-bold text-gray-900")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            _tv2_stepper(5)
            with ui.row().classes("w-full justify-between items-center"):
                ui.button("Back to Configuration", icon="arrow_back", on_click=lambda: ui.navigate.to("/ui/run_workflow/4")).classes("!bg-green-600 text-white px-8 rounded-lg").props("no-caps")
                ui.button(
                    "Trigger Run", icon="play_arrow", on_click=do_submit,
                ).classes("bg-blue-700 text-white px-8 rounded-lg").props(
                    f"{'disabled' if not settings.prefect_api_url else ''} no-caps"
                )
            bench = state.get("benchmark")
            if bench:
                with ui.element("div").classes("w-full border border-blue-200 rounded-xl p-4 bg-blue-50 gap-1"):
                    ui.label(f"Benchmark: {bench['name']} (data commit {bench['data_commit'][:12]})").classes(
                        "text-sm font-semibold text-blue-800")
                    bench_row = next((d for d in datasets if d["qid"] == bench["dataset_qid"]), None)
                    problems = workflow_problems(
                        bench, (bench_row or {}).get("data_path", ""),
                        state.get("sql", {}).get(bench["dataset_qid"], ""), bench["date_column"],
                        bench.get("filters", []),
                    ) if bench_row else ["the benchmark's dataset is no longer selected"]
                    if problems:
                        ui.label("This run would not qualify for the leaderboard:").classes(
                            "text-sm text-orange-700 font-medium")
                        for problem in problems:
                            ui.label(f"- {problem}").classes("text-sm text-orange-700")
            with ui.column().classes("w-full gap-4"):

                    # Input datasets + filename editing
                    with ui.element("div").classes("w-full border border-gray-200 rounded-xl p-5 bg-white"):
                        ui.label("Input Datasets").classes("text-base font-bold text-gray-900 mb-3")
                        for idx, row in enumerate(datasets):
                            qid           = row["qid"]
                            original_name = row["data_path"].split("?")[0].split("/")[-1] if row.get("data_path") else ""
                            suffix        = "." + original_name.rsplit(".", 1)[-1] if "." in original_name else ""
                            default_fn    = state.get("filenames", {}).get(
                                qid, f"input{'' if idx == 0 else idx + 1}{suffix}"
                            )
                            sql_val = state.get("sql", {}).get(qid, "")
                            with ui.column().classes("w-full gap-2 border border-gray-100 rounded-lg p-3 mb-2"):
                                ui.label(row["name"]).classes("text-xs text-gray-500 uppercase tracking-wide font-semibold")
                                with ui.row().classes("w-full gap-6 items-start"):
                                    with ui.column().classes("gap-0"):
                                        ui.label("Original filename").classes("text-xs text-gray-400")
                                        ui.label(original_name or "—").classes("text-sm font-mono text-gray-700")
                                    with ui.column().classes("flex-1 gap-0"):
                                        ui.label("Target filename").classes("text-xs text-gray-400")
                                        ui.label("(name the model-runner will see)").classes("text-xs text-red-400")
                                        inp = ui.input(
                                            value=default_fn,
                                            on_change=update_payload,
                                        ).classes("w-full font-mono text-sm").style("background-color: #f0fdf4")
                                if _has_sql(sql_val):
                                    with ui.column().classes("gap-0 mt-1"):
                                        ui.label("Transform (SQL)").classes("text-xs text-gray-400")
                                        ui.label(sql_val[:150]).classes("text-xs font-mono text-gray-600 whitespace-pre-wrap")
                            filename_refs.append((row["data_path"], inp, sql_val))

                    # Model summary
                    with ui.element("div").classes("w-full border border-gray-200 rounded-xl p-5 bg-white"):
                        ui.label("Model").classes("text-base font-bold text-gray-900 mb-2")
                        with ui.row().classes("items-center gap-3"):
                            with ui.element("div").classes("w-8 h-8 rounded-lg bg-blue-100 flex items-center justify-center shrink-0"):
                                ui.icon("precision_manufacturing").classes("text-blue-600")
                            with ui.column().classes("gap-0.5"):
                                ui.label(model["name"]).classes("text-sm font-semibold text-gray-800")
                                with ui.element("div").classes("bg-blue-100 text-blue-700 text-xs px-2 py-0.5 rounded font-mono inline-block"):
                                    ui.label(model["docker_tag"])

                    # Config summary
                    with ui.element("div").classes("w-full border border-gray-200 rounded-xl p-5 bg-white"):
                        ui.label("Configuration").classes("text-base font-bold text-gray-900 mb-2")
                        ui.label(state.get("config", "")).classes("text-sm font-mono text-gray-600 whitespace-pre-wrap")

                    # Payload preview
                    with ui.expansion("Prefect Payload (preview)").classes("w-full border border-gray-200 rounded-xl"):
                        payload_lbl = ui.label("").classes("font-mono text-xs whitespace-pre-wrap text-gray-700 p-3")

            if not settings.prefect_api_url:
                with ui.row().classes("w-full items-center gap-2 p-3 bg-orange-50 rounded-lg border border-orange-200"):
                    ui.icon("warning").classes("text-orange-500 shrink-0")
                    ui.label(
                        "PREFECT_API_URL is not configured — pre-flight review only, cannot submit."
                    ).classes("text-sm text-orange-700")

            result_lbl = ui.label("").classes("text-sm text-gray-500")
            result_box = ui.column().classes("gap-1")

        _tv2_footer()

        update_payload()
