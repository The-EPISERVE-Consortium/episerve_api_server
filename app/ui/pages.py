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
        with ui.column().classes("p-6 w-full"):
            ui.label("Run a forecast model").classes("text-xl font-semibold")
            ui.label(
                "Select a processed dataset and a registered model, adjust the configuration if needed, "
                "and submit a new model run. The run will be executed on the cluster and results will "
                "appear in the Model Runs page once complete."
            ).classes("text-sm text-gray-500 mb-4")

            # Dataset selection table
            ui.label("Input dataset").classes("text-sm font-medium text-gray-700 mb-1")
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
                    selection="single",
                    pagination={"rowsPerPage": 5},
                ).classes("w-full mb-4")
                dataset_filter.bind_value(dataset_table, "filter")
            except Exception as e:
                _error_label(f"Could not load datasets: {e}")
                dataset_table = None

            # Model selection table
            ui.label("Model").classes("text-sm font-medium text-gray-700 mb-1")
            try:
                model_rows = ckan_client.list_models()
                model_filter = ui.input(placeholder="Filter by name…").classes("w-64 mb-2")
                model_table = ui.table(
                    columns=[
                        {"name": "name",         "label": "Name",        "field": "name",         "align": "left", "sortable": True},
                        {"name": "docker_tag",   "label": "Tag",         "field": "docker_tag",   "align": "left", "sortable": True},
                        {"name": "description",  "label": "Description", "field": "description",  "align": "left"},
                    ],
                    rows=model_rows,
                    row_key="name",
                    selection="single",
                    pagination={"rowsPerPage": 5},
                ).classes("w-full mb-4")
                model_filter.bind_value(model_table, "filter")
            except Exception as e:
                _error_label(f"Could not load models: {e}")
                model_table = None

            config_input = ui.textarea(
                label="Config (JSON)",
                value='{"horizon_weeks": 4, "n_reference_weeks": 4}',
            ).classes("w-full max-w-xl font-mono")

            result_label = ui.label("").classes("text-sm text-gray-500")

            def submit():
                if not dataset_table or not dataset_table.selected:
                    ui.notify("Select an input dataset", type="warning", position="top")
                    return
                if not model_table or not model_table.selected:
                    ui.notify("Select a model", type="warning", position="top")
                    return
                try:
                    config = json.loads(config_input.value)
                except json.JSONDecodeError as e:
                    ui.notify(f"Invalid JSON: {e}", type="negative", position="top")
                    return

                input_path  = dataset_table.selected[0]["lakefs_path"]
                m           = model_table.selected[0]
                image, tag  = m["docker_image"], m["docker_tag"]

                from app.clients import prefect as prefect_client
                try:
                    result = prefect_client.trigger_model_run(
                        input_path=input_path,
                        model_image=image,
                        model_tag=tag,
                        config_json=json.dumps(config),
                    )
                    result_label.set_text(f"Triggered: {result['prefect_flow_run_id']} ({result['status']})")
                    ui.notify("Model run triggered", type="positive", position="top")
                except Exception as e:
                    ui.notify(f"Prefect error: {e}", type="negative", position="top")

            if not settings.prefect_api_url:
                ui.label("⚠ PREFECT_API_URL is not configured. Set it in .env to enable triggering runs.").classes("text-orange-600 text-sm mb-2")
            ui.button("Trigger Run", icon="play_arrow", on_click=submit).classes("bg-blue-700 text-white mt-2").props(f"{'disabled' if not settings.prefect_api_url else ''}")
            result_label
