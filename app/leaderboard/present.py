"""Format a leaderboard for display."""

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


def filter_entries(entries: list[Entry], model: str | None = None, min_history_days: int | float | None = 0) -> list[Entry]:
    """Entries of ``model`` (all if empty) with a history of at least ``min_history_days`` days.

    An unknown history start counts as unknown length and is dropped whenever a minimum above 0 is set.
    """
    minimum = min_history_days or 0
    kept = []
    for entry in entries:
        if model and entry.model_name != model:
            continue
        if minimum > 0:
            days = history_days(entry)
            if days is None or days < minimum:
                continue
        kept.append(entry)
    return kept


def _distance(metric: str, value: float) -> float:
    return round(abs(value - TARGET_COVERAGE), 9) if metric == "coverage" else value


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
        start = entry.history_start.isoformat() if entry.history_start else "…"
        rows.append({
            "rank": rank,
            "model": f"{entry.model_name}",
            "run_qid": entry.run_qid,
            "params": ", ".join(f"{k}={v}" for k, v in entry.params.items()) or "—",
            "history": f"{start} → {entry.history_end.isoformat()}",
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
    start = entry.history_start.isoformat() if entry.history_start else "…"
    return f"{start} → {entry.history_end.isoformat()}"


def chart_option(entry: Entry) -> dict:
    """ECharts option: observed and forecast lines with a shaded q10-q90 band (stacked areas)."""
    points = entry.points
    q10 = [p.get("q10") for p in points]
    widths = [
        p["q90"] - p["q10"] if p.get("q10") is not None and p.get("q90") is not None else None
        for p in points
    ]
    # Missing q10/q90 leave a gap; the band is only drawn where both bounds exist.
    base = [lo if w is not None else None for lo, w in zip(q10, widths)]
    return {
        "tooltip": {
            "trigger": "axis",
            ":formatter": (
                "(params) => { const q = params[0].dataIndex; const d = %s; const e = %s;"
                " const fmt = (v) => (v === null || v === undefined) ? '—' : Number(v).toFixed(2);"
                " let t = params[0].axisValueLabel;"
                " params.forEach(p => { if (p.seriesName === 'Observed' || p.seriesName === 'Forecast')"
                " t += '<br/>' + p.marker + p.seriesName + ': ' + fmt(p.value); });"
                " t += '<br/>q10: ' + fmt(d[q]) + '<br/>q90: ' + fmt(e[q]); return t; }"
            ) % (_json(q10), _json([p.get("q90") for p in points])),
        },
        "legend": {"data": ["Observed", "Forecast", "q10–q90"]},
        "xAxis": {"type": "category", "data": [p["date"] for p in points]},
        "yAxis": {"type": "value", "scale": True},
        "series": [
            {"name": "Observed", "type": "line", "data": [p.get("truth") for p in points]},
            {"name": "Forecast", "type": "line", "data": [p.get("forecast") for p in points]},
            {"name": "q10 base", "type": "line", "stack": "band", "symbol": "none", "silent": True,
             "lineStyle": {"opacity": 0}, "areaStyle": {"opacity": 0}, "data": base},
            {"name": "q10–q90", "type": "line", "stack": "band", "symbol": "none", "silent": True,
             "lineStyle": {"opacity": 0}, "areaStyle": {"color": "rgba(59,130,246,0.2)"}, "data": widths},
        ],
    }


def _json(values: list) -> str:
    import json

    return json.dumps(values)


def summary(board: Leaderboard) -> str:
    """The sentence stating the setup and the best entry."""
    b = board.benchmark
    setup = (
        f"Target {b.target_column}, test window {b.test_start} → {b.test_end}, "
        f"{board.truth_points} observations (data as of {board.truth_as_of or 'unknown'})."
    )
    if not board.entries:
        return f"{setup} No scorable runs for this task."
    best = board.entries[0]
    text = f"{setup} Best entry: {best.model_name}, ranked by {LABELS[board.rank_by]}."
    commits = {e.truth_commit for e in board.entries if e.truth_commit}
    if len(commits) == 1:
        text += f" Scored against lakeFS commit {next(iter(commits))[:12]}."
    elif len(commits) > 1:
        text += f" Entries were scored against {len(commits)} different data versions."
    return text
