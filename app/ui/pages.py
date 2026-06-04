import io
import json
import httpx
import pandas as pd
import duckdb
from nicegui import ui, run

from app.clients import lakefs as lakefs_client
from app.clients import ckan as ckan_client
from app.config import settings


NAV_ITEMS = [
    ("Datasets – Raw",            "/ui/datasets/raw"),
    ("Datasets",                   "/ui/datasets/processed-lab"),
    ("Models",                    "/ui/models"),
    ("Model Runs",                "/ui/model-runs"),
    ("Trigger",                   "/ui/trigger"),
]


def _header(current: str = ""):
    with ui.header().classes("bg-white text-gray-800 border-b border-gray-200 px-8 py-3 flex items-center justify-between shadow-sm"):
        ui.label("EPISERVE").classes("text-lg font-bold tracking-wide")
        with ui.row().classes("gap-8 items-center"):
            for label, path in NAV_ITEMS:
                active = current == path
                ui.link(label, path).classes(
                    "text-sm no-underline font-medium " +
                    ("text-blue-700 border-b-2 border-blue-700 pb-0.5" if active else "text-gray-600 hover:text-blue-700")
                )


def _error_label(msg: str):
    ui.label(f"⚠ {msg}").classes("text-red-600 text-sm mt-2")


def register_pages():

    @ui.page("/ui")
    @ui.page("/ui/datasets/raw")
    def datasets_raw():
        _header("/ui/datasets/raw")
        with ui.column().classes("p-6 w-full"):
            ui.label("Raw Datasets").classes("text-xl font-semibold mb-4")
            try:
                rows = lakefs_client.list_raw_objects()
                filter_input = ui.input(placeholder="Filter by path…").classes("w-64 mb-2")
                table = ui.table(
                    columns=[
                        {"name": "path",          "label": "Path",          "field": "path",          "align": "left",  "sortable": True},
                        {"name": "size_bytes",    "label": "Size (bytes)",  "field": "size_bytes",    "align": "right", "sortable": True},
                        {"name": "last_modified", "label": "Last Modified", "field": "last_modified", "align": "left",  "sortable": True},
                    ],
                    rows=rows,
                    row_key="path",
                    pagination={"rowsPerPage": 20},
                ).classes("w-full")
                filter_input.bind_value(table, "filter")
            except Exception as e:
                _error_label(f"Could not load raw datasets: {e}")

    @ui.page("/ui/datasets/processed-lab")
    def datasets_processed_lab():
        _header("/ui/datasets/processed-lab")
        with ui.column().classes("p-6 w-full"):
            ui.label("Datasets").classes("text-xl font-semibold mb-4")
            try:
                rows = lakefs_client.list_processed_datasets()
                filter_input = ui.input(placeholder="Filter by name…").classes("w-64 mb-2")
                selected_label = ui.label("No row selected.").classes("text-sm text-gray-500 mt-3")
                table = ui.table(
                    columns=[
                        {"name": "name",        "label": "Name",        "field": "name",        "align": "left", "sortable": True},
                        {"name": "qid",         "label": "QID",         "field": "qid",         "align": "left", "sortable": True},
                        {"name": "description", "label": "Description", "field": "description", "align": "left"},
                        {"name": "doip_url",    "label": "Metadata",    "field": "doip_url",    "align": "left"},
                        {"name": "components",  "label": "Download",    "field": "components",  "align": "left"},
                    ],
                    rows=rows,
                    row_key="qid",
                    selection="single",
                    pagination={"rowsPerPage": 5},
                ).classes("w-full")
                table.add_slot('body-cell-doip_url', r'<q-td :props="props"><a :href="props.row.doip_url" target="_blank" class="text-blue-600 hover:underline">Show Metadata</a></q-td>')
                table.add_slot('body-cell-components', r'<q-td :props="props"><span v-for="c in props.row.components" :key="c.name"><a :href="c.url" target="_blank" class="text-blue-600 hover:underline block">{{ c.name }}</a></span></q-td>')
                filter_input.bind_value(table, "filter")

                # mutable state shared across closures
                current_df   = [None]
                current_name = [""]

                # SQL input row — hidden until a dataset is loaded
                with ui.row().classes("w-full mt-6 items-end gap-2") as sql_row:
                    sql_input = ui.input(label="SQL query", value="SELECT * FROM df").classes("flex-1 font-mono text-sm")
                    run_btn   = ui.button("Run", icon="play_arrow").classes("bg-blue-700 text-white")
                sql_row.set_visibility(False)

                data_container = ui.column().classes("w-full mt-2")

                def render_preview(df):
                    data_container.clear()
                    with data_container:
                        ui.label(f"{current_name[0]} — {len(df):,} rows × {len(df.columns)} columns").classes("text-sm text-gray-500 mb-2")
                        cols = [{"name": c, "label": c, "field": c, "align": "left", "sortable": True} for c in df.columns]
                        preview_rows = df.head(500).astype(str).to_dict("records")
                        ui.table(columns=cols, rows=preview_rows, row_key=df.columns[0], pagination={"rowsPerPage": 10}).classes("w-full")

                def run_sql():
                    df = current_df[0]
                    if df is None:
                        return
                    if not sql_input.value.strip():
                        sql_input.value = "SELECT * FROM df"
                        render_preview(df)
                        return
                    try:
                        conn = duckdb.connect()
                        conn.register("df", df)
                        result = conn.execute(sql_input.value).df()
                        render_preview(result)
                    except Exception as ex:
                        data_container.clear()
                        with data_container:
                            _error_label(f"SQL error: {ex}")

                run_btn.on("click", run_sql)

                async def on_selection(e):
                    selected_rows = e.args.get("rows", [])
                    data_container.clear()
                    sql_row.set_visibility(False)
                    current_df[0] = None
                    if not selected_rows:
                        selected_label.set_text("No row selected.")
                        return
                    row = selected_rows[0]
                    selected_label.set_text(f"Selected: {row['name']} ({row['qid']})")
                    components = row.get("components", [])
                    if not components:
                        with data_container:
                            ui.label("No downloadable components for this dataset.").classes("text-sm text-gray-500")
                        return
                    component = components[0]
                    current_name[0] = component["name"]
                    with data_container:
                        ui.spinner(size="lg")
                    try:
                        async with httpx.AsyncClient() as client:
                            response = await client.get(component["url"])
                            response.raise_for_status()
                        df = await run.io_bound(pd.read_parquet, io.BytesIO(response.content))
                        current_df[0] = df
                        sql_input.value = "SELECT * FROM df"
                        sql_row.set_visibility(True)
                        render_preview(df)
                    except Exception as ex:
                        data_container.clear()
                        with data_container:
                            _error_label(f"Could not load component: {ex}")

                table.on("selection", on_selection)
            except Exception as e:
                _error_label(f"Could not load processed datasets: {e}")

    @ui.page("/ui/models")
    def models():
        _header("/ui/models")
        with ui.column().classes("p-6 w-full"):
            ui.label("Models").classes("text-xl font-semibold mb-4")
            try:
                rows = ckan_client.list_models()
                filter_input = ui.input(placeholder="Filter by name…").classes("w-64 mb-2")
                table = ui.table(
                    columns=[
                        {"name": "name",         "label": "Name",        "field": "name",         "align": "left", "sortable": True},
                        {"name": "docker_image", "label": "Image",       "field": "docker_image", "align": "left"},
                        {"name": "docker_tag",   "label": "Tag",         "field": "docker_tag",   "align": "left", "sortable": True},
                        {"name": "description",  "label": "Description", "field": "description",  "align": "left"},
                    ],
                    rows=rows,
                    row_key="name",
                    pagination={"rowsPerPage": 20},
                ).classes("w-full")
                filter_input.bind_value(table, "filter")
            except Exception as e:
                _error_label(f"Could not load models: {e}")

    @ui.page("/ui/model-runs")
    def model_runs():
        _header("/ui/model-runs")
        with ui.column().classes("p-6 w-full"):
            ui.label("Model Runs").classes("text-xl font-semibold mb-4")
            try:
                rows = lakefs_client.list_model_runs()
                filter_input = ui.input(placeholder="Filter by model…").classes("w-64 mb-2")
                table = ui.table(
                    columns=[
                        {"name": "qid",           "label": "QID",       "field": "qid",           "align": "left", "sortable": True},
                        {"name": "model_name",    "label": "Model",     "field": "model_name",    "align": "left", "sortable": True},
                        {"name": "docker_tag",    "label": "Tag",       "field": "docker_tag",    "align": "left"},
                        {"name": "run_timestamp", "label": "Timestamp", "field": "run_timestamp", "align": "left", "sortable": True},
                        {"name": "doip_url",      "label": "DOIP",      "field": "doip_url",      "align": "left"},
                    ],
                    rows=rows,
                    row_key="qid",
                    pagination={"rowsPerPage": 20},
                ).classes("w-full")
                filter_input.bind_value(table, "filter")
            except Exception as e:
                _error_label(f"Could not load model runs: {e}")

    @ui.page("/ui/trigger")
    def trigger():
        _header("/ui/trigger")

        def _step_card(n: int, title: str, status_text: str, status_done: bool = False):
            """Returns (card context, status_label). Use as: with _step_card(...) as (card, lbl):"""
            card = ui.card().classes("w-full p-5")
            card.__enter__()
            with ui.row().classes("items-center gap-3 mb-4"):
                ui.label(str(n)).classes(
                    "bg-blue-700 text-white rounded-full w-7 h-7 flex items-center "
                    "justify-center text-sm font-bold shrink-0"
                )
                ui.label(title).classes("text-base font-semibold text-gray-800")
                lbl = ui.label(status_text).classes(
                    "text-sm ml-auto " + ("text-green-600" if status_done else "text-gray-400")
                )
            return card, lbl

        with ui.column().classes("p-6 w-full gap-4"):
            ui.label("Run a forecast model").classes("text-xl font-semibold")
            ui.label(
                "Complete each step below, then click Pre-flight Check to review and submit."
            ).classes("text-sm text-gray-500")

            # ── Step 1: Input Datasets ──────────────────────────────────
            card1, dataset_status = _step_card(1, "Input Datasets", "Select one or more datasets")
            try:
                dataset_rows = lakefs_client.list_processed_datasets()
                dataset_filter = ui.input(placeholder="Filter by name…").classes("w-64 mb-2")
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
            card1.__exit__(None, None, None)

            # ── Step 2: Model ───────────────────────────────────────────
            card2, model_status = _step_card(2, "Model", "Select a model")
            try:
                model_rows = ckan_client.list_models()
                model_filter = ui.input(placeholder="Filter by name…").classes("w-64 mb-2")
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
                model_filter.bind_value(model_table, "filter")

                def on_model_selection(e):
                    if model_table.selected:
                        m = model_table.selected[0]
                        model_status.set_text(f"✓ {m['name']} ({m['docker_tag']})")
                        model_status.classes(remove="text-gray-400", add="text-green-600")
                    else:
                        model_status.set_text("Select a model")
                        model_status.classes(remove="text-green-600", add="text-gray-400")

                model_table.on("selection", on_model_selection)
            except Exception as e:
                _error_label(f"Could not load models: {e}")
                model_table = None
            card2.__exit__(None, None, None)

            # ── Step 3: Dataset Transformation ──────────────────────────
            card3, sql_status = _step_card(3, "Dataset Transformation", "Optional")
            ui.label(
                "SQL applied to each selected dataset before it is passed to the model-runner."
            ).classes("text-sm text-gray-500 mb-3")
            dataset_sql_inputs = {}
            sql_container = ui.column().classes("w-full gap-4")

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
                        with ui.column().classes("w-full gap-1 border-l-2 border-blue-200 pl-3"):
                            ui.label(row["name"]).classes("text-sm font-medium text-gray-700")
                            with ui.row().classes("w-full items-end gap-2"):
                                inp = ui.textarea(
                                    label="SQL transformation",
                                    value=prev,
                                    placeholder="SELECT * FROM df WHERE …",
                                ).classes("flex-1 font-mono text-sm")
                                ui.button("Verify", icon="check", on_click=_make_verify(inp)).classes("bg-gray-600 text-white shrink-0")
                        dataset_sql_inputs[qid] = inp
                # update dataset status
                n = len(selected)
                if n:
                    dataset_status.set_text(f"✓ {n} dataset(s) selected")
                    dataset_status.classes(remove="text-gray-400", add="text-green-600")
                else:
                    dataset_status.set_text("Select one or more datasets")
                    dataset_status.classes(remove="text-green-600", add="text-gray-400")
                # update sql status
                filled = sum(1 for inp in dataset_sql_inputs.values() if inp.value.strip())
                if filled:
                    sql_status.set_text(f"✓ {filled} transformation(s) defined")
                    sql_status.classes(remove="text-gray-400", add="text-green-600")
                else:
                    sql_status.set_text("Optional")
                    sql_status.classes(remove="text-green-600", add="text-gray-400")

            if dataset_table:
                dataset_table.on("selection", rebuild_sql_inputs)
            card3.__exit__(None, None, None)

            # ── Step 4: Config ──────────────────────────────────────────
            card4, _ = _step_card(4, "Config (JSON)", "")
            config_input = ui.textarea(
                value='{"horizon_weeks": 4, "n_reference_weeks": 4}',
            ).classes("w-full max-w-xl font-mono")
            card4.__exit__(None, None, None)

            result_label = ui.label("").classes("text-sm text-gray-500")

            # ── Pre-flight dialog ───────────────────────────────────────
            filename_inputs = []

            with ui.dialog() as preflight_dialog:
                with ui.card().classes("min-w-[32rem] p-6"):
                    ui.label("Pre-flight Check").classes("text-lg font-semibold mb-4")
                    with ui.column().classes("gap-3 w-full"):
                        with ui.column().classes("gap-1"):
                            ui.label("Input Datasets").classes("text-xs text-gray-500 uppercase tracking-wide")
                            inputs_container = ui.column().classes("w-full gap-2")
                        with ui.column().classes("gap-0"):
                            ui.label("Model").classes("text-xs text-gray-500 uppercase tracking-wide")
                            model_summary = ui.label("").classes("text-sm text-gray-800")
                        with ui.column().classes("gap-0"):
                            ui.label("Dataset Transformation").classes("text-xs text-gray-500 uppercase tracking-wide")
                            transformation_summary = ui.label("").classes("text-sm text-gray-800 font-mono whitespace-pre-wrap")
                        with ui.column().classes("gap-0"):
                            ui.label("Config").classes("text-xs text-gray-500 uppercase tracking-wide")
                            config_summary = ui.label("").classes("text-sm text-gray-800 font-mono whitespace-pre-wrap")
                    with ui.expansion("Prefect Payload").classes("w-full mt-4 text-xs text-gray-500"):
                        payload_label = ui.label("").classes("font-mono text-xs whitespace-pre-wrap text-gray-700")
                    with ui.row().classes("mt-4 gap-2 justify-end w-full"):
                        ui.button("Cancel", on_click=preflight_dialog.close).classes("text-gray-600")
                        confirm_btn = ui.button("Trigger Run", icon="play_arrow").classes("bg-blue-700 text-white").props(f"{'disabled' if not settings.prefect_api_url else ''}")

            def do_submit():
                preflight_dialog.close()
                m = model_table.selected[0]
                try:
                    config = json.loads(config_input.value)
                except json.JSONDecodeError as e:
                    ui.notify(f"Invalid JSON: {e}", type="negative", position="top")
                    return
                input_data_files        = [[dp, inp.value.strip()] for dp, inp, _ in filename_inputs]
                data_transformation_sql = [sql_inp.value.strip() for _, _, sql_inp in filename_inputs]
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
                            "data_transformation_sql": [sql_inp.value.strip() for _, _, sql_inp in filename_inputs],
                        }
                    }
                    payload_label.set_text(json.dumps(payload, indent=2))

                filename_inputs.clear()
                inputs_container.clear()
                with inputs_container:
                    for row in dataset_table.selected:
                        qid = row["qid"]
                        default_name = row["data_path"].split("/")[-1] if row.get("data_path") else ""
                        sql_val = dataset_sql_inputs[qid].value.strip() if qid in dataset_sql_inputs else ""
                        with ui.column().classes("w-full gap-1 border-b border-gray-100 pb-2"):
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.label(row["name"]).classes("text-sm text-gray-600 w-40 truncate shrink-0")
                                inp = ui.input(value=default_name, on_change=update_payload).classes("flex-1 font-mono text-sm")
                            sql_inp = ui.input(label="SQL transformation", value=sql_val, on_change=update_payload).classes("w-full font-mono text-sm")
                        filename_inputs.append((row["data_path"], inp, sql_inp))

                sqls = [dataset_sql_inputs[r["qid"]].value.strip() for r in dataset_table.selected if r["qid"] in dataset_sql_inputs]
                model_summary.set_text(f"{m['name']} ({m['docker_tag']})")
                transformation_summary.set_text(", ".join(s for s in sqls if s) or "— none —")
                config_summary.set_text(config_input.value.strip())
                update_payload()
                preflight_dialog.open()

            if not settings.prefect_api_url:
                ui.label(
                    "⚠ PREFECT_API_URL is not configured. Set it in .env to enable triggering runs. "
                    "You can do the pre-flight check but will not be able to submit to Prefect."
                ).classes("text-orange-600 text-sm mb-2")
            ui.button("Pre-flight Check", icon="checklist", on_click=open_preflight).classes("bg-blue-700 text-white mt-2")
            result_label
