"""MariaDB storage for the scored leaderboard entries (database ``benchmarks``).

The database user has no DELETE right: entries are removed by setting ``removed_at``.
"""

import json

import pymysql
import pymysql.err

from app import db
from app.leaderboard.metrics import METRICS
from app.leaderboard.service import Entry


class DuplicateEntry(Exception):
    """An active entry for this run already exists on the benchmark."""


def _connect() -> pymysql.connections.Connection:
    return db.connect()


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


def list_scores(benchmark_ids: list[int]) -> dict[int, list[Entry]]:
    """Active entries per benchmark with their metrics only (no per-date points), for summaries."""
    if not benchmark_ids:
        return {}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT benchmark_id, model_name, mae, rmse, coverage, interval_score FROM entries "
                f"WHERE removed_at IS NULL AND benchmark_id IN ({', '.join(['%s'] * len(benchmark_ids))})",
                tuple(benchmark_ids),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    result: dict[int, list[Entry]] = {}
    for row in rows:
        result.setdefault(row["benchmark_id"], []).append(Entry(
            run_qid="", model_name=row["model_name"], docker_tag="", params={}, history_start=None,
            history_end=None, run_timestamp="", n_scored=0, n_expected=0,
            metrics={m: (None if row[m] is None else float(row[m])) for m in METRICS},
        ))
    return result


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
