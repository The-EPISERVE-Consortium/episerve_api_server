"""Tests for the benchmark definition: validation, generated input SQL, horizon."""

from datetime import date, timedelta

import pytest

from app.benchmarks.definition import Benchmark, DefinitionError, derive_horizon, input_sql, validate
from app.leaderboard.entries import HistoryWindow, has_equality_filter, parse_history_window


def _b(**overrides):
    base = dict(
        dataset_qid="Q9493814122238", target_column="viruslast_normalisiert", date_column="datum",
        test_start=date(2026, 4, 1), test_end=date(2026, 9, 30),
        filter_column="typ", filter_value="Influenza A+B", history_end=date(2026, 3, 25),
    )
    return Benchmark(**{**base, **overrides})


def test_input_sql_with_filter_and_history_start():
    assert input_sql(_b(history_start=date(2024, 1, 3))) == (
        'SELECT CAST("datum" AS DATE) AS "datum", "viruslast_normalisiert" FROM df '
        "WHERE \"typ\" = 'Influenza A+B' AND CAST(\"datum\" AS DATE) >= DATE '2024-01-03' "
        "AND CAST(\"datum\" AS DATE) <= DATE '2026-03-25' ORDER BY \"datum\""
    )


def test_input_sql_without_filter_and_start():
    assert input_sql(_b(filter_column=None, filter_value=None)) == (
        'SELECT CAST("datum" AS DATE) AS "datum", "viruslast_normalisiert" FROM df '
        "WHERE CAST(\"datum\" AS DATE) <= DATE '2026-03-25' ORDER BY \"datum\""
    )


def test_input_sql_escapes_quotes_in_identifiers_and_values():
    sql = input_sql(_b(target_column='a"b', filter_value="O'Brien"))
    assert '"a""b"' in sql and "'O''Brien'" in sql


def test_generated_sql_is_accepted_by_the_qualification_rules():
    sql = input_sql(_b(history_start=date(2024, 1, 3)))
    assert parse_history_window(sql, "datum") == HistoryWindow(date(2024, 1, 3), date(2026, 3, 25))
    assert has_equality_filter(sql, "typ", "Influenza A+B")


@pytest.mark.parametrize("name,overrides,message", [
    ("  ", {}, "name"),
    ("ok", {"dataset_qid": "bad"}, "dataset"),
    ("ok", {"filter_column": None, "filter_value": "x"}, "needs a filter column"),
    ("ok", {"filter_column": "typ", "filter_value": None}, "filter value"),
    ("ok", {"input_filename": "../x.parquet"}, "file name"),
    ("ok", {"test_start": date(2026, 10, 1)}, "test start"),
    ("ok", {"history_end": date(2026, 4, 1)}, "history must end before"),
])
def test_validate_rejects(name, overrides, message):
    with pytest.raises(DefinitionError, match=message):
        validate(_b(**overrides), name)


def _weekly(start, n):
    return [start + timedelta(days=7 * i) for i in range(n)]


def test_regular_weekly_horizon():
    h = derive_horizon(_weekly(date(2026, 4, 1), 27), last_history=date(2026, 3, 25))
    assert (h.prediction_length, h.frequency_days, h.regular) == (27, 7, True)


def test_gap_between_history_and_window_is_covered():
    h = derive_horizon(_weekly(date(2026, 4, 15), 25), last_history=date(2026, 3, 25))
    assert h.prediction_length == 27


def test_irregular_spacing_is_flagged():
    h = derive_horizon([date(2026, 4, 1), date(2026, 4, 8), date(2026, 4, 22)], date(2026, 3, 25))
    assert (h.prediction_length, h.frequency_days, h.regular) == (4, 7, False)


@pytest.mark.parametrize("observed,last", [([], date(2026, 3, 25)), ([date(2026, 4, 1)], date(2026, 4, 1)), ([date(2026, 4, 1), date(2026, 4, 8)], date(2026, 4, 5))])
def test_horizon_rejects_empty_or_overlapping(observed, last):
    with pytest.raises(DefinitionError):
        derive_horizon(observed, last)
