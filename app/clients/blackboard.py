import pymysql
import pymysql.cursors

from app.config import settings


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=settings.mariadb_host,
        user=settings.blackboard_user,
        password=settings.blackboard_password,
        database=settings.blackboard_db,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",  # server default connection charset is utf8mb3;
                            # result/trace routinely contain 4-byte characters
    )


def list_task_runs() -> list[dict]:
    """Return every row from the blackboard's task_runs table, newest first.

    Deliberately excludes the `trace` column's content -- it's a full
    self-contained HTML document that can run into the megabytes (a real
    trace.html has been seen at 1.4MB+), and NiceGUI ships table row data to
    the browser as a single websocket message. Embedding that in every row
    just to render a presence badge blew past the websocket's message-size
    limit. `has_trace` is computed in SQL instead; the actual content is
    fetched separately, on demand, via a plain HTTP GET (see get_trace_html
    and the /ui/blackboard/trace/{id} route) which has no such limit.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, task_type, kind, prompt, schedule_type, periodic_interval_minutes, "
                "status, result, (trace IS NOT NULL) AS has_trace, created_at, "
                "periodic_last_triggered_at, last_status_change "
                "FROM task_runs ORDER BY created_at DESC"
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def get_trace_html(row_id: int) -> str | None:
    """Return one row's trace.html content, or None if it has none."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT trace FROM task_runs WHERE id=%s", (row_id,))
            row = cur.fetchone()
            return row["trace"] if row else None
    finally:
        conn.close()


def insert_initial_task(prompt: str, schedule_type: str, periodic_interval_minutes: int | None = None) -> int:
    """Insert a new kind='initial' row -- a task not chained from any result.

    Everything else is left at its default: status starts 'new', so the
    orchestrator picks it up on its next poll; task_type/result/trace stay
    NULL since they're only meaningful for kind='result' rows.

    Args:
        prompt: The literal prompt to trigger.
        schedule_type: 'once' or 'periodic'.
        periodic_interval_minutes: Required when schedule_type='periodic',
            ignored otherwise.

    Returns:
        The new row's id.

    Raises:
        ValueError: If schedule_type isn't 'once'/'periodic', or if
            'periodic' is requested without a positive interval.
    """
    if schedule_type not in ("once", "periodic"):
        raise ValueError(f"schedule_type must be 'once' or 'periodic', got {schedule_type!r}")
    if schedule_type == "periodic" and not (periodic_interval_minutes and periodic_interval_minutes > 0):
        raise ValueError("periodic_interval_minutes must be a positive number for a periodic task")
    interval = periodic_interval_minutes if schedule_type == "periodic" else None

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_runs (kind, prompt, schedule_type, periodic_interval_minutes) "
                "VALUES ('initial', %s, %s, %s)",
                (prompt, schedule_type, interval),
            )
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()
