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
                            # finding/trace routinely contain 4-byte characters
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
                "SELECT id, task_type, post_type, prompt, periodic_interval_minutes, "
                "state, finding, (trace IS NOT NULL) AS has_trace, created_at, "
                "periodic_last_triggered_at, last_state_change "
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


def insert_initial_task(
    prompt: str,
    periodic_interval_minutes: int | None = None,
    task_type: str | None = None,
) -> int:
    """Insert a new post_type='run_me' row -- a task not chained from any finding.

    Everything else is left at its default: state starts 'waiting', so the
    orchestrator picks it up on its next poll; finding/trace stay NULL since
    they're only meaningful for post_type='someone_take_over' rows. task_type
    is optional here -- unlike a post_type='someone_take_over' row, it's
    never matched against routing.py (the row already carries its own
    `prompt`), it's purely a free-text label for a human reading the table.

    Args:
        prompt: The literal prompt to trigger.
        periodic_interval_minutes: If set, the row recurs every this many
            minutes; if None, it fires once.
        task_type: Optional free-text label.

    Returns:
        The new row's id.

    Raises:
        ValueError: If periodic_interval_minutes is given but not positive.
    """
    if periodic_interval_minutes is not None and periodic_interval_minutes <= 0:
        raise ValueError("periodic_interval_minutes must be a positive number when given")

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_runs (post_type, prompt, periodic_interval_minutes, task_type) "
                "VALUES ('run_me', %s, %s, %s)",
                (prompt, periodic_interval_minutes, task_type or None),
            )
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()


_MANUALLY_SETTABLE_STATES = {"waiting", "dismissed"}


def set_state(row_id: int, state: str) -> None:
    """Manually set a row's state from the UI.

    Only 'waiting' (re-queue -- the orchestrator picks it up on its next
    poll) and 'dismissed' (permanently excluded from the orchestrator's
    eligibility query, e.g. to retire a periodic row or dismiss a finding
    nothing routes) are allowed here -- 'dispatching_run'/
    'waiting_for_next_periodic_run'/'resolved' are the orchestrator's own
    claim-lifecycle states and are never set by a human.

    Args:
        row_id: `task_runs.id` to update.
        state: 'waiting' or 'dismissed'.

    Raises:
        ValueError: If `state` isn't one of the manually-settable values.
    """
    if state not in _MANUALLY_SETTABLE_STATES:
        raise ValueError(f"state must be one of {_MANUALLY_SETTABLE_STATES}, got {state!r}")

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE task_runs SET state=%s WHERE id=%s", (state, row_id))
            conn.commit()
    finally:
        conn.close()
