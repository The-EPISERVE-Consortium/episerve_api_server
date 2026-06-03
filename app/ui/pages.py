import json
from nicegui import ui

from app.clients import lakefs as lakefs_client
from app.clients import ckan as ckan_client


NAV_ITEMS = [
    ("Datasets – Raw",       "/ui/datasets/raw"),
    ("Datasets – Processed", "/ui/datasets/processed"),
    ("Models",               "/ui/models"),
    ("Model Runs",           "/ui/model-runs"),
    ("Trigger",              "/ui/trigger"),
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
                ui.table(
                    columns=[
                        {"name": "path",          "label": "Path",          "field": "path",          "align": "left",  "sortable": True},
                        {"name": "size_bytes",    "label": "Size (bytes)",  "field": "size_bytes",    "align": "right", "sortable": True},
                        {"name": "last_modified", "label": "Last Modified", "field": "last_modified", "align": "left",  "sortable": True},
                    ],
                    rows=rows,
                    row_key="path",
                ).classes("w-full")
            except Exception as e:
                _error_label(f"Could not load raw datasets: {e}")

    @ui.page("/ui/datasets/processed")
    def datasets_processed():
        _header("/ui/datasets/processed")
        with ui.column().classes("p-6 w-full"):
            ui.label("Processed Datasets").classes("text-xl font-semibold mb-4")
            try:
                rows = lakefs_client.list_processed_datasets()
                ui.table(
                    columns=[
                        {"name": "name",          "label": "Name",          "field": "name",          "align": "left", "sortable": True},
                        {"name": "qid",           "label": "QID",           "field": "qid",           "align": "left", "sortable": True},
                        {"name": "description",   "label": "Description",   "field": "description",   "align": "left"},
                        {"name": "doip_url",      "label": "DOIP",          "field": "doip_url",      "align": "left"},
                    ],
                    rows=rows,
                    row_key="qid",
                ).classes("w-full")
            except Exception as e:
                _error_label(f"Could not load processed datasets: {e}")

    @ui.page("/ui/models")
    def models():
        _header("/ui/models")
        with ui.column().classes("p-6 w-full"):
            ui.label("Models").classes("text-xl font-semibold mb-4")
            try:
                rows = ckan_client.list_models()
                ui.table(
                    columns=[
                        {"name": "name",         "label": "Name",        "field": "name",         "align": "left", "sortable": True},
                        {"name": "docker_image", "label": "Image",       "field": "docker_image", "align": "left"},
                        {"name": "docker_tag",   "label": "Tag",         "field": "docker_tag",   "align": "left", "sortable": True},
                        {"name": "description",  "label": "Description", "field": "description",  "align": "left"},
                    ],
                    rows=rows,
                    row_key="name",
                ).classes("w-full")
            except Exception as e:
                _error_label(f"Could not load models: {e}")

    @ui.page("/ui/model-runs")
    def model_runs():
        _header("/ui/model-runs")
        with ui.column().classes("p-6 w-full"):
            ui.label("Model Runs").classes("text-xl font-semibold mb-4")
            try:
                rows = lakefs_client.list_model_runs()
                ui.table(
                    columns=[
                        {"name": "qid",           "label": "QID",       "field": "qid",           "align": "left", "sortable": True},
                        {"name": "model_name",    "label": "Model",     "field": "model_name",    "align": "left", "sortable": True},
                        {"name": "docker_tag",    "label": "Tag",       "field": "docker_tag",    "align": "left"},
                        {"name": "run_timestamp", "label": "Timestamp", "field": "run_timestamp", "align": "left", "sortable": True},
                        {"name": "doip_url",      "label": "DOIP",      "field": "doip_url",      "align": "left"},
                    ],
                    rows=rows,
                    row_key="qid",
                ).classes("w-full")
            except Exception as e:
                _error_label(f"Could not load model runs: {e}")

    @ui.page("/ui/trigger")
    def trigger():
        _header("/ui/trigger")
        with ui.column().classes("p-6 w-full max-w-2xl"):
            ui.label("Run a forecast model").classes("text-xl font-semibold")
            ui.label(
                "Select a processed dataset and a registered model, adjust the configuration if needed, "
                "and submit a new model run. The run will be executed on the cluster and results will "
                "appear in the Model Runs page once complete."
            ).classes("text-sm text-gray-500 mb-4")

            # Load options
            try:
                dataset_options = {
                    f"{d['name']} ({d['qid']})": d["lakefs_path"]
                    for d in lakefs_client.list_processed_datasets()
                }
            except Exception as e:
                _error_label(f"Could not load datasets: {e}")
                dataset_options = {}

            try:
                model_options = {
                    m["name"]: f"{m['docker_image']}:{m['docker_tag']}"
                    for m in ckan_client.list_models()
                }
            except Exception as e:
                _error_label(f"Could not load models: {e}")
                model_options = {}

            input_select = ui.select(
                label="Input dataset",
                options=list(dataset_options.keys()),
            ).classes("w-full")

            model_select = ui.select(
                label="Model",
                options=list(model_options.keys()),
            ).classes("w-full")

            config_input = ui.textarea(
                label="Config (JSON)",
                value='{"horizon_weeks": 4, "n_reference_weeks": 4}',
            ).classes("w-full font-mono")

            result_label = ui.label("").classes("text-sm text-gray-500")

            def submit():
                if not input_select.value:
                    ui.notify("Select an input dataset", type="warning", position="top")
                    return
                if not model_select.value:
                    ui.notify("Select a model", type="warning", position="top")
                    return
                try:
                    config = json.loads(config_input.value)
                except json.JSONDecodeError as e:
                    ui.notify(f"Invalid JSON: {e}", type="negative", position="top")
                    return

                input_path  = dataset_options[input_select.value]
                full_image  = model_options[model_select.value]
                image, tag  = full_image.rsplit(":", 1) if ":" in full_image else (full_image, "latest")

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

            ui.button("Trigger Run", icon="play_arrow", on_click=submit).classes("bg-blue-700 text-white mt-2")
            result_label
