"""Searchable forecast model catalog."""

from nicegui import ui
from starlette.requests import Request
from app.clients import ckan as ckan_client
from app.ui.layout import header
from app.ui.components.messages import error_label


def register_models_page():
    """Register the models page route."""
    @ui.page("/ui/models")
    def models(request: Request):
        """Render the searchable forecast model catalog.

        Args:
            request: Incoming HTTP request used by the page.
        """
        header("/ui/models", api="models")

        try:
            raw_rows = ckan_client.list_models()
        except Exception as e:
            with ui.column().classes("epi-page-padding"):
                error_label(f"Could not load models: {e}")
            return

        def _fmt_date_model(dt_str: str) -> tuple:
            """Format an image creation date as an absolute and relative date.

            Args:
                dt_str: Timestamp from catalog metadata.

            Returns:
                The formatted creation date and its relative age.
            """
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
            """Apply the current search and sort controls, then refresh the results."""
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
            """Render the number of currently visible catalog entries."""
            n = len(filtered_rows)
            ui.label(f"Showing {n} model{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        @ui.refreshable
        def model_table():
            """Render the filtered model table and its metadata links."""
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
                <q-td :props="props" class="epi-description-cell">
                    <div class="font-semibold text-gray-800 text-sm">{{ props.row.name }}</div>
                    <div class="epi-table-description text-xs text-gray-500 mt-0.5">{{ props.row.description }}</div>
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

        with ui.column().classes("epi-page"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Models").classes("epi-page-title")
                    ui.label("Browse available forecast models.").classes("epi-page-description")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("epi-search"):
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

            with ui.element("div").classes("epi-table-panel"):
                model_table()
