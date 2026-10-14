"""Validate dataset-transformation SQL without executing it."""

import duckdb
from duckdb import StatementType

_ALLOWED_STATEMENT_TYPES = {StatementType.SELECT}


def validate_transformation_sql(sql: str) -> None:
    """Validate a dataset-transformation query without running it.

    Parses the input with DuckDB's parser only. No connection is opened, no
    table is bound, and nothing is fetched from the filesystem or network, so
    attacker-supplied text cannot execute on the server. The transformation is
    expected to be a single read-only SELECT statement; anything else is
    rejected before it can reach an engine that would run it.

    Args:
        sql: SQL text entered in the UI transformation editor.

    Raises:
        duckdb.ParserException: If the text is not syntactically valid SQL.
        ValueError: If it is empty, multiple statements, or not a SELECT.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Enter a SQL statement.")
    statements = duckdb.extract_statements(sql)
    if not statements:
        raise ValueError("Enter a SQL statement.")
    if len(statements) > 1:
        raise ValueError("Use a single SQL statement.")
    if statements[0].type not in _ALLOWED_STATEMENT_TYPES:
        raise ValueError("Only a SELECT statement is allowed.")
