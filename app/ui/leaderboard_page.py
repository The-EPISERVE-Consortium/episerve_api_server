"""The /ui/leaderboard page: stored benchmarks and their ranked entries."""

from nicegui import ui, app as _napp
from starlette.requests import Request

from app.auth import _daily_token
from app.leaderboard import actions, store
from app.leaderboard.present import (
    LABELS, chart_option, filter_entries, history_window, point_rows, summary, table_rows,
)
from app.ui.pages import _api_box, _error_label, _header

_ALL_MODELS = "All models"
# Metric cells show formatted strings; sort on the raw numbers, missing values last when ascending.
_RAW_SORT = (
    "(a, b, rowA, rowB) => { const x = rowA.raw.METRIC, y = rowB.raw.METRIC;"
    " if (x === null || x === undefined) return (y === null || y === undefined) ? 0 : 1;"
    " if (y === null || y === undefined) return -1; return x - y; }"
)


def _is_logged_in() -> bool:
    return _napp.storage.user.get("token") == _daily_token()


def register_leaderboard_page() -> None:
    """Register ``/ui/leaderboard``. Reads the database only; never CKAN, DOIP or lakeFS."""

    @ui.page("/ui/leaderboard")
    def leaderboard_page(request: Request, benchmark: int | None = None):
        _header("/ui/leaderboard")
        logged_in = _is_logged_in()
        try:
            boards = store.list_benchmarks()
        except Exception as e:
            with ui.column().classes("px-8 py-6"):
                _error_label(f"Could not load benchmarks: {e}")
            return
        ids = [b["id"] for b in boards]
        selected = benchmark if benchmark in ids else (ids[0] if ids else None)
        state = {"rank_by": "interval_score", "all_runs": False, "model": None, "min_days": 0}

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Leaderboard").classes("text-3xl font-bold text-gray-900")
                    ui.label(
                        "Which model predicted a chosen series best over a fixed test window? Scores are "
                        "computed once, when a run is added, and stored with the data version they used."
                    ).classes("text-sm text-gray-500 mt-1")
                _api_box(f"{request.base_url}benchmarks" + (f"/{selected}/leaderboard" if selected else ""))
            if not boards:
                ui.label("No benchmarks yet." + ("" if logged_in else " Log in to create one.")).classes(
                    "text-sm text-gray-500")
                return

            with ui.row().classes("items-end gap-3"):
                ui.select(
                    {b["id"]: f"{b['name']} ({b['entry_count']} entries)" for b in boards},
                    value=selected, label="Benchmark",
                    on_change=lambda e: ui.navigate.to(f"/ui/leaderboard?benchmark={e.value}"),
                ).props("outlined dense").classes("min-w-[22rem]")

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
            except Exception as e:
                _error_label(f"Could not load the leaderboard: {e}")
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

                def open_detail(e):
                    try:
                        qid = e.args[1]["run_qid"]
                        entry = next((x for x in board.entries if x.run_qid == qid), None)
                    except (IndexError, KeyError, TypeError):
                        entry = None
                    if entry is None:
                        ui.notify("Run not found", type="warning")
                        return
                    with ui.dialog() as dialog, ui.card().classes("w-[60rem] max-w-full"):
                        ui.label(f"{entry.model_name} — run {entry.run_qid}").classes("text-lg font-semibold")
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
                        ui.button("Close", on_click=dialog.close).props("flat")
                    dialog.on("hide", dialog.delete)
                    dialog.open()

                table.on("rowClick", open_detail)

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
