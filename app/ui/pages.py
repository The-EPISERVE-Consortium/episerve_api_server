import json
from nicegui import ui

from app.clients import lakefs as lakefs_client
from app.clients import ckan as ckan_client


def _header():
    with ui.header().classes("bg-blue-800 text-white items-center gap-4 px-6 py-3"):
        ui.label("EPISERVE Platform").classes("text-xl font-bold")


def _error(msg: str):
    ui.notify(str(msg), type="negative", position="top", timeout=8000)


def _error_label(msg: str):
    ui.label(f"⚠ {msg}").classes("text-red-600 text-sm")


def register_pages():

    @ui.page("/ui")
    def main():
        _header()
        with ui.tabs().classes("w-full") as tabs:
            tab_datasets   = ui.tab("Datasets")
            tab_models     = ui.tab("Models")
            tab_model_runs = ui.tab("Model Runs")

        with ui.tab_panels(tabs, value=tab_datasets).classes("w-full p-4"):

            # ── Datasets ──────────────────────────────────────────────────────
            with ui.tab_panel(tab_datasets):
                with ui.tabs().classes("w-full") as sub_tabs:
                    tab_raw       = ui.tab("Raw")
                    tab_processed = ui.tab("Processed")

                with ui.tab_panels(sub_tabs, value=tab_raw).classes("w-full"):

                    with ui.tab_panel(tab_raw):
                        ui.label("Raw Datasets").classes("text-lg font-semibold mb-2")
                        try:
                            rows = lakefs_client.list_raw_objects()
                            ui.table(
                                columns=[
                                    {"name": "path",          "label": "Path",          "field": "path",          "align": "left", "sortable": True},
                                    {"name": "size_bytes",    "label": "Size (bytes)",  "field": "size_bytes",    "align": "right", "sortable": True},
                                    {"name": "last_modified", "label": "Last Modified", "field": "last_modified", "align": "left",  "sortable": True},
                                ],
                                rows=rows,
                                row_key="path",
                            ).classes("w-full")
                        except Exception as e:
                            _error_label(f"Could not load raw datasets: {e}")

                    with ui.tab_panel(tab_processed):
                        ui.label("Processed Datasets").classes("text-lg font-semibold mb-2")
                        try:
                            rows = lakefs_client.list_processed_datasets()
                            ui.table(
                                columns=[
                                    {"name": "name",        "label": "Name",        "field": "name",        "align": "left",  "sortable": True},
                                    {"name": "qid",         "label": "QID",         "field": "qid",         "align": "left",  "sortable": True},
                                    {"name": "description", "label": "Description", "field": "description", "align": "left"},
                                    {"name": "doip_url",    "label": "DOIP",        "field": "doip_url",    "align": "left"},
                                ],
                                rows=rows,
                                row_key="qid",
                            ).classes("w-full")
                        except Exception as e:
                            _error_label(f"Could not load processed datasets: {e}")

            # ── Models ────────────────────────────────────────────────────────
            with ui.tab_panel(tab_models):
                ui.label("Models").classes("text-lg font-semibold mb-2")
                try:
                    rows = ckan_client.list_models()
                    ui.table(
                        columns=[
                            {"name": "name",         "label": "Name",         "field": "name",         "align": "left", "sortable": True},
                            {"name": "docker_image", "label": "Image",        "field": "docker_image", "align": "left"},
                            {"name": "docker_tag",   "label": "Tag",          "field": "docker_tag",   "align": "left", "sortable": True},
                            {"name": "description",  "label": "Description",  "field": "description",  "align": "left"},
                        ],
                        rows=rows,
                        row_key="name",
                    ).classes("w-full")
                except Exception as e:
                    _error_label(f"Could not load models: {e}")

            # ── Model Runs ────────────────────────────────────────────────────
            with ui.tab_panel(tab_model_runs):
                with ui.row().classes("w-full items-center justify-between mb-2"):
                    ui.label("Model Runs").classes("text-lg font-semibold")
                    ui.button("Trigger Run", icon="play_arrow", on_click=lambda: trigger_dialog.open()).classes("bg-blue-700 text-white")

                runs_table = ui.table(
                    columns=[
                        {"name": "qid",           "label": "QID",        "field": "qid",           "align": "left",  "sortable": True},
                        {"name": "model_name",    "label": "Model",      "field": "model_name",    "align": "left",  "sortable": True},
                        {"name": "docker_tag",    "label": "Tag",        "field": "docker_tag",    "align": "left"},
                        {"name": "run_timestamp", "label": "Timestamp",  "field": "run_timestamp", "align": "left",  "sortable": True},
                        {"name": "doip_url",      "label": "DOIP",       "field": "doip_url",      "align": "left"},
                    ],
                    rows=[],
                    row_key="qid",
                ).classes("w-full")

                def refresh_runs():
                    try:
                        runs_table.rows = lakefs_client.list_model_runs()
                    except Exception as e:
                        _error(f"lakeFS error: {e}")

                refresh_runs()

                # ── Trigger dialog ─────────────────────────────────────────
                with ui.dialog() as trigger_dialog, ui.card().classes("w-[600px]"):
                    ui.label("Trigger Model Run").classes("text-lg font-semibold mb-2")

                    model_image_input = ui.input(
                        label="Model image",
                        placeholder="ghcr.io/the-episerve-consortium/model__prediction__grippeweb__baseline-nullmodel",
                    ).classes("w-full")

                    model_tag_input = ui.input(label="Image tag", value="latest").classes("w-full")

                    input_path_input = ui.input(
                        label="Input path (lakeFS)",
                        placeholder="lakefs://data-processed/main/...",
                    ).classes("w-full")

                    config_input = ui.textarea(
                        label="Config (JSON)",
                        value='{"horizon_weeks": 4, "n_reference_weeks": 4}',
                    ).classes("w-full font-mono")

                    result_label = ui.label("").classes("text-sm text-gray-500")

                    def submit_run():
                        try:
                            config = json.loads(config_input.value)
                        except json.JSONDecodeError as e:
                            _error(f"Invalid JSON: {e}")
                            return
                        from app.clients import prefect as prefect_client
                        try:
                            result = prefect_client.trigger_model_run(
                                input_path=input_path_input.value,
                                model_image=model_image_input.value,
                                model_tag=model_tag_input.value,
                                config_json=json.dumps(config),
                            )
                            result_label.set_text(f"Triggered: {result['prefect_flow_run_id']} ({result['status']})")
                            ui.notify("Model run triggered", type="positive", position="top")
                            trigger_dialog.close()
                            refresh_runs()
                        except Exception as e:
                            _error(f"Prefect error: {e}")

                    with ui.row().classes("w-full justify-end gap-2 mt-2"):
                        ui.button("Cancel", on_click=trigger_dialog.close).props("flat")
                        ui.button("Run", icon="play_arrow", on_click=submit_run).classes("bg-blue-700 text-white")
