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
    """Return every row from the blackboard's task_runs table, newest first."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, task_type, status, result, trace, created_at, "
                "claimed_by, claimed_at FROM task_runs ORDER BY created_at DESC"
            )
            return list(cur.fetchall())
    finally:
        conn.close()
