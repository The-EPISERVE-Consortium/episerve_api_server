"""Single-page model-run submission and pre-flight review."""

import json
import duckdb
from nicegui import ui
from app.clients import ckan as ckan_client
from app.config import settings
from app.sql_validation import validate_transformation_sql
from app.ui.layout import header
from app.ui.components.messages import error_label
from app.ui.session import executor, require_login


def register_trigger_page():
    """Register the trigger page route."""
    @ui.page("/ui/trigger")
    def trigger():
        """Render dataset, transformation, and model selection with pre-flight review."""
        if not require_login("runs"):
            return
        header("/ui/trigger", api="model-runs", method="POST")

        class _StepCtx:
            """Track the nested UI containers and status label of a trigger step."""

            def __init__(self, row, panel, status_lbl):
                """Keep references to a step panel and its status indicator.

                Args:
                    row: Outer UI row containing the step heading and panel.
                    panel: Content panel belonging to the configuration step.
                    status_lbl: Label displaying the step completion status.
                """
                self._row        = row
                self._panel      = panel
                self.status_lbl  = status_lbl
            def __exit__(self, *a):
                """Finish the step context and attach its status indicator.

                Args:
                    *a: Exception details forwarded to the underlying UI contexts.
                """
                self._panel.__exit__(*a)
                self._row.__exit__(*a)

        def _step_row(n: int, title: str, description: str, is_last: bool = False):
            """Create a numbered configuration step and its completion indicator.

            Args:
                n: One-based step number.
                title: Heading for this configuration step.
                description: Guidance shown beside the step controls.
                is_last: Whether to omit the connector after this step.
            """
            row = ui.row().classes("w-full gap-6 items-stretch")
            row.__enter__()
            with ui.column().classes("w-48 shrink-0 gap-0"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(str(n)).classes(
                        "epi-primary rounded-full w-7 h-7 flex items-center "
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

        with ui.column().classes("epi-page epi-page-spacious"):
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
                error_label(f"Could not load datasets: {e}")
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
                    """Update the selected model and its default configuration.

                    Args:
                        e: UI event containing the selected row or changed value.
                    """
                    if model_table.selected:
                        m = model_table.selected[0]
                        model_status.set_text(f"✓ {m['name']} ({m['docker_tag']})")
                        model_status.classes(remove="text-gray-400", add="text-green-600")
                    else:
                        model_status.set_text("")
                        model_status.classes(remove="text-green-600", add="text-gray-400")

                model_table.on("selection", on_model_selection)
            except Exception as e:
                error_label(f"Could not load models: {e}")
                model_table = None
            ctx2.__exit__(None, None, None)

            def _has_sql(val: str) -> bool:
                """Return whether a transformation contains executable content.

                Args:
                    val: Transformation SQL to inspect.

                Returns:
                    True if at least one line is neither blank nor a SQL comment.
                """
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
                """Bind a transformation verification callback to a dataset editor.

                Args:
                    inp: SQL editor whose current value the callback reads or updates.
                """
                def verify():
                    """Validate the current transformation and display a preview or error."""
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
                """Bind a SQL formatting callback to an editor.

                Args:
                    inp: SQL editor whose current value the callback reads or updates.
                """
                def fmt():
                    """Format the current transformation SQL in its editor."""
                    try:
                        import sqlparse
                        inp.value = sqlparse.format(inp.value, reindent=True, keyword_case='upper')
                    except ImportError:
                        ui.notify("sqlparse not available", type="warning", position="top")
                    except Exception as ex:
                        ui.notify(f"Format error: {ex}", type="negative", position="top")
                return fmt

            def rebuild_sql_inputs(e):
                """Rebuild transformation editors for the selected input datasets.

                Args:
                    e: UI event containing the selected row or changed value.
                """
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
                                ui.button("Verify SQL", icon="check", on_click=_make_verify(inp)).classes("epi-primary text-xs")
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
                """Format the configuration JSON or display its parsing error."""
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
                        confirm_btn = ui.button("Trigger Run", icon="play_arrow").classes("epi-primary").props(f"{'disabled' if not settings.prefect_api_url else ''}")

            def do_submit():
                """Validate the reviewed request and submit it to the model runner."""
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
                        executed_by=executor(),
                    )
                    result_label.set_text(f"Triggered: {result['prefect_flow_run_id']} ({result['status']})")
                    ui.notify("Model run triggered", type="positive", position="top")
                except Exception as e:
                    ui.notify(f"Prefect error: {e}", type="negative", position="top")

            confirm_btn.on("click", do_submit)

            def open_preflight():
                """Validate current selections and open the run review dialog."""
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
                    """Refresh the request preview from the current form values."""
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
                ui.button("Pre-flight Check", icon="checklist", on_click=open_preflight).classes("epi-primary")
                ui.label("Validate all steps before running the model.").classes("text-sm text-gray-500")
            result_label
