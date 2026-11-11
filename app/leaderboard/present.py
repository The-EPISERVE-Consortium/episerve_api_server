"""Format a leaderboard for display."""

from app.leaderboard.about import is_baseline
from app.leaderboard.metrics import METRICS
from app.leaderboard.service import TARGET_COVERAGE, Entry, Leaderboard

LABELS = {"mae": "MAE", "rmse": "RMSE", "coverage": "80% coverage", "interval_score": "interval score"}


def _fmt(metric: str, value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f}%" if metric == "coverage" else f"{value:.2f}"


def history_days(entry: Entry) -> int | None:
    """Inclusive number of days from history start to history end; None if the start is unknown."""
    if entry.history_start is None:
        return None
    return (entry.history_end - entry.history_start).days + 1


def _searchable(entry: Entry) -> str:
    params = " ".join(f"{k}={v}" for k, v in entry.params.items())
    return f"{entry.model_name} {entry.run_qid} {params}".lower()


def filter_entries(
    entries: list[Entry], model: str | None = None, min_history_days: int | float | None = 0, text: str | None = None,
) -> list[Entry]:
    """Entries of ``model`` (all if empty) with a history of at least ``min_history_days`` days.

    ``text`` keeps entries whose model name, run QID or parameters contain it (case-insensitive).
    An unknown history start counts as unknown length and is dropped whenever a minimum above 0 is set.
    """
    minimum = min_history_days or 0
    needle = (text or "").strip().lower()
    kept = []
    for entry in entries:
        if model and entry.model_name != model:
            continue
        if needle and needle not in _searchable(entry):
            continue
        if minimum > 0:
            days = history_days(entry)
            if days is None or days < minimum:
                continue
        kept.append(entry)
    return kept


def _run_date(timestamp: str) -> str:
    """'2026-10-16T22:48:17Z' -> '2026-10-16 22:48'; a dash when there is no usable timestamp."""
    if not timestamp or len(timestamp) < 16 or timestamp[10] not in "T ":
        return "—"
    return f"{timestamp[:10]} {timestamp[11:16]}"


def _distance(metric: str, value: float) -> float:
    return round(abs(value - TARGET_COVERAGE), 9) if metric == "coverage" else value


def params_text(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items()) or "—"


def history_text(start, end) -> str:
    """The history window as 'A → B'; an open start shows as '…'."""
    return f"{start.isoformat() if start else '…'} → {end.isoformat()}"


def columns_text(used: list[str] | None, all_columns: list[str]) -> str:
    """The columns a run used, short: ``all (5)``, the names when it used a selection, ``—`` when not recorded."""
    if used is None:
        return "—"
    if all_columns and list(used) == list(all_columns):
        return f"all ({len(used)})"
    return ", ".join(used) if used else "none"


def model_label(name: str) -> str:
    """Shorten the catalog's generic forecast model prefix for display.

    Args:
        name: Full model identifier.

    Returns:
        Display name, with the original identifier as a fallback.
    """
    return name.removeprefix("model__prediction__generic__") or name


def comparison_rows(board: Leaderboard, entries: list[Entry]) -> list[dict]:
    """Build compact comparison rows while retaining exact input names for tooltips.

    Args:
        board: Benchmark and its input column schema.
        entries: Ranked runs visible in the comparison table.

    Returns:
        Display rows with abbreviated model and input column labels.
    """
    rows = table_rows(board, entries)
    date_and_target = {board.benchmark.date_column, board.benchmark.target_column}
    for row, entry in zip(rows, entries, strict=True):
        used = entry.used_columns
        if used is None:
            label, names = "Not recorded", ""
        elif not used:
            label, names = "None", ""
        else:
            names = ", ".join(used)
            if board.input_columns and set(used) == set(board.input_columns):
                label = f"All {len(used)} columns"
            elif len(used) == 2 and set(used) == date_and_target:
                label = "Date + target"
            else:
                label = f"{len(used)} column{'s' if len(used) != 1 else ''}"
        row.update(model_label=model_label(entry.model_name), input_label=label, input_names=names)
    return rows


def table_rows(board: Leaderboard, entries: list[Entry] | None = None) -> list[dict]:
    """One display row per entry (default: all of the board's), with the best value of each column marked.

    Ranks and best-value marks refer to the given entries only.
    """
    entries = board.entries if entries is None else entries
    best_values = {}
    for metric in METRICS:
        distances = [_distance(metric, e.metrics[metric]) for e in entries if e.metrics.get(metric) is not None]
        best_values[metric] = min(distances) if distances else None
    rows = []
    for rank, entry in enumerate(entries, start=1):
        rows.append({
            "rank": rank,
            "model": f"{entry.model_name}",
            "baseline": is_baseline(entry.model_name),
            "run_qid": entry.run_qid,
            "date": _run_date(entry.run_timestamp),
            "params": params_text(entry.params),
            "used": columns_text(entry.used_columns, board.input_columns),
            "history": history_text(entry.history_start, entry.history_end),
            "history_days": history_days(entry),
            "scored": f"{entry.n_scored}/{entry.n_expected}",
            **{m: _fmt(m, entry.metrics.get(m)) for m in METRICS},
            "raw": {m: entry.metrics.get(m) for m in METRICS},
            "best": {
                m: entry.metrics.get(m) is not None and _distance(m, entry.metrics[m]) == best_values[m]
                for m in METRICS
            },
        })
    return rows


def _num(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def point_rows(entry: Entry) -> list[dict]:
    """Per-date rows for the detail table; error is forecast minus observed."""
    rows = []
    for p in entry.points:
        truth, forecast = p.get("truth"), p.get("forecast")
        error = forecast - truth if truth is not None and forecast is not None else None
        rows.append({
            "date": str(p["date"]),
            "observed": _num(truth), "forecast": _num(forecast),
            "q10": _num(p.get("q10")), "q90": _num(p.get("q90")),
            "error": _num(error),
        })
    return rows


def history_window(entry: Entry) -> str:
    return history_text(entry.history_start, entry.history_end)


def chart_option(entry: Entry, history: list[dict] | None = None) -> dict:
    """Build an accessible forecast chart with a shaded q10–q90 interval.

    Args:
        entry: Stored forecast and observed values, including optional quantiles.
        history: Optional date and value pairs from the frozen model input.

    Returns:
        ECharts options with black observations, red forecasts, and optional history.
    """
    history = history or []
    by_date = {point["date"]: point for point in entry.points}
    dates = sorted({point["date"] for point in history} | set(by_date))
    points = [by_date.get(day, {"date": day}) for day in dates]
    input_by_date = {point["date"]: point["value"] for point in history}
    q10 = [p.get("q10") for p in points]
    widths = [
        p["q90"] - p["q10"] if p.get("q10") is not None and p.get("q90") is not None else None
        for p in points
    ]
    # Missing q10/q90 leave a gap; the band is only drawn where both bounds exist.
    base = [lo if w is not None else None for lo, w in zip(q10, widths)]
    legend = {
        "data": (["Model input"] if history else []) + ["Observed", "Forecast", {"name": "q10–q90", "icon": "roundRect"}],
        "bottom": 4,
        "type": "plain",
        "textStyle": {"color": "#475569", "fontSize": 13},
        ":formatter": "(name) => name === 'q10–q90' ? '80% prediction interval (q10–q90)' : name",
    }
    grid = {"left": 14, "right": 48, "top": 40, "bottom": 76, "containLabel": True}
    return {
        "tooltip": {
            "trigger": "axis",
            ":formatter": (
                "(params) => { const q = params[0].dataIndex; const d = %s; const e = %s;"
                " const fmt = (v) => (v === null || v === undefined) ? '—' : Number(v).toFixed(2);"
                " let t = params[0].axisValueLabel;"
                " params.forEach(p => { if (p.seriesName === 'Observed' || p.seriesName === 'Forecast' || p.seriesName === 'Model input')"
                " t += '<br/>' + p.marker + p.seriesName + ': ' + fmt(p.value); });"
                " if (params.some(p => p.seriesName === 'Forecast' && p.value !== null))"
                " t += '<br/>q10: ' + fmt(d[q]) + '<br/>q90: ' + fmt(e[q]); return t; }"
            ) % (_json(q10), _json([p.get("q90") for p in points])),
        },
        "animation": False,
        "aria": {"enabled": True},
        "color": ["#111111", "#c62828", "#c62828", "#c62828", "#111111"],
        "grid": grid,
        "legend": legend,
        "xAxis": {"type": "category", "boundaryGap": False, "data": [p["date"] for p in points],
                  "axisLabel": {"color": "#475569", "hideOverlap": True,
                                "interval": max(0, (len(points) - 1) // 4 - 1), "showMaxLabel": True,
                                ":formatter": "(value) => { const d = new Date(value + 'T00:00:00Z'); return d.toLocaleDateString('en-GB', {day: '2-digit', month: 'short', "
                                + ("year: '2-digit', " if history else "") + "timeZone: 'UTC'}); }"},
                  "axisLine": {"lineStyle": {"color": "#d4cfc5"}}},
        "yAxis": {"type": "value", "scale": True, "splitNumber": 4,
                  ":min": "(extent) => Math.min(0, extent.min)", "nameTextStyle": {"color": "#475569"},
                  "axisLabel": {"color": "#475569"}, "splitLine": {"lineStyle": {"color": "#e2e8f0"}}},
        "media": [{"query": {"maxWidth": 500}, "option": {
            "grid": {**grid, "bottom": 135 if history else 110},
            "legend": {**legend, "orient": "vertical", "left": "center", "bottom": 0,
                       "itemGap": 8, "textStyle": {"fontSize": 11}},
        }}],
        "series": [
            {"name": "Observed", "type": "line", "showSymbol": False, "itemStyle": {"color": "#111111"}, "lineStyle": {"width": 2, "color": "#111111"}, "data": [p.get("truth") for p in points]},
            {"name": "Forecast", "type": "line", "showSymbol": False, "itemStyle": {"color": "#c62828"}, "lineStyle": {"width": 2, "color": "#c62828"}, "data": [p.get("forecast") for p in points]},
            {"name": "q10 base", "type": "line", "stack": "band", "symbol": "none", "silent": True,
             "lineStyle": {"opacity": 0}, "areaStyle": {"opacity": 0}, "data": base},
            {"name": "q10–q90", "type": "line", "stack": "band", "symbol": "none", "silent": True,
             "itemStyle": {"color": "rgba(198,40,40,0.3)"},
             "lineStyle": {"opacity": 0}, "areaStyle": {"color": "rgba(198,40,40,0.14)"}, "data": widths},
        ] + ([{"name": "Model input", "type": "line", "showSymbol": False,
               "itemStyle": {"color": "#111111"}, "lineStyle": {"color": "#111111", "type": "dashed", "width": 2},
               "data": [input_by_date.get(day) for day in dates],
               "markLine": {"silent": True, "symbol": "none", "label": {"formatter": "Forecast starts", "position": "insideEndTop"},
                            "lineStyle": {"color": "#6b7280", "type": "dashed"},
                            "data": [{"xAxis": entry.points[0]["date"]}] if entry.points else []}}] if history else []),
    }


def _json(values: list) -> str:
    import json

    return json.dumps(values)


def summary(board: Leaderboard) -> str:
    """Summarize the number of runs, ranking metric, and data version differences.

    Args:
        board: Ranked runs for the benchmark.

    Returns:
        Short summary of the complete leaderboard.
    """
    count = len(board.entries)
    text = f"{count} {'run' if count == 1 else 'runs'} · Ranked by {LABELS[board.rank_by]}"
    commits = {e.truth_commit for e in board.entries if e.truth_commit}
    if len(commits) > 1:
        text += f". Entries were scored against {len(commits)} different data versions."
    return text
