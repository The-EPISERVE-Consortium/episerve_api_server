"""Dataset catalog and structured data previews."""

from nicegui import ui, run
from starlette.requests import Request
from app.clients import ckan as ckan_client
from app.config import settings
from app.dataset_preview import FILTER_OPERATORS, MAX_FILTERS, PAGE_SIZES, PreviewFilter, PreviewOptions, load_dataset, query_dataset, resolve_component
from app.ui.layout import header
from app.ui.components.messages import error_label


def register_datasets_page():
    """Register the datasets page route."""
    @ui.page("/ui/datasets")
    def datasets_processed_lab(request: Request):
        """Render catalog browsing and structured dataset preview controls.

        Args:
            request: Incoming request used to construct the API link.
        """
        from datetime import datetime

        header("/ui/datasets", api="datasets")

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
            with ui.column().classes("epi-page-padding"):
                error_label(f"Could not load datasets: {e}")
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
                <q-td :props="props" class="epi-description-cell">
                    <div class="font-semibold text-gray-800 text-sm">{{ props.row.name }}</div>
                    <div class="epi-table-description text-xs text-gray-500 mt-0.5">{{ props.row.description }}</div>
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
                            error_label("Could not preview this component. It must be a valid Parquet file on the configured DOIP service within the preview size limits.")
                    return
                finally:
                    preview_busy[0] = False
                await update_preview()

            tbl.on("selection", on_selection)

        with ui.column().classes("epi-page"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Datasets").classes("epi-page-title")
                    ui.label("Browse, explore and download available datasets.").classes("epi-page-description")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("epi-search"):
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

            with ui.element("div").classes("epi-table-panel"):
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
                    ui.button("Apply", icon="filter_alt", on_click=apply_preview).classes("epi-primary")
                with ui.row().classes("items-center gap-2"):
                    preview_controls["previous"] = ui.button("Previous", on_click=previous_page)
                    preview_controls["page_label"] = ui.label("")
                    preview_controls["next"] = ui.button("Next", on_click=next_page)
            controls_row.set_visibility(False)
            preview_controls_row[0] = controls_row

            _dc = ui.column().classes("w-full")
            data_container_ref[0] = _dc
