"""Load predictions, observations and run parameters for the leaderboard."""

import hashlib
import json
import re
from datetime import date
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from lakefs.exceptions import NotFoundException

from app.clients import lakefs as lakefs_client
from app.dataset_preview import (
    MAX_DOWNLOAD_BYTES, _column_sql, download_preview_bytes, validate_component_url,
)
from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry
from app.sql_validation import validate_transformation_sql

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
    """Read the observed series from the benchmark's generated reference file.

    The file is read at the commit recorded in the manifest and checked against its stored hash.

    Returns:
        Columns ``date`` and ``truth`` sorted by date.

    Raises:
        LoadError: If the file is missing, changed (hash differs), unreadable or empty.
    """
    reference = manifest["reference"]
    try:
        data = lakefs_client.read_benchmark_component(
            manifest["commit"], reference["qid"], reference["component"], MAX_DOWNLOAD_BYTES)
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


def input_history(
    manifest: dict, benchmark: Benchmark, start: date | None, end: date, *, sql: str | None = None,
) -> list[dict]:
    """Read target history from the exact frozen input supplied to a model run.

    Args:
        manifest: Generated benchmark files with immutable commit and checksum.
        benchmark: Supplies the generated date and target column names.
        start: First input date recorded for the run, or an unbounded start.
        end: Last input date recorded for the run.
        sql: Saved run transformation to apply before selecting history.

    Returns:
        Sorted date and value pairs, retaining gaps for missing target values.

    Raises:
        LoadError: The frozen input is missing, altered, invalid, or empty.
    """
    info = manifest["input"]
    try:
        data = lakefs_client.read_benchmark_component(
            manifest["commit"], info["qid"], info["component"], MAX_DOWNLOAD_BYTES)
    except (NotFoundException, ValueError) as exc:
        raise LoadError("The benchmark's frozen input file could not be read.") from exc
    if hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise LoadError("The benchmark's input file does not match its recorded hash.")
    try:
        frame = _replay_input(data, sql)
    except (pa.ArrowException, KeyError, ValueError, TypeError) as exc:
        raise LoadError("The benchmark's frozen input series could not be read.") from exc
    return _history_points(frame, benchmark, start, end)


def _history_points(frame: pd.DataFrame, benchmark: Benchmark, start: date | None, end: date) -> list[dict]:
    """Normalize target history without leaking reference dates into the input.

    Args:
        frame: Generated input or the replayed legacy input transformation.
        benchmark: Supplies date, target, and reference boundary columns.
        start: First recorded input date, or an unbounded start.
        end: Last recorded input date.

    Returns:
        Sorted date and value pairs, retaining missing target values as gaps.

    Raises:
        LoadError: Input dates or columns are invalid, ambiguous, or empty.
    """
    try:
        dates = pd.to_datetime(frame[benchmark.date_column], errors="coerce").dt.normalize()
        values = pd.to_numeric(frame[benchmark.target_column], errors="coerce")
    except (KeyError, ValueError, TypeError) as exc:
        raise LoadError("The recorded input series could not be read.") from exc
    if dates.isna().any() or dates.duplicated().any():
        raise LoadError("The benchmark's frozen input contains invalid or duplicate dates.")
    selected = (dates <= pd.Timestamp(end)) & (dates < pd.Timestamp(benchmark.test_start))
    if start is not None:
        selected &= dates >= pd.Timestamp(start)
    out = pd.DataFrame({"date": dates[selected], "value": values[selected]}).sort_values("date")
    if out.empty:
        raise LoadError("The benchmark's frozen input has no history for this run.")
    return [{"date": row.date.date().isoformat(), "value": float(row.value) if pd.notna(row.value) and np.isfinite(row.value) else None}
            for row in out.itertuples(index=False)]



def legacy_input_history(entry: Entry, benchmark: Benchmark, component: str, doip_base: str) -> list[dict]:
    """Replay a legacy run's saved input transformation at its recorded source pin.

    Args:
        entry: Stored run scored before generated benchmark inputs were introduced.
        benchmark: Expected dataset, date column, target, and reference boundaries.
        component: Expected processed-data component from the stored benchmark.
        doip_base: Trusted service hosting the run's saved configuration.

    Returns:
        Target history from the original versioned and transformed model input.

    Raises:
        LoadError: Saved configuration is unavailable, does not match the recorded
            source pin, or cannot be replayed as a single read-only transformation.
    """
    sql = _saved_input_query(entry, benchmark.dataset_qid, component, doip_base)
    try:
        data = lakefs_client.read_processed_component(entry.truth_commit, benchmark.dataset_qid, component, MAX_DOWNLOAD_BYTES)
        frame = _replay_input(data, sql)
    except Exception as exc:
        raise LoadError("The run's recorded input transformation could not be replayed.") from exc
    return _history_points(frame, benchmark, entry.history_start, entry.history_end)


def run_input_history(manifest: dict, benchmark: Benchmark, entry: Entry, doip_base: str) -> list[dict]:
    """Replay a generated-input run's saved transformation on its verified file.

    Args:
        manifest: Frozen generated input metadata, including its checksum.
        benchmark: Date and target columns and reference boundaries.
        entry: Scored run with its source pin and history boundaries.
        doip_base: Trusted service hosting the saved run configuration.

    Returns:
        Sorted history points from the transformed model input.

    Raises:
        LoadError: The saved input pin, query, or frozen file cannot be verified.
    """
    info = manifest["input"]
    sql = _saved_input_query(entry, info["qid"], info["component"], doip_base)
    return input_history(manifest, benchmark, entry.history_start, entry.history_end, sql=sql)


def _saved_input_query(entry: Entry, qid: str, component: str, doip_base: str) -> str | None:
    """Read a single-input run's query after verifying its saved source pin.

    Args:
        entry: Scored run identifying the saved configuration and source version.
        qid: Expected source object identifier.
        component: Expected source component name.
        doip_base: Trusted service hosting the saved configuration and source.

    Returns:
        Saved SQL, or None when the run used the file without a transformation.

    Raises:
        LoadError: Configuration is unavailable, malformed, or uses another source.
    """
    if not re.fullmatch(r"Q[0-9]+", entry.run_qid):
        raise LoadError("The recorded run identifier is invalid.")
    try:
        raw = download_preview_bytes(
            f"{doip_base.rstrip('/')}/doip/retrieve/{entry.run_qid}/input/config_prefect.json",
            doip_base, MAX_CONFIG_BYTES,
        )
        config = json.loads(raw)
    except Exception as exc:
        raise LoadError("The run's saved input configuration could not be read.") from exc
    if not isinstance(config, dict):
        raise LoadError("The run's saved input configuration is invalid.")
    files, queries = config.get("input_data_files"), config.get("data_transformation_sql", [])
    if queries is None:
        queries = []
    if (not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], list) or len(files[0]) != 2
            or not isinstance(queries, list) or len(queries) > 1):
        raise LoadError("The run needs one recorded source and at most one input transformation.")
    try:
        source = urlsplit(validate_component_url(files[0][0], doip_base))
        expected_path = f"{urlsplit(doip_base).path.rstrip('/')}/doip/retrieve/{qid}/{component}"
        if source.path != expected_path or parse_qs(source.query) != {"version": [entry.truth_commit]}:
            raise LoadError("The saved input source does not match the run's recorded dataset and data version.")
        sql = queries[0] if queries else None
        if sql is not None and not isinstance(sql, str):
            raise LoadError("The run's recorded input transformation is invalid.")
        return sql if sql and sql.strip() else None
    except LoadError:
        raise
    except Exception as exc:
        raise LoadError("The run's saved input source is invalid.") from exc


def _replay_input(data: bytes, sql: str | None) -> pd.DataFrame:
    """Apply a saved read-only query in an engine with external access disabled.

    Args:
        data: Original pinned Parquet input bytes.
        sql: Saved transformation, or None for an unchanged input file.

    Returns:
        Input rows and columns after the saved transformation.

    Raises:
        LoadError: The input or query is invalid or cannot be safely replayed.
    """
    try:
        table = pq.read_table(BytesIO(data))
        if sql is None:
            return table.to_pandas()
        validate_transformation_sql(sql)
        with duckdb.connect(config={
            "enable_external_access": "false", "allow_unsigned_extensions": "false",
            "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
            "memory_limit": "256MB", "threads": "1", "temp_directory": "",
        }) as connection:
            connection.register("df", table)
            connection.execute("SET lock_configuration = true")
            return connection.execute(sql).df()
    except Exception as exc:
        raise LoadError("The run's recorded input transformation could not be replayed.") from exc
