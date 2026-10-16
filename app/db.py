"""The shared MariaDB connection for the ``benchmarks`` database."""

import pymysql
import pymysql.cursors

from app.config import settings


def connect() -> pymysql.connections.Connection:
    """Open a connection with the benchmarks credentials (DictCursor, utf8mb4)."""
    return pymysql.connect(
        host=settings.mariadb_host,
        user=settings.benchmarks_user,
        password=settings.benchmarks_password,
        database=settings.benchmarks_db,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
    )
