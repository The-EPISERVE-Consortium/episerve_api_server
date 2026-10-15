"""Benchmark definition and the rules that decide whether a run can be scored."""

import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Benchmark:
    """What is predicted: the series and the window the forecasts are scored on."""

    dataset_qid: str
    target_column: str
    date_column: str
    test_start: date
    test_end: date
    filter_column: str | None = None
    filter_value: str | None = None


@dataclass(frozen=True)
class HistoryWindow:
    """Dates of input history a run used; ``start`` is None when unbounded."""

    start: date | None
    end: date


@dataclass(frozen=True)
class Qualification:
    """Outcome of checking one run against a benchmark.

    ``relevant`` is False when the run concerns another dataset (not listed).
    A non-empty ``reason`` means it is listed as not scorable.
    """

    relevant: bool
    reason: str | None = None
    window: HistoryWindow | None = None


_DATE = r"(?:DATE\s*)?'(\d{4}-\d{2}-\d{2})'"


def _bound_pattern(date_column: str, operators: str) -> re.Pattern:
    col = re.escape(date_column)
    column = rf'(?:CAST\(\s*"?{col}"?\s+AS\s+DATE\s*\)|"?{col}"?)'
    return re.compile(rf"{column}\s*({operators})\s*{_DATE}", re.IGNORECASE)


def parse_history_window(sql: str, date_column: str) -> HistoryWindow | None:
    """Read the date bounds a transformation applied to the history.

    Args:
        sql: The run's transformation SQL.
        date_column: Date column of the source dataset.

    Returns:
        The window, or None when no upper date bound can be recognised.
    """
    upper = _bound_pattern(date_column, "<=|<").search(sql)
    if upper is None:
        return None
    end = date.fromisoformat(upper.group(2))
    if upper.group(1) == "<":
        end -= timedelta(days=1)
    lower = _bound_pattern(date_column, ">=|>").search(sql)
    start = None
    if lower is not None:
        start = date.fromisoformat(lower.group(2))
        if lower.group(1) == ">":
            start += timedelta(days=1)
    return HistoryWindow(start, end)


def has_equality_filter(sql: str, column: str, value: str) -> bool:
    """Whether the SQL restricts ``column`` to exactly ``value``."""
    col = re.escape(column)
    literal = re.escape(value).replace("''", "'")
    pattern = rf"\"?{col}\"?\s*=\s*'{literal}'"
    return re.search(pattern, sql, re.IGNORECASE) is not None


def qualify_run(run: dict, benchmark: Benchmark) -> Qualification:
    """Decide whether a catalog run can be an entry of the benchmark.

    Args:
        run: A record from ``ckan_client.list_model_runs()``.
        benchmark: The benchmark being evaluated.

    Returns:
        ``relevant=False`` for runs on other datasets; otherwise a reason when
        the run cannot be scored, or the parsed history window when it can.
    """
    qids = run.get("input_dataset_qids") or []
    if benchmark.dataset_qid not in qids:
        return Qualification(relevant=False)
    if run.get("status") != "success":
        return Qualification(True, f"run status is {run.get('status') or 'unknown'}, not success")
    statements = run.get("data_transformation_sql") or []
    index = qids.index(benchmark.dataset_qid)
    sql = statements[index] if len(statements) == len(qids) else None
    window = parse_history_window(sql, benchmark.date_column) if sql else None
    if window is None:
        return Qualification(True, "history window not found in the run's SQL")
    if window.end >= benchmark.test_start:
        return Qualification(True, f"history ends {window.end}, on or after the test start", window)
    if benchmark.filter_column and benchmark.filter_value is not None:
        if not has_equality_filter(sql, benchmark.filter_column, benchmark.filter_value):
            return Qualification(
                True,
                f"filter {benchmark.filter_column} = {benchmark.filter_value!r} not found in the run's SQL",
                window,
            )
    return Qualification(True, None, window)
