"""Benchmark definition: one series cut into an input range and a reference range.

The series is a dataset column (optionally narrowed by any number of equality filters, column = value); further
*covariates* (other datasets) can be added. The *input* range (``history_start``..``history_end``) is what a model run
may see; the *reference* range (``test_start``..``test_end``) is what its forecast is compared against. When the
benchmark is finalized both are generated from the lakeFS commit it pins and stored as two files (see
``materialize``); the model input is always that one generated file.
"""

import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date

from app.config import settings

MAX_NAME = 200
MAX_COLUMN = 200
MAX_FILTER_VALUE = 500
_QID = re.compile(r"Q[0-9]+")
_FILENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}")


class DefinitionError(ValueError):
    """A benchmark definition is invalid; the message says why."""


AGGREGATIONS = ("mean", "sum", "last")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")


@dataclass(frozen=True)
class Covariate:
    """A further data source whose columns are added to the model input, aligned to the target series.

    The target series sets the time grid; the rows of the source between two grid dates are aggregated
    (``aggregation``) into the later date. In the input file the columns are named ``<name>__<column>``.
    """

    name: str
    dataset_qid: str
    date_column: str
    columns: tuple[str, ...]
    filters: tuple[tuple[str, str], ...] = ()
    aggregation: str = "mean"

    def output_columns(self) -> list[str]:
        """The column names in the input file: ``<name>__<column>`` with everything but letters, digits and _ in the
        column replaced by _ (so ``temperature_2m_mean (°C)`` becomes ``temperature_2m_mean_C``)."""
        return [f"{self.name}__{re.sub(r'[^0-9A-Za-z_]+', '_', column).strip('_')}" for column in self.columns]


@dataclass(frozen=True)
class Benchmark:
    """The prediction task and how its input and reference data are generated."""

    dataset_qid: str
    target_column: str
    date_column: str
    test_start: date
    test_end: date
    filters: tuple[tuple[str, str], ...] = ()   # equality filters (column, value) that together select one series
    history_start: date | None = None
    history_end: date | None = None
    input_filename: str = "input.parquet"
    prediction_length: int | None = None
    data_commit: str | None = None
    covariates: tuple[Covariate, ...] = ()   # with covariates the model input is one generated file (see materialize)


def covariates_from_row(row: dict) -> tuple[Covariate, ...]:
    """The covariate sources of a stored benchmark row; ``covariates`` is JSON text or a list of dicts."""
    stored = row.get("covariates")
    if isinstance(stored, str):
        stored = json.loads(stored) if stored else []
    return tuple(
        Covariate(name=item["name"], dataset_qid=item["dataset_qid"], date_column=item["date_column"],
                  columns=tuple(item["columns"]), filters=tuple((c, v) for c, v in item.get("filters") or []),
                  aggregation=item.get("aggregation") or "mean")
        for item in (stored or [])
    )


def normalize_covariates(items) -> tuple[Covariate, ...]:
    """Covariates from dicts (API, form) or Covariate objects; text is trimmed."""
    out = []
    for item in items or []:
        if isinstance(item, Covariate):
            out.append(item)
            continue
        get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
        columns = get("columns") or ()
        if isinstance(columns, str):
            columns = [part for part in columns.split(",")]
        out.append(Covariate(
            name=(get("name") or "").strip(), dataset_qid=(get("dataset_qid") or "").strip(),
            date_column=(get("date_column") or "").strip(),
            columns=tuple((column or "").strip() for column in columns),
            filters=normalize_filters(get("filters") or ()),
            aggregation=(get("aggregation") or "mean").strip(),
        ))
    return tuple(out)


def covariates_json(covariates: tuple[Covariate, ...]) -> str | None:
    """The JSON text stored for a benchmark's covariates; None when there are none."""
    if not covariates:
        return None
    return json.dumps([
        {"name": c.name, "dataset_qid": c.dataset_qid, "date_column": c.date_column, "columns": list(c.columns),
         "filters": [list(pair) for pair in c.filters], "aggregation": c.aggregation}
        for c in covariates
    ])


def materialized_from_row(row: dict) -> dict | None:
    """The manifest of the generated input and reference files of a finalized benchmark with covariates, or None."""
    stored = row.get("materialized")
    if isinstance(stored, str):
        stored = json.loads(stored) if stored else None
    return stored or None


def object_url(manifest: dict, kind: str = "input", *, versioned: bool = True) -> str:
    """The DOIP URL of a generated file (``input`` or ``reference``); with ``versioned`` it carries the commit of the
    benchmarks repository it was stored in."""
    entry = manifest[kind]
    url = f"{settings.doip_url.rstrip('/')}/doip/retrieve/{entry['qid']}/{entry['component']}"
    return f"{url}?version={manifest['commit']}" if versioned else url


def input_uri(manifest: dict, *, versioned: bool = True) -> str:
    """The DOIP URL a model run reads the generated input file from."""
    return object_url(manifest, "input", versioned=versioned)


def filters_from_row(row: dict) -> list[tuple[str, str]]:
    """The equality filters of a stored benchmark row as (column, value); ``filters`` is JSON text or a list."""
    stored = row.get("filters")
    if isinstance(stored, str):
        stored = json.loads(stored) if stored else []
    return [(column, value) for column, value in (stored or [])]


def normalize_filters(filters) -> tuple[tuple[str, str], ...]:
    """Filters as a tuple of (column, value) with trimmed columns, from pairs, {column, value} dicts or objects with
    ``column`` and ``value``."""
    pairs = []
    for item in filters or []:
        if isinstance(item, dict):
            column, value = item["column"], item["value"]
        elif hasattr(item, "column"):
            column, value = item.column, item.value
        else:
            column, value = item[0], item[1]
        pairs.append(((column or "").strip(), value))
    return tuple(pairs)


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
    for column, value in benchmark.filters:
        if not (column or "").strip() or len(column) > MAX_COLUMN:
            raise DefinitionError(f"The filter column must be 1 to {MAX_COLUMN} characters (filter column).")
        if not value or len(value) > MAX_FILTER_VALUE:
            raise DefinitionError(
                f"A filter value of 1 to {MAX_FILTER_VALUE} characters is required with a filter column (filter value).")
    columns = [column.strip().lower() for column, _ in benchmark.filters]
    if len(set(columns)) != len(columns):
        raise DefinitionError("A column can be filtered only once (filter column).")
    _validate_covariates(benchmark)
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


def _validate_covariates(benchmark: Benchmark) -> None:
    taken = {benchmark.date_column.strip().lower(), benchmark.target_column.strip().lower()}
    names = set()
    for number, c in enumerate(benchmark.covariates, start=1):
        where = f"covariate {number}"
        if not _NAME.fullmatch(c.name or ""):
            raise DefinitionError(
                f"The name of {where} must start with a letter and use only letters, digits and _ (at most 40).")
        if c.name.lower() in names:
            raise DefinitionError(f"The covariate name {c.name!r} is used twice.")
        names.add(c.name.lower())
        if not _QID.fullmatch(c.dataset_qid or ""):
            raise DefinitionError(f"Choose a dataset for covariate {c.name!r} (dataset id like Q123).")
        if not c.date_column or len(c.date_column) > MAX_COLUMN:
            raise DefinitionError(f"Covariate {c.name!r} needs a date column.")
        if not c.columns or any(not col or len(col) > MAX_COLUMN for col in c.columns):
            raise DefinitionError(f"Covariate {c.name!r} needs at least one column (each 1 to {MAX_COLUMN} characters).")
        if len({col.lower() for col in c.columns}) != len(c.columns):
            raise DefinitionError(f"Covariate {c.name!r} lists a column twice.")
        if c.aggregation not in AGGREGATIONS:
            raise DefinitionError(f"The aggregation of covariate {c.name!r} must be one of {', '.join(AGGREGATIONS)}.")
        for column, value in c.filters:
            if not (column or "").strip() or len(column) > MAX_COLUMN or not value or len(value) > MAX_FILTER_VALUE:
                raise DefinitionError(f"Every filter of covariate {c.name!r} needs a column and a value.")
        for output in c.output_columns():
            if output.lower() in taken:
                raise DefinitionError(f"The column {output!r} would exist twice in the input file.")
            taken.add(output.lower())


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
