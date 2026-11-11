"""Results, forecast rows, and provenance for one stored benchmark run."""

from datetime import datetime, timezone
import logging

from nicegui import run, ui

from app.leaderboard import actions

from app.leaderboard.present import chart_option, history_days, history_window, model_label, point_rows
from app.leaderboard.service import Entry, Leaderboard

logger = logging.getLogger(__name__)

_TABS = {"results": "Results", "forecast-data": "Forecast data", "run-details": "Run details"}
_METRIC_HELP = {
    "mae": "Mean absolute error; lower is better.",
    "rmse": "Root mean squared error; lower is better.",
    "coverage": "Share of observed values inside the q10–q90 interval. Target: 80%.",
    "interval_score": "Interval score balances interval width and missed observations; lower is better.",
}


def _date(value) -> str:
    """Format a date boundary without inventing an unknown value.

    Args:
        value: A date object or missing boundary.

    Returns:
        Readable date or an explicit unknown value.
    """
    return value.strftime("%d %b %Y") if value else "Not recorded"


def _timestamp(value: str) -> str:
    """Format an ISO timestamp and retain its timezone meaning.

    Args:
        value: Stored timestamp, optionally with a UTC offset.

    Returns:
        Readable timestamp, the original unparseable value, or a missing label.
    """
    if not value:
        return "Not recorded"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo:
        return parsed.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    return parsed.strftime("%d %b %Y, %H:%M")


def _field(label: str, value: str, *, mono: bool = False) -> None:
    """Render an accessible metadata term and its complete stored value.

    Args:
        label: Name of the metadata field.
        value: Value to display without truncation.
        mono: Whether the value is a technical identifier.
    """
    with ui.element("div").classes("epi-run-field"):
        with ui.element("dt"):
            ui.label(label)
        with ui.element("dd"):
            ui.label(value).classes("epi-benchmark-mono" if mono else "")


def _metadata(entry: Entry) -> None:
    """Render model configuration and the exact provenance of a scored run.

    Args:
        entry: Stored scored run with configuration and version pins.
    """
    for heading, fields in (
        ("Model and input", [
            ("Model identifier", entry.model_name, True),
            ("Run identifier", entry.run_qid, True),
            ("Docker tag", entry.docker_tag or "Not recorded", True),
            ("Run date", entry.run_timestamp or "Not recorded", False),
            ("History window", history_window(entry), False),
            ("Input columns", "Not recorded" if entry.used_columns is None else ", ".join(entry.used_columns) or "None", True),
        ]),
        ("Parameters", [(key, str(value), True) for key, value in entry.params.items()] or [("Parameters", "—", False)]),
        ("Data and scoring provenance", [
            ("lakeFS commit", entry.truth_commit or "Not recorded", True),
            ("CKAN last modified", entry.truth_as_of or "Not recorded", False),
            ("Scored at", entry.scored_at or "Not recorded", False),
        ]),
    ):
        with ui.element("section").classes("epi-run-section"):
            ui.label(heading).classes("epi-benchmark-heading").props("role=heading aria-level=2")
            with ui.element("dl").classes("epi-run-metadata"):
                for label, value, mono in fields:
                    _field(label, value, mono=mono)


def _forecast_chart(entry: Entry, board: Leaderboard) -> None:
    """Render the forecast and lazily fetch optional frozen model input history.

    Args:
        entry: Stored scored run with its recorded input window.
        board: Parent benchmark supplying the target column and identifier.
    """
    state = {"history": None, "loading": False, "error": ""}

    async def change_history() -> None:
        """Update the chart after a history toggle and retain successful reads."""
        if state["loading"]:
            return
        if toggle.value and state["history"] is None:
            state["loading"] = True
            toggle.disable()
            state["error"] = ""
            status.set_text("Loading frozen model input…")
            try:
                state["history"] = await run.io_bound(actions.read_input_history, board.benchmark_id, entry.run_qid)
            except Exception:
                logger.exception("could not load frozen input history for run %s", entry.run_qid)
                state["error"] = "Model input history could not be loaded. Switch it on again to retry."
                toggle.set_value(False)
                status.set_text(state["error"])
                return
            finally:
                toggle.enable()
                state["loading"] = False
        history = state["history"] if toggle.value else None
        status.set_text("Target values from the frozen benchmark input supplied to this run." if history else state["error"])
        options = chart_option(entry, history)
        options["yAxis"]["name"] = board.benchmark.target_column
        chart.options.clear()
        chart.options.update(options)
        chart.update()

    with ui.row().classes("epi-run-history-control"):
        toggle = ui.switch("Show model input history", value=False, on_change=change_history)
    status = ui.label("").classes("epi-benchmark-note epi-run-history-status").props("role=status aria-live=polite")
    if entry.used_columns is not None and board.benchmark.target_column not in entry.used_columns:
        toggle.disable()
        status.set_text("This run did not use the benchmark target column as an input.")
    options = chart_option(entry)
    options["yAxis"]["name"] = board.benchmark.target_column
    chart = ui.echart(options).classes("epi-run-chart").props(
        "aria-label='Observed values, forecast, prediction interval, and optional model input history'"
    )


def render(entry: Entry, board: Leaderboard, *, tab: str = "results") -> None:
    """Render stored run results with an optional view of the frozen input.

    Args:
        entry: Stored scored run to display.
        board: Parent benchmark providing its name and reference boundaries.
        tab: Requested initial detail tab, falling back to Results when invalid.
    """
    title = model_label(entry.model_name).replace("-", " ")
    title = title[:1].upper() + title[1:]
    ui.label(title).classes("epi-page-title epi-benchmark-title").props("role=heading aria-level=1")
    with ui.row().classes("epi-run-identity"):
        ui.label(f"Run {entry.run_qid}").classes("epi-benchmark-identifier")
        ui.link("Open in ModelHub ↗", f"https://data.episerve.zib.de/dataset/{entry.run_qid.lower()}", new_tab=True)
    ui.link(board.name or "Benchmark", f"/ui/benchmarks/{board.benchmark_id}").classes("epi-run-benchmark-link")
    ui.label(f"Run on {_timestamp(entry.run_timestamp)}").classes("epi-benchmark-body")
    selected = tab if tab in _TABS else "results"
    with ui.tabs(value=selected).props("align=left no-caps").classes("epi-benchmark-tabs") as tabs:
        for key, label in _TABS.items():
            ui.tab(key, label=label)
    with ui.tab_panels(tabs, value=selected, animated=False).classes("epi-benchmark-panels"):
        with ui.tab_panel("results").classes("epi-benchmark-panel epi-run-results"):
            with ui.element("dl").classes("epi-run-metrics"):
                for metric, label in (("mae", "MAE"), ("rmse", "RMSE"), ("coverage", "Coverage"), ("interval_score", "Interval score")):
                    value = entry.metrics.get(metric)
                    formatted = "—" if value is None else f"{value * 100:.2f}%" if metric == "coverage" else f"{value:.2f}"
                    with ui.element("div").classes("epi-run-metric"):
                        with ui.element("dt"):
                            ui.label(label).tooltip(_METRIC_HELP[metric])
                        with ui.element("dd"):
                            ui.label(formatted)
                        if metric == "coverage":
                            ui.label("Target: 80%").classes("epi-benchmark-note")
            ui.label(f"{entry.n_scored} of {entry.n_expected} reference values scored").classes("epi-benchmark-note")
            with ui.element("section").classes("epi-run-forecast"):
                ui.label("Forecast vs observed").classes("epi-benchmark-heading").props("role=heading aria-level=2")
                ui.label(f"{_date(board.benchmark.test_start)} – {_date(board.benchmark.test_end)} · {board.benchmark.target_column}").classes("epi-benchmark-body")
                _forecast_chart(entry, board)
            ui.label("Coverage is the share of observed values inside the q10–q90 interval.").classes("epi-benchmark-note")
            with ui.element("div").classes("epi-run-windows"):
                days = history_days(entry)
                for label, duration, first, last, color in (
                    ("Model input", f" · {days} days" if days is not None else "", entry.history_start, entry.history_end, "input"),
                    ("Scoring reference", f" · {(board.benchmark.test_end - board.benchmark.test_start).days + 1} days", board.benchmark.test_start, board.benchmark.test_end, "reference"),
                ):
                    with ui.row().classes("epi-benchmark-window-label"):
                        ui.element("span").classes(f"epi-benchmark-swatch epi-benchmark-period-{color}")
                        with ui.column().classes("gap-0"):
                            ui.label(label + duration).classes("font-semibold text-sm")
                            ui.label(f"{_date(first)} – {_date(last)}").classes("epi-benchmark-body")
            ui.link("View forecast data →", f"/ui/benchmarks/{board.benchmark_id}/runs/{entry.run_qid}?tab=forecast-data").on(
                "click", lambda: tabs.set_value("forecast-data"), js_handler="(event) => { event.preventDefault(); emit(); }",
            ).classes("epi-run-data-link")
        with ui.tab_panel("forecast-data").classes("epi-benchmark-panel"):
            ui.label("Forecast data").classes("epi-benchmark-heading").props("role=heading aria-level=2")
            ui.label("Error = forecast − observed. Missing values are shown as —.").classes("epi-benchmark-note")
            ui.label("Scroll horizontally to see all columns.").classes("epi-run-scroll-hint epi-benchmark-note")
            ui.table(
                columns=[{"name": column, "label": label, "field": column,
                          "align": "left" if column == "date" else "right"}
                         for column, label in (("date", "Date"), ("observed", "Observed"), ("forecast", "Forecast"),
                                               ("q10", "q10"), ("q90", "q90"), ("error", "Error (forecast − observed)"))],
                rows=point_rows(entry), row_key="date", pagination={"rowsPerPage": 10},
            ).props('flat bordered :rows-per-page-options="[10, 25, 50, 0]"').classes("epi-benchmark-table epi-run-table w-full")
        with ui.tab_panel("run-details").classes("epi-benchmark-panel epi-run-details"):
            _metadata(entry)
