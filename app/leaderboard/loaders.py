"""Load predictions, observations and run parameters for the leaderboard."""

import json
from datetime import date
from io import BytesIO

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from lakefs.exceptions import NotFoundException

from app.clients import lakefs as lakefs_client
from app.dataset_preview import (
    MAX_DOWNLOAD_BYTES, _column_sql, download_preview_bytes,
)
from app.leaderboard.entries import Benchmark

MAX_CONFIG_BYTES = 64 * 1024


class LoadError(ValueError):
    """Raised when predictions or observations cannot be used for scoring."""


def load_predictions(records: list[dict], benchmark: Benchmark) -> pd.DataFrame:
    """Turn DataStore rows (all text) into a typed forecast table.

    Args:
        records: Rows of the run's ``predictions.tsv`` DataStore resource.
        benchmark: Supplies the date and target column names and the test window.

    Returns:
        Columns ``date``, ``forecast`` and, when present, ``q10``/``q90``;
        rows limited to the test window.

    Raises:
        LoadError: If a required column is missing or a date is unparseable.
    """
    frame = pd.DataFrame(records)
    for name in (benchmark.date_column, benchmark.target_column):
        if name not in frame.columns:
            raise LoadError(f"predictions have no column {name!r}")
    dates = pd.to_datetime(frame[benchmark.date_column], errors="coerce")
    if dates.isna().any():
        raise LoadError(f"predictions contain an unparseable date in {benchmark.date_column!r}")
    out = pd.DataFrame({
        "date": dates.dt.normalize(),
        "forecast": pd.to_numeric(frame[benchmark.target_column], errors="coerce"),
    })
    for key, suffix in (("q10", "_q10"), ("q90", "_q90")):
        column = benchmark.target_column + suffix
        if column in frame.columns:
            out[key] = pd.to_numeric(frame[column], errors="coerce")
    for key in [name for name in ("forecast", "q10", "q90") if name in out.columns]:
        out[key] = out[key].where(np.isfinite(out[key]))
    window = (out["date"] >= pd.Timestamp(benchmark.test_start)) & (out["date"] <= pd.Timestamp(benchmark.test_end))
    clipped = out[window].drop_duplicates("date", keep="last")
    return clipped.reset_index(drop=True)


def _read_series(
    data: bytes, benchmark: Benchmark, start: date, end: date, *,
    require_unique: bool, empty_message: str,
) -> pd.DataFrame:
    """Read ``date``/``truth`` rows between ``start`` and ``end`` (inclusive), sorted by date."""
    if any(value is None for _, value in benchmark.filters):
        raise LoadError("filter value is required with a filter column")
    needed = [benchmark.date_column, benchmark.target_column]
    needed += [column for column, _ in benchmark.filters]
    try:
        names = pq.read_schema(BytesIO(data)).names
        for name in needed:
            if name not in names:
                raise LoadError(f"dataset has no column {name!r}")
        table = pq.read_table(BytesIO(data), columns=needed)
    except (pa.ArrowException, duckdb.Error) as exc:
        raise LoadError("the dataset file could not be read") from exc
    date_sql = _column_sql(benchmark.date_column, names)
    target_sql = _column_sql(benchmark.target_column, names)
    where = f"TRY_CAST({date_sql} AS DATE) BETWEEN ? AND ?"
    values: list = [start, end]
    for column, value in benchmark.filters:
        where += f" AND CAST({_column_sql(column, names)} AS VARCHAR) = ?"
        values.append(value)
    try:
        with duckdb.connect(config={
            "enable_external_access": "false",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "memory_limit": "256MB",
            "threads": "1",
        }) as connection:
            connection.register("source", table)
            truth = connection.execute(
                f"SELECT TRY_CAST({date_sql} AS DATE) AS date, TRY_CAST({target_sql} AS DOUBLE) AS truth "
                f"FROM source WHERE {where} AND TRY_CAST({target_sql} AS DOUBLE) IS NOT NULL "
                f"AND isfinite(TRY_CAST({target_sql} AS DOUBLE)) ORDER BY 1",
                values,
            ).df()
    except (duckdb.Error, pa.ArrowException) as exc:
        raise LoadError("the dataset could not be queried") from exc
    if truth.empty:
        raise LoadError(empty_message)
    if require_unique and truth["date"].duplicated().any():
        raise LoadError(
            "the dataset has several observations per date in the test window; "
            "add a filter to select one series"
        )
    truth["date"] = pd.to_datetime(truth["date"]).dt.normalize()
    return truth


def parse_truth(data: bytes, benchmark: Benchmark) -> pd.DataFrame:
    """Read the observed series for the test window from a dataset Parquet file.

    Args:
        data: Parquet bytes of the dataset component.
        benchmark: Series and window to read.

    Returns:
        Columns ``date`` and ``truth`` sorted by date.

    Raises:
        LoadError: If a column is missing, the data is unreadable, the window is
            empty, or several observations share a date.
    """
    return _read_series(
        data, benchmark, benchmark.test_start, benchmark.test_end,
        require_unique=True, empty_message="the dataset has no observations in the test window",
    )


def last_observed_date(data: bytes, benchmark: Benchmark, on_or_before: date) -> date:
    """Latest date with a usable target value on or before ``on_or_before``.

    Args:
        data: Parquet bytes of the dataset component.
        benchmark: Supplies the series (date, target and filter columns).
        on_or_before: Inclusive upper bound.

    Returns:
        The latest observed date.

    Raises:
        LoadError: If a column is missing, the data is unreadable, or nothing was observed.
    """
    series = _read_series(
        data, benchmark, date(1900, 1, 1), on_or_before, require_unique=False,
        empty_message=f"the dataset has no observations on or before {on_or_before.isoformat()}",
    )
    return series["date"].max().date()


def truth_from_pin(qid: str, component_name: str, commit: str, benchmark: Benchmark) -> pd.DataFrame:
    """Read the observed series from the component a benchmark pinned.

    Args:
        qid: Dataset QID.
        component_name: File name of the pinned component.
        commit: lakeFS commit id of the processed-data repository.
        benchmark: Series and window to read.

    Returns:
        Columns ``date`` and ``truth`` sorted by date.

    Raises:
        LoadError: As ``parse_truth``, and when an argument is unsafe, the file exceeds the
            size limit or does not exist at the commit.
    """
    try:
        data = lakefs_client.read_processed_component(commit, qid, component_name, MAX_DOWNLOAD_BYTES)
    except NotFoundException as exc:
        raise LoadError("The dataset file does not exist at the pinned commit.") from exc
    except ValueError as exc:
        raise LoadError(str(exc)) from exc
    return parse_truth(data, benchmark)


def fetch_run_params(qid: str, doip_base: str) -> dict:
    """Fetch a run's ``config.json``; an empty dict if it cannot be read."""
    url = f"{doip_base.rstrip('/')}/doip/retrieve/{qid}/input/config.json"
    try:
        parsed = json.loads(
            download_preview_bytes(url, doip_base, MAX_CONFIG_BYTES), parse_constant=lambda _: None
        )
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
