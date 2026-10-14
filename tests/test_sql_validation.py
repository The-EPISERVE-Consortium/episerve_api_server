"""Regression tests for F1: transformation SQL must be parsed, never executed."""

import duckdb
import pytest

from app.sql_validation import validate_transformation_sql


def test_accepts_single_select():
    validate_transformation_sql("SELECT * FROM df WHERE a > 1")
    validate_transformation_sql("WITH t AS (SELECT 1 AS a) SELECT * FROM t")


def test_rejects_empty():
    for bad in ("", "   ", "\n"):
        with pytest.raises(ValueError):
            validate_transformation_sql(bad)


def test_rejects_multiple_statements_and_side_effects(tmp_path):
    marker = tmp_path / "pwned.txt"
    leak = tmp_path / "leak.txt"
    for bad in (
        "SELECT 1; DROP TABLE df",
        f"SELECT 1; COPY (SELECT 'x') TO '{marker}'",
        f"SELECT 1; COPY (SELECT content FROM read_text('/etc/hostname')) TO '{leak}'",
        "SELECT 1; SELECT 2",
    ):
        with pytest.raises(ValueError):
            validate_transformation_sql(bad)
    # Parsing must not execute any of the injected statements.
    assert not marker.exists()
    assert not leak.exists()


def test_rejects_non_select_statement():
    for bad in ("INSERT INTO df VALUES (1)", "ATTACH 'x.db'", "CALL foo()"):
        with pytest.raises(ValueError):
            validate_transformation_sql(bad)


def test_parser_errors_surface_as_duckdb_parser_exception():
    with pytest.raises(duckdb.ParserException):
        validate_transformation_sql("SELEC bad syntax")
