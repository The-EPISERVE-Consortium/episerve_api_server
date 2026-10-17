"""MariaDB storage for benchmark definitions (database ``benchmarks``, table ``benchmarks``).

The database user has no DELETE right: benchmarks are retired by setting ``retired_at``.
A final benchmark (``finalized_at`` set) is immutable: updates re-check the state inside
one transaction (``SELECT … FOR UPDATE``) before changing anything.
"""

import json
from datetime import date

from app import db
from app.benchmarks.definition import Benchmark, filters_from_row
from app.benchmarks.errors import NotDraft


def _connect():
    return db.connect()


_SELECT = (
    "SELECT b.id, b.name, b.description, b.dataset_qid, b.target_column, b.date_column, b.filter_column, "
    "b.filter_value, b.test_start, b.test_end, b.history_start, b.history_end, b.input_filename, "
    "b.component_name, b.prediction_length, b.truth_points, b.data_commit, b.created_by, b.extra_filters, "
    "(SELECT u.name FROM users u WHERE u.id = b.created_by) AS created_by_name, "
    "CAST(b.created_at AS CHAR) AS created_at, CAST(b.finalized_at AS CHAR) AS finalized_at, "
    "CAST(b.retired_at AS CHAR) AS retired_at, "
    "(SELECT COUNT(*) FROM entries e WHERE e.benchmark_id = b.id AND e.removed_at IS NULL) AS entry_count "
    "FROM benchmarks b"
)


def state(row: dict) -> str:
    """``"retired"``, ``"final"`` or ``"draft"`` for a benchmark row."""
    if row.get("retired_at"):
        return "retired"
    return "final" if row.get("finalized_at") else "draft"


def is_public(row: dict) -> bool:
    """True once the benchmark was finalized (retired or not); anything never finalized is not public."""
    return bool(row.get("finalized_at"))


def benchmark_from_row(row: dict) -> Benchmark:
    """The definition stored in a benchmark row."""
    return Benchmark(
        dataset_qid=row["dataset_qid"], target_column=row["target_column"], date_column=row["date_column"],
        test_start=row["test_start"], test_end=row["test_end"], filter_column=row.get("filter_column"),
        filter_value=row.get("filter_value"), history_start=row.get("history_start"),
        history_end=row.get("history_end"), input_filename=row.get("input_filename") or "input.parquet",
        prediction_length=row.get("prediction_length"), data_commit=row.get("data_commit"),
        extra_filters=tuple(filters_from_row(row)[1:]),
    )


def _definition_params(name: str, description: str | None, b: Benchmark) -> tuple:
    return (name, description, b.dataset_qid, b.target_column, b.date_column, b.filter_column, b.filter_value,
            b.test_start, b.test_end, b.history_start, b.history_end, b.input_filename,
            json.dumps([list(pair) for pair in b.extra_filters]) if b.extra_filters else None)


def create_draft(name: str, description: str | None, benchmark: Benchmark, created_by: int | None = None) -> int:
    """Insert a draft benchmark (made by the user with id ``created_by``) and return its id."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO benchmarks (name, description, dataset_qid, target_column, date_column, "
                "filter_column, filter_value, test_start, test_end, history_start, history_end, input_filename, "
                "extra_filters, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (*_definition_params(name, description, benchmark), created_by),
            )
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()


def _lock_draft(cur, conn, benchmark_id: int) -> bool:
    """Lock the row; False if unknown; raise NotDraft if it is final or retired."""
    cur.execute("SELECT finalized_at, retired_at FROM benchmarks WHERE id = %s FOR UPDATE", (benchmark_id,))
    row = cur.fetchone()
    if row is None:
        return False
    if row["finalized_at"] or row["retired_at"]:
        conn.rollback()
        raise NotDraft(f"Benchmark {benchmark_id} is final or retired and cannot be changed.")
    return True


def update_draft(benchmark_id: int, name: str, description: str | None, benchmark: Benchmark) -> bool:
    """Replace the definition of a draft; False if the id is unknown.

    Raises:
        NotDraft: If the benchmark is final or retired.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if not _lock_draft(cur, conn, benchmark_id):
                return False
            cur.execute(
                "UPDATE benchmarks SET name = %s, description = %s, dataset_qid = %s, target_column = %s, "
                "date_column = %s, filter_column = %s, filter_value = %s, test_start = %s, test_end = %s, "
                "history_start = %s, history_end = %s, input_filename = %s, extra_filters = %s WHERE id = %s",
                (*_definition_params(name, description, benchmark), benchmark_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def finalize(
    benchmark_id: int, *, data_commit: str, component_name: str, truth_points: int,
    prediction_length: int, history_end: date,
) -> bool:
    """Freeze a draft and record the pin; False if the id is unknown.

    Raises:
        NotDraft: If the benchmark is already final or retired.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if not _lock_draft(cur, conn, benchmark_id):
                return False
            cur.execute(
                "UPDATE benchmarks SET data_commit = %s, component_name = %s, truth_points = %s, "
                "prediction_length = %s, history_end = %s, finalized_at = NOW() WHERE id = %s",
                (data_commit, component_name, truth_points, prediction_length, history_end, benchmark_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def set_description(benchmark_id: int, description: str | None) -> None:
    """Replace the description of a benchmark in any state (it is not part of the frozen definition)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE benchmarks SET description = %s WHERE id = %s", (description, benchmark_id))
        conn.commit()
    finally:
        conn.close()


def retire(benchmark_id: int) -> bool:
    """Hide a benchmark; True if it was not retired yet."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE benchmarks SET retired_at = NOW() WHERE id = %s AND retired_at IS NULL", (benchmark_id,))
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def _list(where: str) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"{_SELECT} WHERE {where} ORDER BY b.created_at DESC, b.id DESC")
            return list(cur.fetchall())
    finally:
        conn.close()


def list_final() -> list[dict]:
    """Final, not retired benchmarks (public), newest first, with active-entry counts."""
    return _list("b.finalized_at IS NOT NULL AND b.retired_at IS NULL")


def list_drafts() -> list[dict]:
    """Draft benchmarks, newest first."""
    return _list("b.finalized_at IS NULL AND b.retired_at IS NULL")


def get(benchmark_id: int) -> dict | None:
    """One benchmark row in any state, or None."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"{_SELECT} WHERE b.id = %s", (benchmark_id,))
            return cur.fetchone()
    finally:
        conn.close()
