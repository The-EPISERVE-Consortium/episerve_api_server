"""The /ui/leaderboard page: stored benchmarks and their ranked entries."""

import html
import logging
from urllib.parse import quote

from nicegui import run, ui

from app.ui import benchmark_details, run_details, session
from app.benchmarks import actions as bactions
from app.leaderboard import actions
from app.benchmarks.definition import materialized_from_row
from app.leaderboard.about import COLUMN_HELP, METRIC_DOCS
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable
from app.leaderboard.present import (
    LABELS, columns_text, comparison_rows, filter_entries, history_text, model_label,
    params_text, summary, table_rows,
)
from app.ui.components.messages import error_label
from app.ui.layout import header
from app.ui.theme import BENCHMARK_DETAIL_CSS

logger = logging.getLogger(__name__)

_LOGIN_EXPIRED = "Your login has expired; please log in again."
_ALL_MODELS = "All models"
_DEFAULT_RANK_BY = "mae"
_TOP_N = 3  # runs shown while "Show all runs" is off
_PAGE_SIZES = (10, 25, 50, 0)  # Quasar uses zero for an unpaginated table
_BEST_LABELS = {"mae": "Lowest MAE", "rmse": "Lowest RMSE", "coverage": "Closest to 80%", "interval_score": "Lowest interval score"}
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


def _run_cells(table: ui.table, benchmark_id: int) -> None:
    """Show model and run together and provide a direct link to each run.

    Args:
        table: Ranked runs table receiving the custom cells.
        benchmark_id: Benchmark that owns the run detail pages.
    """
    link = (f" :href=\"'/ui/benchmarks/{benchmark_id}/runs/' + encodeURIComponent(props.row.run_qid)\""
            " target='_blank' rel='noopener' @click.stop")
    table.add_slot("body-cell-model", (
        "<q-td :props='props' class='epi-leaderboard-model'>"
        "<div :title='props.row.model' class='font-semibold'>{{ props.row.model_label }}"
        "<q-badge v-if='props.row.baseline' color='grey-7' class='q-ml-sm'>baseline</q-badge></div>"
        f"<a{link}>{{{{ props.row.run_qid }}}}</a></q-td>"
    ))
    table.add_slot("body-cell-details", (
        f"<q-td :props='props'><a{link} :aria-label=\"'View run ' + props.row.run_qid\">View run ↗</a></q-td>"
    ))
    table.add_slot("body-cell-used", (
        "<q-td :props='props' :title='props.row.input_names'>{{ props.row.input_label }}</q-td>"
    ))


def _help_headers(table, columns: list[dict]) -> None:
    """Add metric guidance and tooltips to sortable column headers.

    Args:
        table: Table receiving the header slots.
        columns: Columns that may have definitions in COLUMN_HELP.
    """
    for column in columns:
        text = COLUMN_HELP.get(column["name"])
        if text:
            target = "<div class='epi-leaderboard-metric-note'>Target: 80%</div>" if column["name"] == "coverage" else ""
            table.add_slot(f"header-cell-{column['name']}", (
                f"<q-th :props='props'>{{{{ props.col.label }}}}{target}"
                f"<q-tooltip max-width='22rem' anchor='bottom middle' self='top middle'>{html.escape(text)}</q-tooltip>"
                "</q-th>"))


_OPEN_RUN_JS = (
    "(evt, row) => window.open('/ui/benchmarks/BENCHMARK/runs/' + encodeURIComponent(row.run_qid), '_blank', 'noopener')"
)


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


def _criteria_section() -> None:
    """Keep the metric definitions available in a collapsed disclosure."""
    with ui.expansion("Evaluation criteria", caption="Metric definitions and scoring rules").props(
        "switch-toggle-side expand-icon=chevron_right expanded-icon=expand_more"
    ).classes("epi-benchmark-disclosure epi-leaderboard-criteria"):
        ui.table(
            columns=[{"name": "metric", "label": "Metric", "field": "metric", "align": "left"},
                     {"name": "meaning", "label": "Definition", "field": "meaning", "align": "left",
                      "style": "white-space: normal; min-width: 12rem"},
                     {"name": "better", "label": "Better", "field": "better", "align": "left"}],
            rows=[{"metric": n, "meaning": m, "better": b} for n, m, b in METRIC_DOCS], row_key="metric",
        ).props("dense flat").classes("epi-benchmark-table w-full")


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
    def run_page(benchmark_id: int, run_qid: str, tab: str = "results"):
        """Render one stored run and its requested detail tab.

        Args:
            benchmark_id: Benchmark owning the scored run.
            run_qid: Identifier of the run to display.
            tab: Initial Results, Forecast data, or Run details tab.
        """
        header("/ui/benchmarks", api=f"benchmarks/{benchmark_id}/leaderboard?all_runs=true")
        ui.add_css(BENCHMARK_DETAIL_CSS)
        with ui.column().classes("epi-page epi-benchmark-page epi-run-page"):
            ui.link("← Back to the leaderboard", f"/ui/benchmarks/{benchmark_id}?tab=leaderboard").classes("text-sm")
            try:
                board = actions.read_leaderboard(benchmark_id, "interval_score", False)
            except actions.NotFound:
                error_label("Benchmark not found.")
                return
            except Exception:
                logger.exception("could not load the run")
                error_label("Could not load the run.")
                return
            entry = next((x for x in board.entries if x.run_qid == run_qid), None)
            if entry is None:
                error_label("Run not found.")
                return
            run_details.render(entry, board, tab=tab)

    @ui.page("/ui/benchmarks/{benchmark_id}")
    def leaderboard_page(benchmark_id: int, tab: str = _DEFAULT_TAB):
        """Render a benchmark and open its requested detail tab.

        Args:
            benchmark_id: Identifier of the benchmark to display.
            tab: Initial tab key, including supported legacy aliases.
        """
        header("/ui/benchmarks", api=f"benchmarks/{benchmark_id}/leaderboard?rank_by={_DEFAULT_RANK_BY}&all_runs=true")
        logged_in = _is_logged_in()
        selected = benchmark_id
        try:
            row = bactions.get(selected)
        except bactions.NotFound:
            row = None
        except Exception:
            logger.exception("could not load the benchmark")
            with ui.column().classes("epi-page-padding"):
                error_label("Could not load the benchmark.")
            return
        if row is None:
            with ui.column().classes("epi-page-padding gap-2"):
                ui.link("← All benchmarks", "/ui/benchmarks").classes("text-sm")
                error_label("Benchmark not found.")
            return
        state = {"rank_by": _DEFAULT_RANK_BY, "all_runs": False, "rows_per_page": 25,
                 "model": None, "min_days": 0, "text": ""}
        tab = _FORMER_TABS.get(tab, tab)
        open_tab = tab if tab in dict(_TABS) else _DEFAULT_TAB

        manifest = materialized_from_row(row)
        ui.add_css(BENCHMARK_DETAIL_CSS)
        with ui.column().classes("epi-page epi-benchmark-page"):
            with ui.row().classes("epi-benchmark-toolbar"):
                ui.link("← All benchmarks", "/ui/benchmarks")
            ui.label(row["name"]).classes("epi-page-title epi-benchmark-title").props("role=heading aria-level=1")
            with ui.tabs(value=open_tab).props("align=left no-caps").classes("epi-benchmark-tabs") as tabs:
                for key, label in _TABS:
                    ui.tab(key, label=label)
            with ui.tab_panels(tabs, value=open_tab).classes("epi-benchmark-panels"):
                with ui.tab_panel("overview").classes("epi-benchmark-panel"):
                    benchmark_details.overview(row, can_edit=_is_admin(), on_edit=lambda: _edit_description_dialog(row))
                with ui.tab_panel("data").classes("epi-benchmark-panel"):
                    if manifest:
                        benchmark_details.generated_data(row, manifest)
                    else:
                        ui.label("No generated files are recorded for this benchmark.").classes("epi-benchmark-body")
                with ui.tab_panel("leaderboard").classes("epi-benchmark-panel epi-leaderboard-panel"):
                    view = ui.column().classes("epi-leaderboard-view")
                    candidates_box = ui.column().classes("w-full gap-2")
                    _criteria_section()

        @ui.refreshable
        def board_view():
            """Render the ranking summary, filters, and comparison table."""
            try:
                board = actions.read_leaderboard(selected, state["rank_by"], False)
            except actions.NotFound:
                error_label("Benchmark not found.")
                return
            except Exception:
                logger.exception("could not load the leaderboard")
                error_label("Could not load the leaderboard.")
                return

            metric = board.rank_by
            with ui.row().classes("epi-leaderboard-heading"):
                with ui.column().classes("gap-0"):
                    ui.label("Ranked runs").classes("epi-benchmark-heading").props("role=heading aria-level=2")
                    ui.label(summary(board)).classes("epi-benchmark-body")
                if board.entries and board.entries[0].metrics.get(metric) is not None:
                    leader = table_rows(board, board.entries[:1])[0]
                    with ui.row().classes("epi-leaderboard-best"):
                        ui.label(_BEST_LABELS[metric]).classes("font-semibold")
                        ui.label(leader[metric]).classes("epi-leaderboard-best-value")
                        qid = leader["run_qid"]
                        ui.link(f"{qid} ↗", f"/ui/benchmarks/{selected}/runs/{quote(qid, safe='')}", new_tab=True)

            columns = [
                {"name": "rank", "label": "#", "field": "rank", "align": "left", "sortable": True},
                {"name": "model", "label": "Model / run", "field": "model", "align": "left", "sortable": True},
                {"name": "used", "label": "Input columns", "field": "input_label", "align": "left"},
                *[
                    {"name": m, "label": ("Interval score" if m == "interval_score" else LABELS[m])
                     + (" ↓" if m == metric and m != "coverage" else ""),
                     "field": m, "align": "right", "sortable": True, ":sort": _RAW_SORT.replace("METRIC", m),
                     "headerClasses": "epi-leaderboard-ranked" if m == metric else ""}
                    for m in LABELS
                ],
                {"name": "scored", "label": "Scored", "field": "scored", "align": "right"},
                {"name": "details", "label": "Details", "field": "run_qid", "align": "left"},
            ]
            models = sorted({entry.model_name for entry in board.entries})
            model_options = {_ALL_MODELS: _ALL_MODELS, **{name: model_label(name) for name in models}}

            def save_page_size(e):
                """Remember the page size when the table is rebuilt.

                Args:
                    e: Table pagination change carrying the selected page size.
                """
                size = e.value.get("rowsPerPage")
                if size in _PAGE_SIZES:
                    state["rows_per_page"] = size

            @ui.refreshable
            def board_table():
                """Filter the ranked runs and render the visible comparison rows."""
                if not board.entries:
                    ui.label("No runs on this leaderboard yet." + (
                        " Add one from the candidates below." if logged_in else " Log in to add runs."
                    )).classes("epi-benchmark-body")
                    return
                entries = filter_entries(board.entries, state["model"], state["min_days"], state["text"])
                if not entries:
                    ui.label("No runs match these filters.").classes("epi-benchmark-body")
                    return
                if state["model"] or state["min_days"] or state["text"].strip():
                    ui.label(f"Showing {len(entries)} of {len(board.entries)} entries").classes("epi-benchmark-note")
                if not state["all_runs"] and len(entries) > _TOP_N:
                    ui.label(f'The top {_TOP_N} of {len(entries)} runs. Turn on "Show all runs" to browse all pages.'
                             ).classes("epi-benchmark-note")
                    entries = entries[:_TOP_N]
                with ui.column().classes("w-full min-w-0 gap-1"):
                    ui.label("Scroll to compare scores").classes("epi-benchmark-note epi-leaderboard-scroll-hint")
                    table = ui.table(columns=columns, rows=comparison_rows(board, entries), row_key="run_qid",
                                     pagination={"page": 1, "rowsPerPage": state["rows_per_page"]},
                                     on_pagination_change=save_page_size).classes(
                        "epi-benchmark-table epi-leaderboard-table w-full"
                    ).props(f'flat bordered :rows-per-page-options="{list(_PAGE_SIZES)}"')
                _help_headers(table, columns)
                _run_cells(table, selected)
                table.add_slot(f"body-cell-{metric}", (
                    "<q-td :props='props' class='epi-leaderboard-ranked' "
                    f''':class="props.row.best.{metric} ? 'epi-leaderboard-metric-best' : ''">'''
                    "{{ props.value }}</q-td>"
                ))
                table.on("rowClick", js_handler=_OPEN_RUN_JS.replace("BENCHMARK", str(selected)))

            def set_rank(e):
                """Reload the runs using the selected ranking metric.

                Args:
                    e: Value change carrying the metric key.
                """
                state["rank_by"] = e.value
                board_view.refresh()

            def set_all(e):
                """Toggle the limit on the number of ranked runs shown.

                Args:
                    e: Value change carrying the switch state.
                """
                state["all_runs"] = bool(e.value)
                board_table.refresh()

            def set_model(e):
                """Apply the model filter using the full catalog identifier.

                Args:
                    e: Value change carrying the model identifier.
                """
                state["model"] = e.value if e.value != _ALL_MODELS else None
                board_table.refresh()

            def set_min_days(e):
                """Filter by the minimum number of calendar days of input history.

                Args:
                    e: Value change carrying the requested minimum.
                """
                try:
                    state["min_days"] = max(0, int(e.value or 0))
                except (TypeError, ValueError):
                    state["min_days"] = 0
                board_table.refresh()

            def set_text(e):
                """Search the model names, run identifiers, and parameters.

                Args:
                    e: Value change carrying the search query.
                """
                state["text"] = e.value or ""
                board_table.refresh()

            with ui.row().classes("epi-leaderboard-filters"):
                ui.select(LABELS, label="Rank by", value=state["rank_by"], on_change=set_rank).props("outlined dense")
                search = ui.input(value=state["text"], placeholder="Search runs", on_change=set_text).props(
                    "outlined dense clearable aria-label='Search runs'"
                )
                with search.add_slot("prepend"):
                    ui.icon("search")
                ui.select(model_options, label="Model", value=state["model"] or _ALL_MODELS,
                          on_change=set_model).props("outlined dense")
                ui.number("Min. history (days)", value=state["min_days"], min=0, precision=0,
                          on_change=set_min_days).props("outlined dense")
                ui.switch("Show all runs", value=state["all_runs"], on_change=set_all)
            board_table()

        @ui.refreshable
        async def candidates_view():
            """Finished runs that fit this benchmark but are not on the leaderboard yet, each with an add button.

            Listing them reads the provenance of every run (CKAN, DOIP), so it happens in a worker thread after
            the page is shown, and only for users who may add runs.
            """
            ui.label("Potential candidates for this leaderboard").classes("text-lg font-semibold text-gray-900")
            ui.label("Finished model runs that fit this benchmark (they read exactly its input file, nothing else) "
                     "and are not on the leaderboard yet. A new run appears here after the catalog has synced it, "
                     "usually a minute or two after it finishes; reload the page to see it.").classes("text-sm text-gray-600")
            loading = ui.row().classes("items-center gap-2")
            with loading:
                ui.spinner(size="sm")
                ui.label("Loading runs…").classes("text-sm text-gray-600")
            try:
                found = await run.io_bound(actions.candidates, selected)
            except Exception:
                logger.exception("could not list the candidate runs")
                loading.delete()
                error_label("Could not list the candidate runs.")
                return
            loading.delete()
            input_columns = list(((materialized_from_row(row) or {}).get("input") or {}).get("columns") or [])
            rows = [{
                "qid": c["qid"], "model": c["model_name"], "tag": c.get("docker_tag") or "—",
                "date": c["run_timestamp"][:10], "params": params_text(c.get("params") or {}),
                "used": columns_text(c.get("used_columns"), input_columns),
                "history": history_text(c.get("history_start"), c["history_end"]) if c.get("history_end") else "—",
            } for c in found if c["reason"] is None]
            if not rows:
                ui.label("No other matching runs right now.").classes("text-sm text-gray-500")
                return
            candidate_columns = [{"name": "model", "label": "Model", "field": "model", "align": "left",
                                  "style": "white-space: normal; max-width: 20rem"},
                                 {"name": "tag", "label": "Tag", "field": "tag", "align": "left"},
                                 {"name": "run", "label": "Run", "field": "qid", "align": "left"},
                                 {"name": "date", "label": "Date", "field": "date", "align": "left"},
                                 {"name": "params", "label": "Parameters", "field": "params", "align": "left",
                                  "style": "white-space: normal; max-width: 14rem"},
                                 {"name": "used", "label": "Used columns", "field": "used", "align": "left",
                                  "style": "white-space: normal; max-width: 14rem"},
                                 {"name": "history", "label": "History (A→B)", "field": "history", "align": "left",
                                  "style": "white-space: normal; min-width: 8rem"}]
            with ui.row().classes("items-center gap-2"):
                select_all = ui.button("Select all").props("flat no-caps dense")
                add_selected = ui.button("Add selected").props("no-caps dense")
            candidates = ui.table(columns=candidate_columns, rows=rows, row_key="qid", selection="multiple").props(
                "flat bordered dense hide-bottom").classes("max-w-6xl w-full")
            _help_headers(candidates, candidate_columns)
            candidates.add_slot("body-cell-model", _MODEL_CELL)

            def show_selection(_=None):
                count = len(candidates.selected)
                add_selected.set_text(f"Add selected ({count})" if count else "Add selected")
                add_selected.set_enabled(count > 0)

            def choose_all():
                candidates.selected = list(candidates.rows)
                show_selection()

            async def add_selection():
                qids = [row["qid"] for row in candidates.selected]
                if not _is_logged_in():
                    ui.notify(_LOGIN_EXPIRED, type="warning")
                    return
                if not qids:
                    return
                candidates.props("loading")
                add_selected.disable()
                select_all.disable()
                added, refused = [], []
                for qid in qids:  # one after the other: every run is scored and stored on its own
                    try:
                        await run.io_bound(lambda q=qid: actions.add_run(selected, q))
                        added.append(qid)
                    except (NotScorable, actions.Conflict, actions.NotFound, LoadError) as err:
                        refused.append(f"{qid}: {err}")
                    except Exception:
                        logger.exception("could not add the run %s", qid)
                        refused.append(f"{qid}: could not be added")
                if added:
                    ui.notify(f"Added to the leaderboard: {', '.join(added)}", type="positive")
                for reason in refused:
                    ui.notify(reason, type="negative", multi_line=True)
                board_view.refresh()
                candidates_view.refresh()

            select_all.on_click(choose_all)
            add_selected.on_click(add_selection)
            candidates.on_select(show_selection)
            show_selection()

        with view:
            board_view()
        if logged_in:
            async def load_candidates():
                with candidates_box:
                    await candidates_view()

            ui.timer(0.1, load_candidates, once=True)  # after the page is shown
