"""Benchmark definition: one series cut into an input range and a reference range.

The series is a dataset column (optionally filtered to one value). The *input* range
(``history_start``..``history_end``) is what a model run may see; the *reference* range
(``test_start``..``test_end``) is what its forecast is compared against. Both are read
from lakeFS at the commit pinned when the benchmark is finalized.
"""

import math
import re
import statistics
from dataclasses import dataclass
from datetime import date

MAX_NAME = 200
MAX_COLUMN = 200
MAX_FILTER_VALUE = 500
_QID = re.compile(r"Q[0-9]+")
_FILENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}")


class DefinitionError(ValueError):
    """A benchmark definition is invalid; the message says why."""


@dataclass(frozen=True)
class Benchmark:
    """The prediction task and how its input and reference data are generated."""

    dataset_qid: str
    target_column: str
    date_column: str
    test_start: date
    test_end: date
    filter_column: str | None = None
    filter_value: str | None = None
    history_start: date | None = None
    history_end: date | None = None
    input_filename: str = "input.parquet"
    prediction_length: int | None = None
    data_commit: str | None = None


@dataclass(frozen=True)
class Horizon:
    """How far a forecast must reach to cover every observed date of the test window."""

    prediction_length: int
    frequency_days: int
    regular: bool


def validate(benchmark: Benchmark, name: str) -> None:
    """Check a definition (and its name) without touching any data.

    Args:
        benchmark: The definition; ``history_end`` may be None ("auto").
        name: The display name.

    Raises:
        DefinitionError: With the reason.
    """
    if not (name or "").strip() or len(name.strip()) > MAX_NAME:
        raise DefinitionError(f"A name of 1 to {MAX_NAME} characters is required (name).")
    if not _QID.fullmatch(benchmark.dataset_qid or ""):
        raise DefinitionError("Choose a dataset (dataset id like Q123).")
    for label, value in (("target", benchmark.target_column), ("date", benchmark.date_column)):
        if not (value or "").strip() or len(value) > MAX_COLUMN:
            raise DefinitionError(f"The {label} column is required (column, at most {MAX_COLUMN} characters).")
    if benchmark.filter_column is not None and benchmark.filter_column != "":
        if not benchmark.filter_column.strip() or len(benchmark.filter_column) > MAX_COLUMN:
            raise DefinitionError(f"The filter column must be 1 to {MAX_COLUMN} characters (filter column).")
        if not benchmark.filter_value or len(benchmark.filter_value) > MAX_FILTER_VALUE:
            raise DefinitionError(f"A filter value of 1 to {MAX_FILTER_VALUE} characters is required with a filter column (filter value).")
    elif benchmark.filter_value:
        raise DefinitionError("A filter value needs a filter column.")
    if benchmark.test_start is None or benchmark.test_end is None:
        raise DefinitionError("The test start and end are required.")
    if benchmark.test_start > benchmark.test_end:
        raise DefinitionError("The test start must not be after the test end.")
    if benchmark.history_end is not None and benchmark.history_end >= benchmark.test_start:
        raise DefinitionError("The history must end before the test start.")
    if (
        benchmark.history_start is not None and benchmark.history_end is not None
        and benchmark.history_start > benchmark.history_end
    ):
        raise DefinitionError("The history start must not be after the history end.")
    if not _FILENAME.fullmatch(benchmark.input_filename or "") or not benchmark.input_filename.endswith(".parquet"):
        raise DefinitionError("The input file name must look like input.parquet (letters, digits, _ . -; ending in .parquet).")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def input_sql(benchmark: Benchmark) -> str:
    """The SQL (table ``df``) that cuts the input range out of the dataset.

    Raises:
        DefinitionError: If ``history_end`` is not set.
    """
    if benchmark.history_end is None:
        raise DefinitionError("The history end is not set.")
    date_col = _ident(benchmark.date_column)
    cast = f"CAST({date_col} AS DATE)"
    where = []
    if benchmark.filter_column:
        where.append(f"{_ident(benchmark.filter_column)} = {_literal(benchmark.filter_value or '')}")
    if benchmark.history_start is not None:
        where.append(f"{cast} >= DATE '{benchmark.history_start.isoformat()}'")
    where.append(f"{cast} <= DATE '{benchmark.history_end.isoformat()}'")
    return (
        f"SELECT {cast} AS {date_col}, {_ident(benchmark.target_column)} FROM df "
        f"WHERE {' AND '.join(where)} ORDER BY {date_col}"
    )


def evaluation_sql(benchmark: Benchmark) -> str:
    """The SQL (table ``df``) that cuts the test window (the evaluation data) out of the dataset."""
    date_col = _ident(benchmark.date_column)
    cast = f"CAST({date_col} AS DATE)"
    where = []
    if benchmark.filter_column:
        where.append(f"{_ident(benchmark.filter_column)} = {_literal(benchmark.filter_value or '')}")
    where.append(f"{cast} >= DATE '{benchmark.test_start.isoformat()}'")
    where.append(f"{cast} <= DATE '{benchmark.test_end.isoformat()}'")
    return (
        f"SELECT {cast} AS {date_col}, {_ident(benchmark.target_column)} FROM df "
        f"WHERE {' AND '.join(where)} ORDER BY {date_col}"
    )


def derive_horizon(observed: list[date], last_history: date) -> Horizon:
    """Forecast length (in steps of the data's frequency) that reaches the last observed date.

    Args:
        observed: Observed dates inside the test window.
        last_history: Last observed date of the input range.

    Raises:
        DefinitionError: If there are no observed dates or the input range reaches the window.
    """
    dates = sorted(set(observed))
    if not dates:
        raise DefinitionError("The test window has no observed dates.")
    if last_history >= dates[0]:
        raise DefinitionError("The history overlaps the test window.")
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    frequency = statistics.median_low(gaps) if gaps else max(1, (dates[0] - last_history).days)
    steps = math.ceil((dates[-1] - last_history).days / frequency)
    return Horizon(max(1, steps), frequency, all(g == frequency for g in gaps))
