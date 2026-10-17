"""The /ui/leaderboard page: stored benchmarks and their ranked entries."""

import logging

from nicegui import run, ui
from starlette.requests import Request

from app.ui import session
from app.benchmarks import actions as bactions
from app.leaderboard import actions
from app.benchmarks.definition import evaluation_sql
from app.benchmarks.store import benchmark_from_row
from app.leaderboard.about import METRIC_DOCS, description_text, overview_facts
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable
from app.leaderboard.present import (
    LABELS, chart_option, filter_entries, history_window, point_rows, summary, table_rows,
)
from app.ui.pages import _error_label, _header

logger = logging.getLogger(__name__)

_LOGIN_EXPIRED = "Your login has expired; please log in again."
_ALL_MODELS = "All models"
# Metric cells show formatted strings; sort on the raw numbers, missing values last when ascending.
_RAW_SORT = (
    "(a, b, rowA, rowB) => { const x = rowA.raw.METRIC, y = rowB.raw.METRIC;"
    " if (x === null || x === undefined) return (y === null || y === undefined) ? 0 : 1;"
    " if (y === null || y === undefined) return -1; return x - y; }"
)


def _is_logged_in() -> bool:
    return session.can("benchmarks")


_TABS = [("overview", "Overview"), ("data", "Data"), ("leaderboard", "Leaderboard")]
_DEFAULT_TAB = "overview"
# earlier tab names open the tab that now holds their content
_FORMER_TABS = {"get-started": "overview", "evaluation": "data", "run": "leaderboard"}
_MODEL_CELL = (
    "<q-td :props='props'>{{ props.value.split('__').join('__\\u200b') }}"
    "<q-badge v-if='props.row.baseline' color='grey-7' class='q-ml-sm'>baseline</q-badge></q-td>"
)


def _run_cell(benchmark_id: int) -> str:
    """Run QID as a link to the run page; the click is stopped so the row's own handler does not open it twice."""
    return (
        "<q-td :props='props'><a :href=\"'/ui/benchmarks/" + str(benchmark_id) + "/runs/' + props.row.run_qid\""
        " target='_blank' @click.stop class='text-blue-700'>{{ props.row.run_qid }}</a></q-td>"
    )


_OPEN_RUN_JS = (
    "(evt, row) => window.open('/ui/benchmarks/BENCHMARK/runs/' + encodeURIComponent(row.run_qid), '_blank')"
)


def _entry_detail(entry, benchmark_id: int, logged_in: bool) -> None:
    """The detail of one scored run: window, pins, parameters, chart and per-date rows (+ Remove when logged in)."""
    ui.label(f"{entry.model_name} — run {entry.run_qid}").classes("text-2xl font-semibold")
    ui.link(
        "Show run in ModelHub",
        f"https://data.episerve.zib.de/dataset/{entry.run_qid.lower()}", new_tab=True,
    ).classes("text-sm")
    ui.label(f"History window: {history_window(entry)}").classes("text-sm text-gray-600")
    ui.label(
        f"Data: lakeFS commit {entry.truth_commit} · CKAN last modified "
        f"{entry.truth_as_of or 'unknown'} · scored {entry.scored_at}"
    ).classes("text-sm text-gray-600")
    if entry.params:
        with ui.column().classes("gap-0"):
            for k, v in entry.params.items():
                ui.label(f"{k}: {v}").classes("text-sm text-gray-600")
    else:
        ui.label("Parameters: —").classes("text-sm text-gray-600")
    ui.echart(chart_option(entry)).classes("w-full h-80")
    ui.table(
        columns=[
            {"name": c, "label": c if c != "error" else "error (forecast − observed)",
             "field": c, "align": "left" if c == "date" else "right"}
            for c in ("date", "observed", "forecast", "q10", "q90", "error")
        ],
        rows=point_rows(entry), row_key="date",
    ).props("dense flat").classes("w-full")
    if logged_in and entry.entry_id is not None:
        def remove(entry_id=entry.entry_id):
            if not _is_logged_in():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return
            try:
                actions.remove_entry(benchmark_id, entry_id)
            except actions.NotFound as e:
                ui.notify(str(e), type="negative")
                return
            except Exception:
                logger.exception("could not remove the entry")
                ui.notify("Could not remove the entry.", type="negative")
                return
            ui.navigate.to(f"/ui/benchmarks/{benchmark_id}")

        ui.button("Remove from leaderboard", on_click=remove).props("flat color=negative")


def _is_admin() -> bool:
    return session.can("admin")


def _edit_description_dialog(row: dict) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-[40rem] max-w-full gap-2"):
        ui.label("Edit description").classes("text-lg font-semibold")
        text = ui.textarea(value=row.get("description") or "").props("outlined autogrow").classes("w-full")
        ui.label("Leave it empty to show a sentence generated from the definition.").classes("text-xs text-gray-500")
        error = ui.label("").classes("text-red-600 text-sm")

        async def save():
            if not _is_admin():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return
            try:
                await run.io_bound(lambda: bactions.set_description(row["id"], text.value))
            except bactions.NotFound as e:
                error.set_text(str(e))
                return
            except Exception:
                logger.exception("could not save the description")
                error.set_text("The description could not be saved.")
                return
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


def _overview_tab(row: dict) -> None:
    with ui.row().classes("items-start gap-2 w-full"):
        ui.label(description_text(row)).classes("text-sm text-gray-600 max-w-3xl whitespace-pre-line")
        if _is_admin():
            ui.button(icon="edit", on_click=lambda: _edit_description_dialog(row)).props(
                "flat round dense size=sm aria-label='Edit description'").classes("text-gray-500")
    rows = [{"caption": caption, "value": value} for caption, value in overview_facts(row)]
    ui.table(
        columns=[{"name": "caption", "label": "", "field": "caption", "align": "left"},
                 {"name": "value", "label": "", "field": "value", "align": "left"}],
        rows=rows, row_key="caption",
    ).props("flat bordered dense hide-header hide-bottom").classes("max-w-xl w-full").add_slot(
        "body-cell-caption", '''<q-td :props="props" class="text-gray-500" style="width: 11rem">{{ props.value }}</q-td>''')


def _data_tab(row: dict, benchmark_id: int) -> None:
    ui.add_css(".nicegui-code pre, .nicegui-code code { white-space: pre-wrap !important; word-break: break-word; }")
    ui.label("Training data").classes("text-xl font-semibold text-gray-900")
    try:
        spec = bactions.run_spec(benchmark_id)
        url = spec["input_data_files"][0][0]
        training = spec["data_transformation_sql"][0]
    except Exception:
        logger.exception("could not build the run spec for the data tab")
        ui.label("The data file link and the training SQL are not available right now.").classes(
            "text-sm text-gray-500")
    else:
        ui.label("The data file at the pinned version:").classes("text-sm text-gray-600")
        ui.link(url, url, new_tab=True).classes("text-sm break-all")
        ui.label("Apply this SQL to the file; the result is what a model sees.").classes("text-sm text-gray-600")
        ui.code(training, language="sql").classes("w-full max-w-4xl")
    ui.label("Evaluation data").classes("text-xl font-semibold text-gray-900 mt-4")
    ui.label("Apply this SQL to the same file; forecasts are scored against the result.").classes(
        "text-sm text-gray-600")
    ui.code(evaluation_sql(benchmark_from_row(row)), language="sql").classes("w-full max-w-4xl")
    ui.label("Evaluation criteria").classes("text-xl font-semibold text-gray-900 mt-4")
    ui.table(
        columns=[{"name": "metric", "label": "Metric", "field": "metric", "align": "left"},
                 {"name": "meaning", "label": "Definition", "field": "meaning", "align": "left",
                  "style": "white-space: normal"},
                 {"name": "better", "label": "Better", "field": "better", "align": "left"}],
        rows=[{"metric": n, "meaning": m, "better": b} for n, m, b in METRIC_DOCS], row_key="metric",
    ).props("dense flat").classes("w-full max-w-5xl")


def register_leaderboard_page() -> None:
    """Register ``/ui/benchmarks/{id}`` (and redirect ``/ui/leaderboard``). Reads the database only; never CKAN, DOIP or lakeFS."""

    @ui.page("/ui/leaderboard")
    def leaderboard_redirect(benchmark: str | None = None):
        try:
            target = f"/ui/benchmarks/{int(benchmark)}"
        except (TypeError, ValueError):
            target = "/ui/benchmarks"
        ui.navigate.to(target)

    @ui.page("/ui/benchmarks/{benchmark_id}/runs/{run_qid}")
    def run_page(benchmark_id: int, run_qid: str):
        """One scored run of a benchmark (opened in its own window from the leaderboard)."""
        _header("/ui/benchmarks", api=f"benchmarks/{benchmark_id}/leaderboard?all_runs=true")
        logged_in = _is_logged_in()
        with ui.column().classes("px-8 py-6 w-full max-w-5xl gap-3"):
            ui.link("← Back to the leaderboard", f"/ui/benchmarks/{benchmark_id}").classes("text-sm")
            try:
                board = actions.read_leaderboard(benchmark_id, "interval_score", False)
            except actions.NotFound:
                _error_label("Benchmark not found.")
                return
            except Exception:
                logger.exception("could not load the run")
                _error_label("Could not load the run.")
                return
            entry = next((x for x in board.entries if x.run_qid == run_qid), None)
            if entry is None:
                _error_label("Run not found.")
                return
            _entry_detail(entry, benchmark_id, logged_in)

    @ui.page("/ui/benchmarks/{benchmark_id}")
    def leaderboard_page(benchmark_id: int, tab: str = _DEFAULT_TAB):
        _header("/ui/benchmarks", api=f"benchmarks/{benchmark_id}/leaderboard")
        logged_in = _is_logged_in()
        selected = benchmark_id
        try:
            row = bactions.get(selected)
        except bactions.NotFound:
            row = None
        except Exception:
            logger.exception("could not load the benchmark")
            with ui.column().classes("px-8 py-6"):
                _error_label("Could not load the benchmark.")
            return
        if row is None:
            with ui.column().classes("px-8 py-6 gap-2"):
                ui.link("← All benchmarks", "/ui/benchmarks").classes("text-sm")
                _error_label("Benchmark not found.")
            return
        state = {"rank_by": "interval_score", "all_runs": False, "model": None, "min_days": 0, "text": ""}
        tab = _FORMER_TABS.get(tab, tab)
        open_tab = tab if tab in dict(_TABS) else _DEFAULT_TAB

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            ui.link("← All benchmarks", "/ui/benchmarks").classes("text-sm")
            ui.label(row["name"]).classes("text-3xl font-bold text-gray-900")
            with ui.tabs(value=open_tab).classes("w-full") as tabs:
                for key, label in _TABS:
                    ui.tab(key, label=label)
            with ui.tab_panels(tabs, value=open_tab).classes("w-full"):
                with ui.tab_panel("overview").classes("gap-3"):
                    _overview_tab(row)
                with ui.tab_panel("data").classes("gap-3"):
                    _data_tab(row, selected)
                with ui.tab_panel("leaderboard").classes("gap-3"):
                    with ui.row().classes("items-end gap-3"):
                        def set_rank(e):
                            state["rank_by"] = e.value
                            board_view.refresh()

                        def set_all(e):
                            state["all_runs"] = bool(e.value)
                            board_view.refresh()

                        ui.select(LABELS, label="Rank by", value=state["rank_by"], on_change=set_rank).props("outlined dense")
                        ui.switch("Show all runs", value=state["all_runs"], on_change=set_all)
                    view = ui.column().classes("w-full gap-3")
                    candidates_box = ui.column().classes("w-full gap-2 mt-6")

        @ui.refreshable
        def board_view():
            try:
                board = actions.read_leaderboard(selected, state["rank_by"], not state["all_runs"])
            except actions.NotFound:
                _error_label("Benchmark not found.")
                return
            except Exception:
                logger.exception("could not load the leaderboard")
                _error_label("Could not load the leaderboard.")
                return
            ui.label(summary(board)).classes("text-sm text-gray-700")
            if not board.entries:
                ui.label("No runs on this leaderboard yet." + (" Add one from the candidates below." if logged_in else " Log in to add runs.")).classes(
                    "text-sm text-gray-500")
                return
            columns = [
                {"name": "rank", "label": "#", "field": "rank", "align": "left", "sortable": True},
                {"name": "model", "label": "Model", "field": "model", "align": "left", "sortable": True,
                 "style": "white-space: normal; max-width: 15rem"},
                {"name": "run", "label": "Run", "field": "run_qid", "align": "left", "sortable": True},
                {"name": "date", "label": "Date", "field": "date", "align": "left", "sortable": True,
                 "style": "white-space: normal; max-width: 6rem"},
                {"name": "params", "label": "Parameters", "field": "params", "align": "left",
                 "style": "white-space: normal; max-width: 14rem"},
                {"name": "history", "label": "History (A→B)", "field": "history", "align": "left", "sortable": True,
                 "style": "white-space: normal; min-width: 8rem"},
                *[{"name": m, "label": LABELS[m], "field": m, "align": "right", "sortable": True,
                   ":sort": _RAW_SORT.replace("METRIC", m)} for m in LABELS],
                {"name": "scored", "label": "Scored", "field": "scored", "align": "right"},
            ]
            models = sorted({e.model_name for e in board.entries})
            flt = {"model": state["model"], "min_days": state["min_days"], "text": state["text"]}

            @ui.refreshable
            def board_table():
                entries = filter_entries(board.entries, flt["model"], flt["min_days"], flt["text"])
                if flt["model"] or flt["min_days"] or flt["text"].strip():
                    ui.label(f"Showing {len(entries)} of {len(board.entries)} entries").classes("text-xs text-gray-500")
                table = ui.table(columns=columns, rows=table_rows(board, entries), row_key="run_qid",
                                 pagination={"rowsPerPage": 25}).classes("w-full")
                table.props(':rows-per-page-options="[10, 25, 50, 0]"')   # bound: Quasar needs an array, not a string
                table.add_slot("body-cell-model", _MODEL_CELL)
                table.add_slot("body-cell-run", _run_cell(selected))
                for m in LABELS:
                    table.add_slot(f"body-cell-{m}", (
                        "<q-td :props='props' :class=\"props.row.best." + m + " ? 'text-green-700 font-bold' : ''\">"
                        "{{ props.value }}</q-td>"
                    ))
                # The click is handled in the browser so window.open runs inside the user's click (no popup
                # blocking); every entry has its own page, which can also be linked or opened in a new tab.
                table.on("rowClick", js_handler=_OPEN_RUN_JS.replace("BENCHMARK", str(selected)))

            def set_model(e):
                flt["model"] = state["model"] = e.value if e.value != _ALL_MODELS else None
                board_table.refresh()

            def set_min_days(e):
                try:
                    flt["min_days"] = state["min_days"] = max(0, int(e.value or 0))
                except (TypeError, ValueError):
                    flt["min_days"] = state["min_days"] = 0
                board_table.refresh()

            def set_text(e):
                flt["text"] = state["text"] = e.value or ""
                board_table.refresh()

            with ui.row().classes("items-end gap-3"):
                ui.input("Search", value=flt["text"], placeholder="model, run or parameter",
                         on_change=set_text).props("outlined dense clearable").classes("min-w-[16rem]")
                ui.select([_ALL_MODELS, *models], label="Model", value=flt["model"] or _ALL_MODELS,
                          on_change=set_model).props("outlined dense").classes("min-w-[14rem]")
                ui.number("Min. history (days)", value=flt["min_days"], min=0, precision=0,
                          on_change=set_min_days).props("outlined dense")
            board_table()

        @ui.refreshable
        async def candidates_view():
            """Finished runs that fit this benchmark but are not on the leaderboard yet, each with an add button.

            Listing them reads the provenance of every run (CKAN, DOIP), so it happens in a worker thread after
            the page is shown, and only for users who may add runs.
            """
            ui.label("Potential candidates for this leaderboard").classes("text-lg font-semibold text-gray-900")
            ui.label("Finished model runs that fit this benchmark (input read at the pinned data version, one "
                     "input) and are not on the leaderboard yet.").classes("text-sm text-gray-600")
            loading = ui.row().classes("items-center gap-2")
            with loading:
                ui.spinner(size="sm")
                ui.label("Loading runs…").classes("text-sm text-gray-600")
            try:
                found = await run.io_bound(actions.candidates, selected)
            except Exception:
                logger.exception("could not list the candidate runs")
                loading.delete()
                _error_label("Could not list the candidate runs.")
                return
            loading.delete()
            rows = [{"qid": c["qid"], "model": c["model_name"], "date": c["run_timestamp"][:10]}
                    for c in found if c["reason"] is None]
            if not rows:
                ui.label("No other matching runs right now.").classes("text-sm text-gray-500")
                return
            candidates = ui.table(
                columns=[{"name": "model", "label": "Model", "field": "model", "align": "left",
                          "style": "white-space: normal; max-width: 20rem"},
                         {"name": "qid", "label": "Run", "field": "qid", "align": "left"},
                         {"name": "date", "label": "Date", "field": "date", "align": "left"},
                         {"name": "add", "label": "", "field": "qid", "align": "right"}],
                rows=rows, row_key="qid",
            ).props("flat bordered dense hide-bottom").classes("max-w-4xl w-full")
            candidates.add_slot("body-cell-model", _MODEL_CELL)
            candidates.add_slot("body-cell-add", (
                "<q-td :props='props' class='text-right'><q-btn dense flat no-caps color='primary' "
                "label='Add to leaderboard' @click=\"() => $parent.$emit('add', props.row)\" /></q-td>"))

            async def add_candidate(e):
                qid = (e.args or {}).get("qid")
                if not _is_logged_in():
                    ui.notify(_LOGIN_EXPIRED, type="warning")
                    return
                if not qid:
                    return
                candidates.props("loading")
                try:
                    await run.io_bound(lambda: actions.add_run(selected, qid))
                except (NotScorable, actions.Conflict, actions.NotFound, LoadError) as err:
                    candidates.props(remove="loading")
                    ui.notify(str(err), type="negative", multi_line=True)
                    return
                except Exception:
                    logger.exception("could not add the run")
                    candidates.props(remove="loading")
                    ui.notify("Could not add the run.", type="negative")
                    return
                ui.notify(f"{qid} added to the leaderboard", type="positive")
                board_view.refresh()
                candidates_view.refresh()

            candidates.on("add", add_candidate)

        with view:
            board_view()
        if logged_in:
            async def load_candidates():
                with candidates_box:
                    await candidates_view()

            ui.timer(0.1, load_candidates, once=True)  # after the page is shown
