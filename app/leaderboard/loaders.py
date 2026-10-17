"""Load predictions, observations and run parameters for the leaderboard."""

import hashlib
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


def read_series(
    data: bytes, benchmark: Benchmark, start: date, end: date, *, require_unique: bool, empty_message: str,
) -> pd.DataFrame:
    """The target series (columns ``date`` and ``truth``) between two dates, with the benchmark's filters applied."""
    return _read_series(data, benchmark, start, end, require_unique=require_unique, empty_message=empty_message)


def truth_from_reference(manifest: dict, benchmark: Benchmark) -> pd.DataFrame:
    """Read the observed series from the reference file a benchmark with covariates generated.

    The file is read at the commit recorded in the manifest and checked against its stored hash.

    Returns:
        Columns ``date`` and ``truth`` sorted by date.

    Raises:
        LoadError: If the file is missing, changed (hash differs), unreadable or empty.
    """
    reference = manifest["reference"]
    try:
        data = lakefs_client.read_benchmark_file(manifest["commit"], reference["path"], MAX_DOWNLOAD_BYTES)
    except NotFoundException as exc:
        raise LoadError("The benchmark's reference file does not exist.") from exc
    except ValueError as exc:
        raise LoadError(str(exc)) from exc
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise LoadError("The benchmark's reference file does not match its recorded hash.")
    try:
        frame = pq.read_table(BytesIO(data)).to_pandas()
        out = pd.DataFrame({
            "date": pd.to_datetime(frame[benchmark.date_column]).dt.normalize(),
            "truth": pd.to_numeric(frame[benchmark.target_column], errors="coerce"),
        })
    except (pa.ArrowException, KeyError, ValueError) as exc:
        raise LoadError("The benchmark's reference file could not be read.") from exc
    out = out[np.isfinite(out["truth"])].sort_values("date").reset_index(drop=True)
    if out.empty:
        raise LoadError("The benchmark's reference file has no observations.")
    return out


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
