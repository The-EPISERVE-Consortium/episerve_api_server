"""Builds the files a benchmark with covariates hands to its models: one input file and one reference file.

The *target series* (a column of one dataset, narrowed by the benchmark's filters) sets the time grid. Every
covariate is aligned to that grid: the covariate rows between two grid dates are aggregated into the later grid date
(``mean``, ``sum`` or ``last``). Nothing is filled in; a grid date without covariate rows stays empty.

* **input**: the grid dates in the history window with the target value and all covariate columns
  (named ``<name>__<column>``). Covariate rows after the history end are never used, so nothing from the test
  window can reach a model.
* **reference**: the target series in the test window (date and target value); it never goes to a model.

The functions work on bytes and data frames only; reading from and writing to lakeFS is done by the caller.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app.benchmarks.definition import Benchmark, Covariate
from app.leaderboard.loaders import LoadError, read_series

MAX_MISSING_SHARE_WARNING = 0.5


@dataclass
class Built:
    """The generated tables and what the author should know about them."""

    input: pd.DataFrame
    reference: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    missing: dict[str, float] = field(default_factory=dict)      # share of empty values per covariate column

    @property
    def columns(self) -> list[str]:
        return list(self.input.columns)


def _read_covariate(data: bytes, covariate: Covariate, last_date: date) -> pd.DataFrame:
    """The rows of a covariate (date and its columns) up to ``last_date``, filters applied, sorted by date."""
    try:
        frame = pq.read_table(BytesIO(data)).to_pandas()
    except (pa.ArrowException, OSError) as exc:
        raise LoadError(f"covariate {covariate.name!r}: the dataset file could not be read") from exc
    lookup = {str(name).lower(): name for name in frame.columns}

    def column(name: str) -> str:
        if name.lower() not in lookup:
            raise LoadError(f"covariate {covariate.name!r}: the dataset has no column {name!r}")
        return lookup[name.lower()]

    mask = pd.Series(True, index=frame.index)
    for filter_column, value in covariate.filters:
        mask &= frame[column(filter_column)].astype(str) == value
    out = pd.DataFrame({"date": pd.to_datetime(frame[column(covariate.date_column)], errors="coerce")})
    for name in covariate.columns:
        out[name] = pd.to_numeric(frame[column(name)], errors="coerce")
    out = out[mask & out["date"].notna()]
    out["date"] = out["date"].dt.normalize()
    out = out[out["date"] <= pd.Timestamp(last_date)]
    if out.empty:
        raise LoadError(f"covariate {covariate.name!r}: no rows up to {last_date.isoformat()} after the filters")
    return out.sort_values("date").reset_index(drop=True)


def _align(grid: pd.DatetimeIndex, rows: pd.DataFrame, covariate: Covariate) -> pd.DataFrame:
    """Aggregate the covariate rows between two grid dates into the later one; returns one row per grid date."""
    step = pd.Series(grid).diff().dropna().median() if len(grid) > 1 else pd.Timedelta(days=1)
    lower_bound = grid[0] - step
    position = np.searchsorted(grid.values, rows["date"].values, side="left")   # grid[i-1] < date <= grid[i]
    keep = (position < len(grid)) & (rows["date"].values > lower_bound.to_datetime64())
    used = rows[keep].copy()
    used["slot"] = position[keep]
    how = covariate.aggregation
    grouped = used.groupby("slot")[list(covariate.columns)]
    if how == "mean":
        aggregated = grouped.mean()
    elif how == "sum":
        aggregated = grouped.sum(min_count=1)
    else:
        aggregated = grouped.last()                                              # last value that is not empty
    aggregated = aggregated.reindex(range(len(grid)))
    aggregated.columns = [f"{covariate.name}__{name}" for name in aggregated.columns]
    return aggregated.reset_index(drop=True)


def build(
    benchmark: Benchmark, history_end: date, target_data: bytes, covariate_data: dict[str, bytes],
) -> Built:
    """Build the input and the reference table of a benchmark.

    Args:
        benchmark: The definition (target series, filters, windows, covariates).
        history_end: Last date of the input window (before the test start).
        target_data: Parquet bytes of the target dataset.
        covariate_data: Parquet bytes of each covariate's dataset by covariate name.

    Raises:
        LoadError: A column is missing, the data is unreadable, a window is empty or the target series has several
            observations per date.
    """
    start = benchmark.history_start or date(1900, 1, 1)
    history = read_series(target_data, benchmark, start, history_end, require_unique=True,
                          empty_message="the dataset has no observations in the input range")
    reference = read_series(target_data, benchmark, benchmark.test_start, benchmark.test_end, require_unique=True,
                            empty_message="the dataset has no observations in the test window")
    grid = pd.DatetimeIndex(history["date"])
    table = pd.DataFrame({benchmark.date_column: history["date"].dt.date, benchmark.target_column: history["truth"]})
    warnings, missing = [], {}
    for covariate in benchmark.covariates:
        rows = _read_covariate(covariate_data[covariate.name], covariate, history_end)
        aligned = _align(grid, rows, covariate)
        if rows["date"].min() > grid[0]:
            warnings.append(f"covariate {covariate.name!r} starts on {rows['date'].min().date()}, "
                            f"after the first input date {grid[0].date()}")
        for name in aligned.columns:
            table[name] = aligned[name].to_numpy()
            share = float(table[name].isna().mean())
            missing[name] = round(share, 4)
            if share > MAX_MISSING_SHARE_WARNING:
                warnings.append(f"{name}: {share:.0%} of the values are empty")
    ref = pd.DataFrame({benchmark.date_column: reference["date"].dt.date, benchmark.target_column: reference["truth"]})
    return Built(input=table, reference=ref, warnings=warnings, missing=missing)


def to_parquet(frame: pd.DataFrame) -> tuple[bytes, str]:
    """The Parquet bytes of a table and their SHA-256."""
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), buffer)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()
