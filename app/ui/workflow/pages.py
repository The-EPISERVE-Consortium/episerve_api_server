"""The five routes of the model-run workflow."""

import logging
import json
import duckdb
from nicegui import ui, app as _napp
from app.benchmarks import actions as bactions
from app.benchmarks.errors import NotFound as BenchmarkNotFound
from app.benchmarks.workflow import apply_benchmark, clear_benchmark, overlay_config, workflow_problems
from app.clients import ckan as ckan_client
from app.config import settings
from app.sql_validation import validate_transformation_sql
from app.ui.layout import header
from app.ui.session import executor, require_login
from app.ui.workflow.state import workflow_state
from app.ui.workflow.components import stepper, summary_panel, workflow_footer

logger = logging.getLogger(__name__)


def register_workflow_pages():
    """Register the model-run workflow routes."""
    @ui.page("/ui/run_workflow")
    def _tv2_redirect():
        """Redirect authorized users to the first workflow step."""
        if not require_login("runs"):
            return
        ui.navigate.to("/ui/run_workflow/1")


    @ui.page("/ui/run_workflow/1")
    def trigger_v2_step1(benchmark: int | None = None):
        """Render dataset selection and optional benchmark initialization.

        Args:
            benchmark: Optional benchmark identifier used to initialize the workflow.
        """
        if not require_login("runs"):
            return
        state = workflow_state()
        try:
            all_ds = ckan_client.list_processed_datasets()
        except Exception:
            all_ds = []

        selected_qids: set = set(d["qid"] for d in state.get("datasets", []))
        visible: list = list(all_ds)

        def toggle(qid: str):
            """Toggle a dataset selection and refresh the step controls.

            Args:
                qid: Identifier of the dataset to select or deselect.
            """
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
            """Clear dataset selections and remove any attached benchmark."""
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
            """Apply the selected benchmark to the workflow and refresh controls."""
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
            """Filter available choices using the current search text.

            Args:
                v: Current search text.
            """
            vl = v.lower()
            visible.clear()
            visible.extend(
                r for r in all_ds
                if not vl or vl in r["name"].lower() or vl in r.get("description", "").lower()
            )
            dataset_cards.refresh()

        def on_continue():
            """Persist the step selections and navigate to the next workflow step."""
            if not selected_qids:
                ui.notify("Select at least one dataset", type="warning", position="top")
                return
            ui.navigate.to("/ui/run_workflow/2")

        @ui.refreshable
        def dataset_cards():
            """Render the filtered dataset choices and selection controls."""
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
            """Render the selected dataset count and continue action."""
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
            """Refresh the summary panel with the current step selections."""
            summary_panel(state, hint="" if selected_qids else "Select datasets to continue.")

        header("/ui/run_workflow/1", api="model-runs", method="POST")
        with ui.column().classes("epi-page-padding w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("epi-page-title")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            stepper(1)
            if finals:
                with ui.element("div").classes("w-full border border-gray-200 rounded-xl p-5 bg-white"):
                    ui.label("Start from a benchmark").classes("text-lg font-bold text-gray-900")
                    ui.label(
                        "Prefills the dataset at the benchmark's data commit, the transformation and the horizon."
                    ).classes("epi-page-description")
                    with ui.row().classes("w-full items-center gap-3 mt-2"):
                        ui.select(
                            {b["id"]: b["name"] for b in finals}, label="Start from a benchmark",
                            value=chosen_benchmark["id"],
                            on_change=lambda e: chosen_benchmark.update(id=e.value),
                        ).classes("w-80").props("dense outlined")
                        ui.button("Use benchmark", on_click=use_benchmark).props("no-caps").classes(
                            "epi-primary rounded-lg")
            with ui.row().classes("epi-workflow-layout"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.row().classes("w-full items-start justify-between mb-4"):
                        with ui.column().classes("gap-0"):
                            ui.label("1. Select Input Datasets").classes("text-lg font-bold text-gray-900")
                            ui.label("Choose one or more datasets to use as input.").classes("epi-page-description")
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
                        ).classes("epi-primary px-8 rounded-lg").props("no-caps")
        workflow_footer()


    @ui.page("/ui/run_workflow/2")
    def trigger_v2_step2():
        """Render transformation SQL and output filenames for selected datasets."""
        if not require_login("runs"):
            return
        state = workflow_state()
        if not state.get("datasets"):
            ui.navigate.to("/ui/run_workflow/1")
            return

        datasets = state.get("datasets", [])
        sql_refs: dict = {}

        def make_verify(inp):
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

        def make_format_sql(inp):
            """Bind a SQL formatting callback to an editor.

            Args:
                inp: SQL editor whose current value the callback reads or updates.
            """
            def fmt():
                """Format the current transformation SQL in its editor."""
                try:
                    import sqlparse
                    inp.value = sqlparse.format(inp.value, reindent=True, keyword_case="upper")
                except ImportError:
                    ui.notify("sqlparse not available", type="warning", position="top")
                except Exception as ex:
                    ui.notify(f"Format error: {ex}", type="negative", position="top")
            return fmt

        def on_continue():
            """Persist the step selections and navigate to the next workflow step."""
            state["sql"] = {qid: inp.value for qid, inp in sql_refs.items()}
            _napp.storage.user["tv2"] = state
            ui.navigate.to("/ui/run_workflow/3")

        header("/ui/run_workflow/2", api="model-runs", method="POST")
        with ui.column().classes("epi-page-padding w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("epi-page-title")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            stepper(2)
            with ui.row().classes("epi-workflow-layout"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.column().classes("gap-0 mb-4"):
                        ui.label("2. Dataset Transformations").classes("text-lg font-bold text-gray-900")
                        ui.label(
                            "Optionally define a SQL transformation for each selected dataset. "
                            "Leave the default comment to skip."
                        ).classes("epi-page-description")
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
                                ).classes("epi-primary text-xs").props("no-caps dense")
                with ui.column().classes("shrink-0 gap-3"):
                    summary_panel(state)
                    with ui.row().classes("w-full justify-between items-center"):
                        ui.button("Back to Datasets", icon="arrow_back", on_click=lambda: ui.navigate.to("/ui/run_workflow/1")).classes("!bg-green-600 text-white px-8 rounded-lg").props("no-caps")
                        ui.button(
                            "Continue to Model", icon="arrow_forward", on_click=on_continue,
                        ).classes("epi-primary px-8 rounded-lg").props("no-caps")
        workflow_footer()


    @ui.page("/ui/run_workflow/3")
    def trigger_v2_step3():
        """Render model selection for the current workflow."""
        if not require_login("runs"):
            return
        state = workflow_state()
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
            """Select a model and refresh its card and workflow summary.

            Args:
                name: Name of the model to select.
            """
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
            """Filter available choices using the current search text.

            Args:
                v: Current search text.
            """
            vl = v.lower()
            visible_models.clear()
            visible_models.extend(
                m for m in all_models
                if not vl or vl in m["name"].lower() or vl in m.get("description", "").lower()
            )
            model_cards.refresh()

        def on_continue():
            """Persist the step selections and navigate to the next workflow step."""
            if not sel_name[0]:
                ui.notify("Select a model", type="warning", position="top")
                return
            ui.navigate.to("/ui/run_workflow/4")

        @ui.refreshable
        def model_cards():
            """Render the filtered model choices and selection controls."""
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
            """Refresh the summary panel with the current step selections."""
            summary_panel(state, hint="" if sel_name[0] else "Select a model to continue.")

        header("/ui/run_workflow/3", api="model-runs", method="POST")
        with ui.column().classes("epi-page-padding w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("epi-page-title")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            stepper(3)
            with ui.row().classes("epi-workflow-layout"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.row().classes("w-full items-start justify-between mb-4"):
                        with ui.column().classes("gap-0"):
                            ui.label("3. Select Model").classes("text-lg font-bold text-gray-900")
                            ui.label("Choose the forecasting model to run.").classes("epi-page-description")
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
                        ).classes("epi-primary px-8 rounded-lg").props("no-caps")
        workflow_footer()


    @ui.page("/ui/run_workflow/4")
    def trigger_v2_step4():
        """Render model parameter editing and default configuration."""
        if not require_login("runs"):
            return
        state = workflow_state()
        if not state.get("datasets"):
            ui.navigate.to("/ui/run_workflow/1")
            return
        if not state.get("model"):
            ui.navigate.to("/ui/run_workflow/3")
            return

        def format_json():
            """Format the configuration JSON or display its parsing error."""
            try:
                config_inp.value = json.dumps(json.loads(config_inp.value), indent=2)
            except json.JSONDecodeError as e:
                ui.notify(f"Invalid JSON: {e}", type="negative", position="top")

        def on_continue():
            """Persist the step selections and navigate to the next workflow step."""
            state["config"] = config_inp.value
            _napp.storage.user["tv2"] = state
            ui.navigate.to("/ui/run_workflow/5")

        header("/ui/run_workflow/4", api="model-runs", method="POST")
        with ui.column().classes("epi-page-padding w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("epi-page-title")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            stepper(4)
            with ui.row().classes("epi-workflow-layout"):
                with ui.element("div").classes("flex-1 border border-gray-200 rounded-xl p-5 bg-white"):
                    with ui.row().classes("w-full items-start justify-between mb-3"):
                        with ui.column().classes("gap-0"):
                            ui.label("4. Model Configuration (JSON)").classes("text-lg font-bold text-gray-900")
                            ui.label("Provide configuration parameters for the model.").classes("epi-page-description")
                        with ui.row().classes("gap-1 shrink-0"):
                            ui.button(icon="close", on_click=lambda: config_inp.set_value("")).props("flat round dense").classes("text-gray-400")
                            ui.button("Format", icon="auto_fix_high", on_click=format_json).props("flat dense no-caps").classes("text-xs text-gray-500")
                    if state.get("config") is None:
                        from app.model_config import config_template
                        template = config_template((state["model"] or {}).get("parameters"))
                        state["config"] = json.dumps(
                            overlay_config(template, state.get("benchmark"), set(template)), indent=2
                        ) if template else "{}"
                    config_inp = ui.codemirror(
                        value=state["config"],
                        language="json",
                    ).classes("w-full font-mono")
                with ui.column().classes("shrink-0 gap-3"):
                    summary_panel(state)
                    with ui.row().classes("w-full justify-between items-center"):
                        ui.button("Back to Model", icon="arrow_back", on_click=lambda: ui.navigate.to("/ui/run_workflow/3")).classes("!bg-green-600 text-white px-8 rounded-lg").props("no-caps")
                        ui.button(
                            "Continue to Review", icon="arrow_forward", on_click=on_continue,
                        ).classes("epi-primary px-8 rounded-lg").props("no-caps")
        workflow_footer()


    @ui.page("/ui/run_workflow/5")
    def trigger_v2_step5():
        """Render pre-flight validation and submit the reviewed model run."""
        if not require_login("runs"):
            return
        state = workflow_state()
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
            """Return whether a transformation contains executable content.

            Args:
                val: Transformation SQL to inspect.

            Returns:
                True if at least one line is neither blank nor a SQL comment.
            """
            return any(line.strip() and not line.strip().startswith("--") for line in val.splitlines())

        def build_payload() -> dict:
            """Build the Prefect request from the current workflow selections.

            Returns:
                The model-run request containing the current inputs, transformations, and parameters.
            """
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
            """Refresh the request preview from the current form values."""
            payload_lbl.set_text(json.dumps(build_payload(), indent=2))

        def do_submit():
            """Validate the reviewed request and submit it to the model runner."""
            if not require_login("runs"):
                return
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
            problems = config_problems(config, model["parameters"]) if model.get("parameters") else []
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
                    executed_by=executor(),
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

        header("/ui/run_workflow/5", api="model-runs", method="POST")
        with ui.column().classes("epi-page-padding w-full gap-5"):
            with ui.column().classes("gap-1"):
                ui.label("Run a Forecast Model").classes("epi-page-title")
                ui.label("Configure and run a forecast model in a few simple steps.").classes("text-sm text-gray-500")
            stepper(5)
            with ui.row().classes("w-full justify-between items-center"):
                ui.button("Back to Configuration", icon="arrow_back", on_click=lambda: ui.navigate.to("/ui/run_workflow/4")).classes("!bg-green-600 text-white px-8 rounded-lg").props("no-caps")
                ui.button(
                    "Trigger Run", icon="play_arrow", on_click=do_submit,
                ).classes("epi-primary px-8 rounded-lg").props(
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
                        state.get("sql", {}).get(bench["dataset_qid"], ""),
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
                                with ui.row().classes("epi-workflow-layout"):
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

        workflow_footer()

        update_payload()
