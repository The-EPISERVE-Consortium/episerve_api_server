"""The /ui/leaderboard page: stored benchmarks and their ranked entries."""

import logging

from nicegui import run, ui, app as _napp
from starlette.requests import Request

from app.auth import _daily_token
from app.benchmarks import actions as bactions
from app.leaderboard import actions
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable
from app.leaderboard.present import (
    LABELS, chart_option, filter_entries, history_window, point_rows, summary, table_rows,
)
from app.ui.pages import _api_box, _error_label, _header

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
    return _napp.storage.user.get("token") == _daily_token()


async def _add_run_dialog(benchmark_id: int) -> None:
    """Dialog to add a model run to the benchmark; scoring runs when 'Add' is pressed.

    The dialog opens at once; the candidate runs (one provenance fetch each) load in a worker thread.
    """
    holder: dict = {}
    with ui.dialog() as dialog, ui.card().classes("w-[40rem] max-w-full gap-2"):
        ui.label("Add run").classes("text-lg font-semibold")
        error = ui.label("").classes("text-red-600 text-sm")
        loading = ui.row().classes("items-center gap-2")
        with loading:
            ui.spinner(size="sm")
            ui.label("Loading runs…").classes("text-sm text-gray-600")
        body = ui.column().classes("w-full gap-2")
        spinner = ui.spinner(size="md")
        spinner.set_visibility(False)

        async def add():
            if not _is_logged_in():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return
            error.set_text("")
            select = holder.get("select")
            if select is None or not select.value:
                error.set_text("Choose a model run.")
                return
            add_btn.disable()
            spinner.set_visibility(True)
            try:
                await run.io_bound(lambda: actions.add_run(benchmark_id, select.value))
            except (NotScorable, actions.Conflict, actions.NotFound, LoadError) as e:
                error.set_text(str(e))
                return
            except Exception:
                logger.exception("could not add the run")
                error.set_text("Could not add the run.")
                return
            finally:
                spinner.set_visibility(False)
                add_btn.enable()
            dialog.close()
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            add_btn = ui.button("Add", on_click=add)
    dialog.on("hide", dialog.delete)
    dialog.open()
    try:
        candidates = await run.io_bound(actions.candidates, benchmark_id)
    except Exception:
        logger.exception("could not list the candidate runs")
        candidates = []
        error.set_text("Could not list runs.")
    loading.delete()
    addable = {c["qid"]: f"{c['model_name']} · {c['qid']} · {c['run_timestamp'][:10]}"
               for c in candidates if c["reason"] is None}
    blocked = [c for c in candidates if c["reason"] is not None]
    with body:
        holder["select"] = ui.select(addable, label="Model run").props("outlined dense").classes("w-full")
        if blocked:
            with ui.expansion(f"Runs that cannot be added ({len(blocked)})").classes("w-full"):
                for c in blocked:
                    ui.label(f"{c['model_name']} ({c['qid']}): {c['reason']}").classes("text-sm text-gray-600")


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


def _definition_text(row: dict) -> list[tuple[str, str]]:
    """Caption/value pairs describing what a benchmark scores."""
    filter_text = f"{row['filter_column']} = {row['filter_value']}" if row.get("filter_column") else "none"
    return [
        ("Dataset", row["dataset_qid"]),
        ("Target", row["target_column"]),
        ("Filter", filter_text),
        ("Test window", f"{row['test_start']} → {row['test_end']}"),
        ("Forecast length", f"{row['prediction_length']} steps" if row.get("prediction_length") else "—"),
        ("Data commit", (row.get("data_commit") or "")[:12] or "—"),
    ]


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
        _header("/ui/benchmarks")
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
    def leaderboard_page(request: Request, benchmark_id: int):
        _header("/ui/benchmarks")
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
        state = {"rank_by": "interval_score", "all_runs": False, "model": None, "min_days": 0}

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            ui.link("← All benchmarks", "/ui/benchmarks").classes("text-sm")
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label(row["name"]).classes("text-3xl font-bold text-gray-900")
                    ui.label(
                        "Which model predicted this series best over the fixed test window? Scores are "
                        "computed once, when a run is added, and stored with the data version they used."
                    ).classes("text-sm text-gray-500 mt-1")
                _api_box(f"{request.base_url}benchmarks/{selected}/leaderboard")
            with ui.row().classes("gap-x-6 gap-y-0"):
                for caption, value in _definition_text(row):
                    ui.label(f"{caption}: {value}").classes("text-sm text-gray-700")
            with ui.row().classes("items-center gap-3"):
                ui.button("Start a run from this benchmark",
                          on_click=lambda: ui.navigate.to(f"/ui/run_workflow/1?benchmark={selected}")
                          ).props("no-caps outline")
                if logged_in:
                    ui.button("Add run", on_click=lambda: _add_run_dialog(selected))

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
                ui.label("No runs on this leaderboard yet." + ("" if logged_in else " Log in to add runs.")).classes(
                    "text-sm text-gray-500")
                return
            columns = [
                {"name": "rank", "label": "#", "field": "rank", "align": "left", "sortable": True},
                {"name": "model", "label": "Model", "field": "model", "align": "left", "sortable": True},
                {"name": "params", "label": "Parameters", "field": "params", "align": "left"},
                {"name": "history", "label": "History (A→B)", "field": "history", "align": "left", "sortable": True},
                *[{"name": m, "label": LABELS[m], "field": m, "align": "right", "sortable": True,
                   ":sort": _RAW_SORT.replace("METRIC", m)} for m in LABELS],
                {"name": "scored", "label": "Scored", "field": "scored", "align": "right"},
            ]
            models = sorted({e.model_name for e in board.entries})
            flt = {"model": state["model"], "min_days": state["min_days"]}

            @ui.refreshable
            def board_table():
                entries = filter_entries(board.entries, flt["model"], flt["min_days"])
                if flt["model"] or flt["min_days"]:
                    ui.label(f"Showing {len(entries)} of {len(board.entries)} entries").classes("text-xs text-gray-500")
                table = ui.table(columns=columns, rows=table_rows(board, entries), row_key="run_qid").classes("w-full")
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

            with ui.row().classes("items-end gap-3"):
                ui.select([_ALL_MODELS, *models], label="Model", value=flt["model"] or _ALL_MODELS,
                          on_change=set_model).props("outlined dense").classes("min-w-[14rem]")
                ui.number("Min. history (days)", value=flt["min_days"], min=0, precision=0,
                          on_change=set_min_days).props("outlined dense")
            board_table()

        with view:
            board_view()
