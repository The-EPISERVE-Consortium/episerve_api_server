"""Shared fake database connection for store tests (no database)."""


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rowcount, self.lastrowid, self._rows = conn, 0, None, []

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        result = self.conn.results.pop(0) if self.conn.results else ([], 0, None)
        if isinstance(result, Exception):
            raise result
        rows, rowcount, lastrowid = result
        self._rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, results=()):
        self.results, self.executed = list(results), []
        self.commits, self.rolled, self.closed = 0, False, False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rolled = True

    def close(self):
        self.closed = True
