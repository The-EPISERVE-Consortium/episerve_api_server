import hmac
import json
import duckdb
import pandas as pd
from nicegui import ui, run, app as _napp
from starlette.requests import Request

from app.auth import _daily_token
from app.clients import ckan as ckan_client
from app.config import settings


NAV_ITEMS = [
    ("Datasets",        "/ui/datasets"),
    ("Models",          "/ui/models"),
    ("Model Runs",      "/ui/model-runs"),
    ("Run Workflow",        "/ui/run_workflow/1"),
    ("Run Workflow Simple", "/ui/trigger"),
]


def _header(current: str = ""):
    with ui.header().classes("bg-white text-gray-800 border-b border-gray-200 px-8 py-3 flex items-center justify-between shadow-sm"):
        ui.label("EPISERVE").classes("text-lg font-bold tracking-wide")
        with ui.row().classes("gap-8 items-center"):
            for label, path in NAV_ITEMS:
                active = current == path or (
                    "/run_workflow" in path and current.startswith("/ui/run_workflow")
                )
                ui.link(label, path).classes(
                    "text-sm no-underline font-medium " +
                    ("text-blue-700 border-b-2 border-blue-700 pb-0.5" if active else "text-gray-600 hover:text-blue-700")
                )

            # Token dialog
            with ui.dialog() as token_dialog, ui.card().classes("w-full max-w-lg p-6"):
                ui.label("Current Token").classes("text-base font-semibold text-gray-800 mb-3")
                with ui.row().classes("items-center gap-2 w-full"):
                    token_label = ui.label("").classes(
                        "font-mono text-sm bg-gray-100 rounded px-3 py-2 break-all flex-1"
                    )
                    def _copy_token():
                        ui.run_javascript(f"navigator.clipboard.writeText({repr(_napp.storage.user.get('token', ''))})")
                        ui.notify("Copied to clipboard", position="top", type="positive")
                    ui.button(icon="content_copy", on_click=_copy_token).props("flat round dense").classes("text-gray-500")
                ui.button("Close", on_click=token_dialog.close).classes("mt-4 self-end").props("flat")

            def _open_token_dialog():
                token_label.set_text(_napp.storage.user.get("token", ""))
                token_dialog.open()

            # Avatar button + dropdown menu
            with ui.element("div").classes("relative"):
                with ui.button(icon="account_circle").props("flat round").classes("text-gray-600 hover:text-blue-700"):
                    with ui.menu().classes("mt-1"):
                        ui.menu_item("Show current token", on_click=_open_token_dialog)
                        ui.separator()
                        ui.menu_item("Logout", on_click=lambda: (
                            _napp.storage.user.clear(),
                            ui.navigate.to("/login"),
                        ))


def _error_label(msg: str):
    ui.label(f"⚠ {msg}").classes("text-red-600 text-sm mt-2")


def _require_login() -> bool:
    if _napp.storage.user.get("token") != _daily_token():
        ui.navigate.to("/login")
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
        }
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


# ─── Page registrations ────────────────────────────────────────────────────────

def register_pages():

    @ui.page("/login")
    def login_page():
        if _napp.storage.user.get("token") == _daily_token():
            ui.navigate.to("/ui")
            return

        error = ui.label("").classes("text-red-600 text-sm hidden")

        def do_login():
            valid = (
                hmac.compare_digest(username_inp.value, settings.auth_username)
                and hmac.compare_digest(password_inp.value, settings.auth_password)
            )
            if valid:
                _napp.storage.user["token"] = _daily_token()
                ui.navigate.to("/ui")
            else:
                error.set_text("Invalid username or password")
                error.classes(remove="hidden")

        with ui.column().classes("w-full h-screen items-center justify-center bg-gray-50"):
            with ui.element("div").classes("w-full max-w-sm bg-white border border-gray-200 rounded-2xl shadow-sm p-8 gap-0"):
                ui.label("EPISERVE").classes("text-xl font-bold text-gray-900 mb-1")
                ui.label("Sign in to continue").classes("text-sm text-gray-500 mb-6")
                username_inp = ui.input("Username").classes("w-full mb-3")
                password_inp = ui.input("Password").props("type=password").classes("w-full mb-1")
                error
                ui.button("Sign in", on_click=do_login).classes("w-full bg-blue-700 text-white mt-4")

    @ui.page("/ui")
    def _root():
        if not _require_login():
            return
        ui.navigate.to("/ui/datasets")

    @ui.page("/ui/datasets")
    def datasets_processed_lab(request: Request):
        if not _require_login():
            return
        from datetime import datetime

        _header("/ui/datasets")

        def _fmt_date(dt_str: str) -> tuple:
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

        current_url  = [None]
        current_name = [""]

        def apply_filters():
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
            n = len(filtered_rows)
            ui.label(f"Showing {n} dataset{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        sql_row       = [None]
        sql_input_ref = [None]
        data_container_ref = [None]

        def _duckdb_query(url: str, sql: str):
            conn = duckdb.connect()
            conn.execute("INSTALL httpfs; LOAD httpfs")
            conn.execute(f"CREATE VIEW df AS SELECT * FROM read_parquet('{url}')")
            result = conn.execute(sql).df()
            conn.close()
            return result

        def render_preview(df, container):
            container.clear()
            with container:
                ui.label(f"{current_name[0]} — {len(df):,} rows × {len(df.columns)} columns").classes("text-sm text-gray-500 mb-2")
                cols = [{"name": c, "label": c, "field": c, "align": "left", "sortable": True} for c in df.columns]
                ui.table(
                    columns=cols,
                    rows=df.head(500).astype(str).to_dict("records"),
                    row_key=df.columns[0],
                    pagination={"rowsPerPage": 10},
                ).classes("w-full")

        @ui.refreshable
        def ds_table():
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
                    <span class="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600">Dataset</span>
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
                selected = e.args.get("rows", [])
                sr = sql_row[0]
                dc = data_container_ref[0]
                if sr:
                    sr.set_visibility(False)
                if dc:
                    dc.clear()
                current_url[0] = None
                if not selected:
                    return
                row = selected[0]
                components = row.get("components", [])
                if not components:
                    if dc:
                        with dc:
                            ui.label("No downloadable components for this dataset.").classes("text-sm text-gray-500")
                    return
                component = components[0]
                current_name[0] = component["name"]
                current_url[0]  = component["url"]
                if dc:
                    with dc:
                        ui.spinner(size="lg")
                try:
                    df = await run.io_bound(_duckdb_query, component["url"], "SELECT * FROM df LIMIT 500")
                    if sql_input_ref[0]:
                        sql_input_ref[0].value = "SELECT * FROM df LIMIT 500"
                    if sr:
                        sr.set_visibility(True)
                    if dc:
                        render_preview(df, dc)
                except Exception as ex:
                    if dc:
                        dc.clear()
                        with dc:
                            _error_label(f"Could not load component: {ex}")

            tbl.on("selection", on_selection)

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Datasets").classes("text-3xl font-bold text-gray-900")
                    ui.label("Browse, explore and download available datasets.").classes("text-sm text-gray-500 mt-1")
                with ui.element("div").classes("bg-gray-50 border border-gray-200 rounded-lg px-4 py-2 shrink-0"):
                    ui.label("API").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1")
                    ui.label(f"curl {request.base_url}datasets").classes("font-mono text-xs text-gray-700")

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

            async def run_sql():
                url = current_url[0]
                if url is None:
                    return
                dc = data_container_ref[0]
                query = sql_input_ref[0].value.strip() if sql_input_ref[0] else "SELECT * FROM df LIMIT 500"
                try:
                    result = await run.io_bound(_duckdb_query, url, query or "SELECT * FROM df LIMIT 500")
                    if dc:
                        render_preview(result, dc)
                except Exception as ex:
                    if dc:
                        dc.clear()
                        with dc:
                            _error_label(f"SQL error: {ex}")

            with ui.row().classes("w-full items-end gap-2") as _sql_row:
                sql_inp = ui.input(label="SQL query", value="SELECT * FROM df LIMIT 500").classes("flex-1 font-mono text-sm")
                ui.button("Run", icon="play_arrow", on_click=run_sql).classes("bg-blue-700 text-white")
            _sql_row.set_visibility(False)
            sql_row[0]       = _sql_row
            sql_input_ref[0] = sql_inp

            _dc = ui.column().classes("w-full")
            data_container_ref[0] = _dc

    @ui.page("/ui/models")
    def models(request: Request):
        if not _require_login():
            return
        _header("/ui/models")

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
                with ui.element("div").classes("bg-gray-50 border border-gray-200 rounded-lg px-4 py-2 shrink-0"):
                    ui.label("API").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1")
                    ui.label(f"curl {request.base_url}models").classes("font-mono text-xs text-gray-700")

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
        if not _require_login():
            return
        from datetime import datetime
        _header("/ui/model-runs")

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
                selected = e.args.get("rows", [])
                preview_container[0].clear()
                if not selected:
                    return
                row = selected[0]
                doip_url = row.get("doip_url", "")
                if not doip_url:
                    return

                dc = preview_container[0]
                with dc:
                    with ui.row().classes("items-center gap-2"):
                        ui.spinner(size="sm")
                        ui.label("Fetching metadata…").classes("text-sm text-gray-400")

                def _fetch_metadata(url: str) -> dict:
                    import requests
                    return requests.get(url, timeout=30).json()

                def _find_components(meta: dict):
                    components = meta.get("kernel", {}).get("fdo:hasComponent", [])
                    if isinstance(components, dict):
                        components = [components]
                    result = []
                    for comp in components:
                        cid = str(comp.get("componentId", ""))
                        for ext in (".parquet", ".tsv", ".csv"):
                            if cid.lower().endswith(ext):
                                result.append((comp, cid, ext))
                                break
                    return result

                def _fetch_preview(url: str, ext: str, limit: int = 500):
                    if ext == ".parquet":
                        conn = duckdb.connect()
                        conn.execute("INSTALL httpfs; LOAD httpfs")
                        total = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{url}')").fetchone()[0]
                        df = conn.execute(f"SELECT * FROM read_parquet('{url}') LIMIT {limit}").df()
                        conn.close()
                        df.attrs["total_rows"] = total
                        return df
                    import requests, io, pandas as pd
                    resp = requests.get(url, timeout=60)
                    resp.raise_for_status()
                    sep = "\t" if ext == ".tsv" else ","
                    df = pd.read_csv(io.StringIO(resp.text), sep=sep, nrows=limit)
                    df.attrs["total_rows"] = None
                    return df

                def _component_url(base: str, qid: str, comp: dict, cid: str) -> str:
                    path = comp.get("@id", f"output/{cid}").removeprefix("components/")
                    return f"{base}/doip/retrieve/{qid}/{path}"

                def _preview_card(dc, icon: str, label: str, df):
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
                    meta = await run.io_bound(_fetch_metadata, doip_url)
                except Exception as exc:
                    dc.clear()
                    with dc:
                        _error_label(f"Could not fetch metadata: {exc}")
                    return

                all_comps = _find_components(meta)
                dc.clear()
                if not any("predictions" in cid.lower() for _, cid, _ in all_comps):
                    with dc:
                        ui.label("No predictions component found in metadata.").classes("text-sm text-gray-400 italic")
                    return

                qid  = meta.get("@id", row.get("qid", ""))
                base = settings.doip_url.rstrip("/")

                with dc:
                    with ui.row().classes("items-center gap-2"):
                        ui.spinner(size="sm")
                        ui.label("Loading files…").classes("text-sm text-gray-400")

                cur_limit = [500]
                cards_container = [None]

                # Load all components
                loaded = []  # list of (cid, df)
                for comp, cid, ext in all_comps:
                    url = _component_url(base, qid, comp, cid)
                    try:
                        df = await run.io_bound(_fetch_preview, url, ext, cur_limit[0])
                        loaded.append((cid, df))
                    except Exception as exc:
                        with dc:
                            _error_label(f"Could not load {cid}: {exc}")

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

                # Default y to first non-x column of the predictions file
                pred_cid = next((cid for cid, _ in loaded if "predictions" in cid.lower()), "")
                pred_y_opts = [k for k in opt_labels if k.startswith(f"{pred_cid}: ") and not k.endswith(": x_auto_converted")]
                cur_ys = [[pred_y_opts[0]] if pred_y_opts else ([opt_labels[0]] if opt_labels else [])]

                dc.clear()
                with dc:
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
                            if numeric_x:
                                for yl in yl_list:
                                    if yl not in col_map:
                                        continue
                                    y_df, y_col = col_map[yl]
                                    # Use x from the same file if the column exists there
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

                        def on_x(e): cur_x[0] = e.value; update_chart()
                        def on_y(e): cur_ys[0] = e.value if isinstance(e.value, list) else [e.value]; update_chart()

                        async def on_limit(e):
                            try:
                                v = int(e.value or 0)
                            except (ValueError, TypeError):
                                return
                            if v <= 0 or v == cur_limit[0]:
                                return
                            cur_limit[0] = v
                            new_loaded = []
                            for comp2, cid2, ext2 in all_comps:
                                url2 = _component_url(base, qid, comp2, cid2)
                                try:
                                    df2 = await run.io_bound(_fetch_preview, url2, ext2, v)
                                    new_loaded.append((cid2, df2))
                                except Exception:
                                    pass
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
                            ui.number(label="Max rows", value=cur_limit[0], min=1, step=100,
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

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Model Runs").classes("text-3xl font-bold text-gray-900")
                    ui.label("Browse past model run results.").classes("text-sm text-gray-500 mt-1")
                with ui.element("div").classes("bg-gray-50 border border-gray-200 rounded-lg px-4 py-2 shrink-0"):
                    ui.label("API").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mb-1")
                    ui.label(f"curl {request.base_url}model-runs").classes("font-mono text-xs text-gray-700")

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

    @ui.page("/ui/trigger")
    def trigger():
        if not _require_login():
            return
        _header("/ui/trigger")

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
                        duckdb.connect().execute(f"EXPLAIN {sql}")
                        ui.notify("SQL syntax is valid", type="positive", position="top")
                    except duckdb.ParserException as ex:
                        ui.notify(f"Syntax error: {ex}", type="negative", position="top")
                    except Exception:
                        ui.notify("SQL syntax is valid", type="positive", position="top")
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
        if not _require_login():
            return
        ui.navigate.to("/ui/run_workflow/1")

    # Step 1 — Datasets ───────────────────────────────────────────────────────

    @ui.page("/ui/run_workflow/1")
    def trigger_v2_step1():
        if not _require_login():
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
            _napp.storage.user["tv2"] = state
            dataset_cards.refresh()
            bottom_bar.refresh()
            run_summary.refresh()

        def clear_all():
            selected_qids.clear()
            state["datasets"] = []
            _napp.storage.user["tv2"] = state
            dataset_cards.refresh()
            bottom_bar.refresh()
            run_summary.refresh()

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

        _header("/ui/run_workflow/1")
        with ui.column().classes("px-8 py-6 w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("text-3xl font-bold text-gray-900")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            _tv2_stepper(1)
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
        if not _require_login():
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
                    duckdb.connect().execute(f"EXPLAIN {sql}")
                    ui.notify("SQL syntax is valid", type="positive", position="top")
                except duckdb.ParserException as ex:
                    ui.notify(f"Syntax error: {ex}", type="negative", position="top")
                except Exception:
                    ui.notify("SQL syntax is valid", type="positive", position="top")
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

        _header("/ui/run_workflow/2")
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
        if not _require_login():
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

        _header("/ui/run_workflow/3")
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
        if not _require_login():
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

        _header("/ui/run_workflow/4")
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
                            {p["name"]: p.get("value") for p in params}, indent=2
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
        if not _require_login():
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
                json.loads(state.get("config", "{}"))
            except json.JSONDecodeError as e:
                ui.notify(f"Invalid config JSON: {e}", type="negative", position="top")
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
                )
                result_lbl.set_text(f"Triggered: {result['prefect_flow_run_id']} ({result['status']})")
                result_lbl.classes(remove="text-gray-500", add="text-green-600 font-medium")
                ui.notify("Model run triggered", type="positive", position="top")
            except Exception as exc:
                ui.notify(f"Prefect error: {exc}", type="negative", position="top")

        _header("/ui/run_workflow/5")
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
            with ui.column().classes("w-full gap-4"):

                    # Input datasets + filename editing
                    with ui.element("div").classes("w-full border border-gray-200 rounded-xl p-5 bg-white"):
                        ui.label("Input Datasets").classes("text-base font-bold text-gray-900 mb-3")
                        for idx, row in enumerate(datasets):
                            qid           = row["qid"]
                            original_name = row["data_path"].split("/")[-1] if row.get("data_path") else ""
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

        _tv2_footer()

        update_payload()
