"""Model-run catalog, provenance, and component previews."""

import pandas as pd
from nicegui import ui, run
from starlette.requests import Request
from app.clients import ckan as ckan_client
from app.config import settings
from app.model_run_preview import MAX_PREVIEW_ROWS, fetch_component_preview, fetch_run_metadata, find_run_components, resolve_model_run
from app.ui.layout import header
from app.ui.components.messages import error_label


def register_model_runs_page():
    """Register the model runs page routes."""
    @ui.page("/ui/model-runs")
    def model_runs(request: Request):
        """Render model runs using validated, bounded component previews.

        Args:
            request: Incoming request used to construct the API link.
        """
        from datetime import datetime
        header("/ui/model-runs", api="model-runs")

        def _fmt_date(dt_str: str) -> tuple:
            """Format a catalog timestamp for display.

            Args:
                dt_str: Timestamp from catalog metadata.

            Returns:
                The formatted timestamp and a relative age, with a fallback for invalid values.
            """
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
            """Return the display classes for a model-run status.

            Args:
                status: Recorded model-run status.

            Returns:
                Utility classes for the background and text color of the run status.
            """
            s = (status or "").lower()
            if s in ("completed", "success"):   return "bg-green-100 text-green-700"
            if s in ("failed", "error"):        return "bg-red-100 text-red-700"
            if s in ("running", "in_progress"): return "bg-blue-100 text-blue-700"
            return "bg-gray-100 text-gray-600"

        try:
            raw_rows = ckan_client.list_model_runs()
        except Exception as e:
            with ui.column().classes("epi-page-padding"):
                error_label(f"Could not load model runs: {e}")
            return

        try:
            dataset_map = {ds["qid"]: ds["name"] for ds in ckan_client.list_processed_datasets()}
        except Exception:
            dataset_map = {}

        def _resolve_inputs(qids: list) -> list[str]:
            """Resolve input dataset identifiers to display names and links.

            Args:
                qids: Input dataset identifiers from the run metadata.

            Returns:
                Dataset display names, falling back to the original identifier.
            """
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
            """Apply the current search and sort controls, then refresh the results."""
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
            """Render the number of currently visible catalog entries."""
            n = len(filtered_rows)
            ui.label(f"Showing {n} run{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        @ui.refreshable
        def runs_table():
            """Render the filtered model-run table and its action controls."""
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
                        error_label("Select one known model run.")
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
                        with ui.element("div").classes("epi-table-panel"):
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
                        error_label("Could not load valid model-run metadata and approved component URLs.")
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
                                error_label(f"Could not preview {component.label} within the size and format limits.")
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
                            """Refresh the preview chart using the selected axes."""
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
                            """Update the preview chart x-axis selection.

                            Args:
                                e: UI event containing the selected row or changed value.
                            """
                            cur_x[0] = e.value
                            cur_xlabel[0] = _companion_label_col(e.value)
                            if xlabel_sel:
                                xlabel_sel.value = cur_xlabel[0]
                            update_chart()
                        def on_xlabel(e):
                            """Update the preview chart x-axis label selection.

                            Args:
                                e: Axis label selection event.
                            """
                            cur_xlabel[0] = e.value
                            update_chart()

                        def on_y(e):
                            """Update the preview chart y-axis selection.

                            Args:
                                e: Axis selection event containing one or more columns.
                            """
                            cur_ys[0] = e.value if isinstance(e.value, list) else [e.value]
                            update_chart()

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

        with ui.column().classes("epi-page"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Model Runs").classes("epi-page-title")
                    ui.label("Browse past model run results.").classes("epi-page-description")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("epi-search"):
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

            with ui.element("div").classes("epi-table-panel"):
                runs_table()

            preview_container[0] = ui.column().classes("w-full")
