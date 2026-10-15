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


_DATE = r"(?:DATE\s*)?'(\d{4}-\d{2}-\d{2})'(?!\s*[-+*/|])"

# Constructs that can widen, negate or replace the filtered rows; their
# presence makes the history window unprovable.
_WINDOW_BLOCKERS = re.compile(r"\b(?:OR|NOT|UNION|EXCEPT|INTERSECT|INTERVAL)\b", re.IGNORECASE)
_FILTER_BLOCKERS = re.compile(r"\b(?:OR|NOT|CASE)\b", re.IGNORECASE)


_QUALIFIER = r'(?:"?\w+"?\.)?'


def _column_pattern(column: str) -> str:
    """Match a bare or quoted column, optionally qualified, as a whole word."""
    col = re.escape(column)
    return rf'(?<![\w"]){_QUALIFIER}"?{col}"?(?!\w)'


def _bound_pattern(date_column: str, operators: str) -> re.Pattern:
    col = re.escape(date_column)
    column = (
        rf'(?:CAST\(\s*{_QUALIFIER}"?{col}"?\s+AS\s+DATE\s*\)'
        rf"|{_column_pattern(date_column)})"
    )
    return re.compile(rf"{column}\s*({operators})\s*{_DATE}", re.IGNORECASE)


def _bound_dates(sql: str, date_column: str, operators: str) -> list[date]:
    """Effective dates of every bound in ``sql`` with the given operators.

    Exclusive operators are converted to the inclusive day they cover.
    Raises ValueError for impossible dates.
    """
    dates = []
    for operator, text in _bound_pattern(date_column, operators).findall(sql):
        value = date.fromisoformat(text)
        if operator in ("<", ">"):
            value += timedelta(days=-1 if operator == "<" else 1)
        dates.append(value)
    return dates


def parse_history_window(sql: str, date_column: str) -> HistoryWindow | None:
    """Read the date bounds a transformation applied to the history.

    Args:
        sql: The run's transformation SQL.
        date_column: Date column of the source dataset.

    Returns:
        The window, or None when no single upper date bound can be recognised,
        when the bounds are impossible dates, when the upper bounds disagree,
        when the start is after the end, or when the SQL contains OR, NOT,
        UNION/EXCEPT/INTERSECT or INTERVAL (the bounds might not hold).
        Several distinct lower bounds leave the start unbounded (None).
    """
    if _WINDOW_BLOCKERS.search(sql):
        return None
    try:
        uppers = set(_bound_dates(sql, date_column, "<=|<"))
        lowers = set(_bound_dates(sql, date_column, ">=|>"))
    except (ValueError, OverflowError):
        return None
    if len(uppers) != 1:
        return None
    start = next(iter(lowers)) if len(lowers) == 1 else None
    end = next(iter(uppers))
    if start is not None and start > end:
        return None
    return HistoryWindow(start, end)


def has_equality_filter(sql: str, column: str, value: str) -> bool:
    """Whether the SQL restricts ``column`` to exactly ``value``.

    The column name is matched case-insensitively, the value case-sensitively.
    Any OR, NOT or CASE in the SQL makes the filter ambiguous, so it counts as
    not a filter.
    """
    if _FILTER_BLOCKERS.search(sql):
        return False
    literal = re.escape(value.replace("'", "''"))
    pattern = rf"(?i:{_column_pattern(column)})\s*=\s*'{literal}'(?!')"
    return re.search(pattern, sql) is not None


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
