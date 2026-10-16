"""MariaDB storage for benchmarks and their scored entries (database ``benchmarks``).

The database user has no DELETE right: entries and benchmarks are retired by
setting ``removed_at`` / ``retired_at``.
"""

import json

import pymysql
import pymysql.cursors
import pymysql.err

from app.config import settings
from app.leaderboard.entries import Benchmark
from app.leaderboard.metrics import METRICS
from app.leaderboard.service import Entry


class DuplicateEntry(Exception):
    """An active entry for this run already exists on the benchmark."""


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=settings.mariadb_host,
        user=settings.benchmarks_user,
        password=settings.benchmarks_password,
        database=settings.benchmarks_db,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
    )


_BENCHMARK_SELECT = (
    "SELECT b.id, b.name, b.dataset_qid, b.target_column, b.date_column, b.filter_column, "
    "b.filter_value, b.test_start, b.test_end, b.truth_points, "
    "CAST(b.created_at AS CHAR) AS created_at, CAST(b.retired_at AS CHAR) AS retired_at, "
    "(SELECT COUNT(*) FROM entries e WHERE e.benchmark_id = b.id AND e.removed_at IS NULL) AS entry_count "
    "FROM benchmarks b"
)


def create_benchmark(name: str, benchmark: Benchmark, truth_points: int) -> int:
    """Insert a benchmark and return its id.

    Args:
        name: Display name.
        benchmark: The prediction task.
        truth_points: Number of observations in the test window at creation time.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO benchmarks (name, dataset_qid, target_column, date_column, filter_column, "
                "filter_value, test_start, test_end, truth_points) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (name, benchmark.dataset_qid, benchmark.target_column, benchmark.date_column,
                 benchmark.filter_column, benchmark.filter_value, benchmark.test_start,
                 benchmark.test_end, truth_points),
            )
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()


def list_benchmarks() -> list[dict]:
    """Active (not retired) benchmarks, newest first, with their active-entry counts."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_BENCHMARK_SELECT + " WHERE b.retired_at IS NULL ORDER BY b.created_at DESC, b.id DESC")
            return list(cur.fetchall())
    finally:
        conn.close()


def get_benchmark(benchmark_id: int) -> dict | None:
    """One benchmark by id (retired ones included), or None."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_BENCHMARK_SELECT + " WHERE b.id = %s", (benchmark_id,))
            return cur.fetchone()
    finally:
        conn.close()


def benchmark_from_row(row: dict) -> Benchmark:
    """The task definition stored in a benchmark row."""
    return Benchmark(
        dataset_qid=row["dataset_qid"], target_column=row["target_column"], date_column=row["date_column"],
        test_start=row["test_start"], test_end=row["test_end"],
        filter_column=row.get("filter_column"), filter_value=row.get("filter_value"),
    )


def retire_benchmark(benchmark_id: int) -> bool:
    """Hide a benchmark from the list; True if it was active."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE benchmarks SET retired_at = NOW() WHERE id = %s AND retired_at IS NULL", (benchmark_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


_RETRYABLE = (1213, 1205)  # deadlock, lock wait timeout


def add_entry(benchmark_id: int, entry: Entry) -> int:
    """Store a scored entry and return its id.

    The duplicate check and the insert run in one transaction (``SELECT … FOR UPDATE``).
    Concurrent adds can make MariaDB abort one transaction (deadlock 1213, lock wait
    timeout 1205); the whole transaction is then retried once on a fresh connection.

    Raises:
        DuplicateEntry: If an active entry for the same run exists.
        pymysql.err.OperationalError: On a second deadlock/timeout or any other database error.
    """
    try:
        return _add_entry_once(benchmark_id, entry)
    except pymysql.err.OperationalError as exc:
        if not exc.args or exc.args[0] not in _RETRYABLE:
            raise
    return _add_entry_once(benchmark_id, entry)


def _add_entry_once(benchmark_id: int, entry: Entry) -> int:
    conn = _connect()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM entries WHERE benchmark_id = %s AND run_qid = %s AND removed_at IS NULL "
                    "FOR UPDATE",
                    (benchmark_id, entry.run_qid),
                )
                if cur.fetchone():
                    conn.rollback()
                    raise DuplicateEntry(f"Run {entry.run_qid} is already on this leaderboard.")
                cur.execute(
                    "INSERT INTO entries (benchmark_id, run_qid, model_name, docker_tag, run_timestamp, params, "
                    "history_start, history_end, n_scored, n_expected, mae, rmse, coverage, interval_score, points, "
                    "truth_as_of, truth_commit) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (benchmark_id, entry.run_qid, entry.model_name, entry.docker_tag, entry.run_timestamp,
                     json.dumps(entry.params, allow_nan=False), entry.history_start, entry.history_end,
                     entry.n_scored, entry.n_expected,
                     entry.metrics.get("mae"), entry.metrics.get("rmse"), entry.metrics.get("coverage"),
                     entry.metrics.get("interval_score"), json.dumps(entry.points, allow_nan=False),
                     entry.truth_as_of, entry.truth_commit),
                )
                entry_id = cur.lastrowid
            conn.commit()
            return entry_id
        except pymysql.err.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    finally:
        conn.close()


def _entry_from_row(row: dict) -> Entry:
    return Entry(
        run_qid=row["run_qid"], model_name=row["model_name"], docker_tag=row["docker_tag"],
        params=json.loads(row["params"]), history_start=row["history_start"], history_end=row["history_end"],
        run_timestamp=row["run_timestamp"], n_scored=row["n_scored"], n_expected=row["n_expected"],
        metrics={m: (None if row[m] is None else float(row[m])) for m in METRICS},
        points=json.loads(row["points"]), entry_id=row["id"], truth_commit=row["truth_commit"],
        truth_as_of=row["truth_as_of"], scored_at=str(row["scored_at"]),
    )


def list_entries(benchmark_id: int) -> list[Entry]:
    """Active entries of a benchmark, oldest first."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, run_qid, model_name, docker_tag, run_timestamp, params, history_start, history_end, "
                "n_scored, n_expected, mae, rmse, coverage, interval_score, points, truth_as_of, truth_commit, "
                "CAST(scored_at AS CHAR) AS scored_at FROM entries "
                "WHERE benchmark_id = %s AND removed_at IS NULL ORDER BY id",
                (benchmark_id,),
            )
            return [_entry_from_row(row) for row in cur.fetchall()]
    finally:
        conn.close()


def remove_entry(benchmark_id: int, entry_id: int) -> bool:
    """Soft-delete an entry; True if an active entry was removed."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE entries SET removed_at = NOW() WHERE id = %s AND benchmark_id = %s AND removed_at IS NULL",
                (entry_id, benchmark_id),
            )
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()
