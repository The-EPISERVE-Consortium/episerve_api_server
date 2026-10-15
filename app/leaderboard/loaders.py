"""Load predictions, observations and run parameters for the leaderboard."""

import json
from io import BytesIO

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from app.dataset_preview import (
    MAX_DOWNLOAD_BYTES, _column_sql, download_preview_bytes, resolve_component,
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
    window = (out["date"] >= pd.Timestamp(benchmark.test_start)) & (out["date"] <= pd.Timestamp(benchmark.test_end))
    clipped = out[window].drop_duplicates("date", keep="last")
    return clipped.reset_index(drop=True)


def load_truth(dataset: dict, benchmark: Benchmark, doip_base: str) -> pd.DataFrame:
    """Read the observed series for the test window from the CKAN dataset.

    Args:
        dataset: Catalog record from ``ckan_client.list_processed_datasets()``.
        benchmark: Series and window to read.
        doip_base: Trusted DOIP service base URL.

    Returns:
        Columns ``date`` and ``truth`` sorted by date.

    Raises:
        LoadError: If a column is missing, the data is too large, or the window is empty.
        ValueError: If the dataset has no component or the URL is not trusted.
    """
    component = resolve_component([dataset], dataset.get("qid", ""))
    data = download_preview_bytes(component["url"], doip_base, MAX_DOWNLOAD_BYTES)
    table = pq.read_table(BytesIO(data))
    names = table.column_names
    needed = [benchmark.date_column, benchmark.target_column]
    if benchmark.filter_column:
        needed.append(benchmark.filter_column)
    for name in needed:
        if name not in names:
            raise LoadError(f"dataset has no column {name!r}")
    date_sql = _column_sql(benchmark.date_column, names)
    target_sql = _column_sql(benchmark.target_column, names)
    where = f"CAST({date_sql} AS DATE) BETWEEN ? AND ?"
    values: list = [benchmark.test_start, benchmark.test_end]
    if benchmark.filter_column:
        where += f" AND CAST({_column_sql(benchmark.filter_column, names)} AS VARCHAR) = ?"
        values.append(benchmark.filter_value or "")
    with duckdb.connect(config={
        "enable_external_access": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "memory_limit": "256MB",
        "threads": "1",
    }) as connection:
        connection.register("source", table)
        truth = connection.execute(
            f"SELECT CAST({date_sql} AS DATE) AS date, CAST({target_sql} AS DOUBLE) AS truth "
            f"FROM source WHERE {where} AND {target_sql} IS NOT NULL ORDER BY 1",
            values,
        ).df()
    if truth.empty:
        raise LoadError("the dataset has no observations in the test window")
    truth["date"] = pd.to_datetime(truth["date"]).dt.normalize()
    return truth


def fetch_run_params(qid: str, doip_base: str) -> dict:
    """Fetch a run's ``config.json``; an empty dict if it cannot be read."""
    url = f"{doip_base.rstrip('/')}/doip/retrieve/{qid}/input/config.json"
    try:
        parsed = json.loads(download_preview_bytes(url, doip_base, MAX_CONFIG_BYTES))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
