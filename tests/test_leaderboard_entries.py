"""Tests for history-window parsing and run qualification."""

from datetime import date

from app.leaderboard.entries import (
    Benchmark, HistoryWindow, has_equality_filter, parse_history_window, qualify_run,
)

SQL = (
    "SELECT CAST(datum AS DATE) AS datum, viruslast_normalisiert FROM df "
    "WHERE typ = 'Influenza A+B' AND CAST(datum AS DATE) >= DATE '2024-01-03' "
    "AND CAST(datum AS DATE) <= DATE '2026-03-25' ORDER BY datum"
)


def _benchmark(**overrides):
    base = dict(
        dataset_qid="Q9493814122238", target_column="viruslast_normalisiert",
        date_column="datum", test_start=date(2026, 4, 1), test_end=date(2026, 9, 30),
    )
    return Benchmark(**{**base, **overrides})


def _run(**overrides):
    base = {
        "qid": "Q1", "status": "success", "input_dataset_qids": ["Q9493814122238"],
        "data_transformation_sql": [SQL],
    }
    return {**base, **overrides}


def test_parse_history_window_inclusive_bounds():
    assert parse_history_window(SQL, "datum") == HistoryWindow(date(2024, 1, 3), date(2026, 3, 25))


def test_parse_history_window_exclusive_upper_bound_moves_back_a_day():
    sql = "SELECT * FROM df WHERE Meldedatum < DATE '2022-01-01'"
    assert parse_history_window(sql, "Meldedatum") == HistoryWindow(None, date(2021, 12, 31))


def test_parse_history_window_quoted_column_and_plain_string_literal():
    sql = "SELECT * FROM df WHERE \"Meldedatum\" <= '2021-12-31'"
    assert parse_history_window(sql, "Meldedatum").end == date(2021, 12, 31)


def test_parse_history_window_without_upper_bound_is_none():
    assert parse_history_window("SELECT * FROM df WHERE typ = 'x'", "datum") is None


def test_has_equality_filter():
    assert has_equality_filter(SQL, "typ", "Influenza A+B")
    assert not has_equality_filter(SQL, "typ", "RSV A")
    assert has_equality_filter("SELECT * FROM df WHERE \"Altersgruppe\" = '00+'", "Altersgruppe", "00+")


def test_other_dataset_is_not_relevant():
    result = qualify_run(_run(input_dataset_qids=["Q5"]), _benchmark())
    assert result.relevant is False


def test_failed_run_is_listed_with_reason():
    result = qualify_run(_run(status="failed"), _benchmark())
    assert result.relevant and "failed" in result.reason


def test_qualifying_run_has_window_and_no_reason():
    result = qualify_run(_run(), _benchmark())
    assert result.relevant and result.reason is None
    assert result.window == HistoryWindow(date(2024, 1, 3), date(2026, 3, 25))


def test_history_reaching_test_window_is_leakage():
    result = qualify_run(_run(), _benchmark(test_start=date(2026, 3, 25)))
    assert result.relevant and "test" in result.reason


def test_unparseable_sql_is_cannot_be_scored():
    result = qualify_run(_run(data_transformation_sql=["SELECT * FROM df"]), _benchmark())
    assert result.relevant and "history window" in result.reason


def test_missing_filter_in_sql_is_cannot_be_scored():
    result = qualify_run(_run(), _benchmark(filter_column="typ", filter_value="RSV A"))
    assert result.relevant and "filter" in result.reason


def test_matching_filter_passes():
    result = qualify_run(_run(), _benchmark(filter_column="typ", filter_value="Influenza A+B"))
    assert result.reason is None


def test_sql_list_not_aligned_with_datasets_is_cannot_be_scored():
    run = _run(input_dataset_qids=["Q9493814122238", "Q2"], data_transformation_sql=[SQL])
    assert "history window" in qualify_run(run, _benchmark()).reason


def _window_sql(clause):
    return f"SELECT * FROM df WHERE {clause}"


def test_date_column_must_not_match_a_longer_column_name():
    # 'meldedatum' contains 'datum' and must not be read as the date column.
    assert parse_history_window(_window_sql("meldedatum <= '2020-01-01'"), "datum") is None


def test_qualified_date_column_is_accepted():
    sql = _window_sql("df.datum <= '2020-01-01'")
    assert parse_history_window(sql, "datum") == HistoryWindow(None, date(2020, 1, 1))
    sql = _window_sql('"df"."datum" <= \'2020-01-01\'')
    assert parse_history_window(sql, "datum") == HistoryWindow(None, date(2020, 1, 1))


def test_several_distinct_upper_bounds_give_no_window():
    sql = _window_sql("datum <= '2020-01-01' OR datum <= '2025-01-01'")
    assert parse_history_window(sql, "datum") is None


def test_repeated_identical_upper_bound_is_accepted():
    sql = _window_sql("datum <= '2020-01-01' AND datum <= '2020-01-01'")
    assert parse_history_window(sql, "datum") == HistoryWindow(None, date(2020, 1, 1))


def test_several_distinct_lower_bounds_leave_start_unbounded():
    sql = _window_sql("datum >= '2019-01-01' AND datum >= '2020-01-01' AND datum <= '2021-01-01'")
    assert parse_history_window(sql, "datum") == HistoryWindow(None, date(2021, 1, 1))


def test_exclusive_lower_bound_moves_forward_a_day():
    sql = _window_sql("datum > '2023-12-31' AND datum <= '2025-01-01'")
    assert parse_history_window(sql, "datum") == HistoryWindow(date(2024, 1, 1), date(2025, 1, 1))


def test_quoted_cast_column_is_parsed():
    sql = _window_sql('CAST("datum" AS DATE) <= \'2021-01-01\'')
    assert parse_history_window(sql, "datum") == HistoryWindow(None, date(2021, 1, 1))


def test_impossible_date_gives_no_window_and_does_not_raise():
    sql = _window_sql("datum <= '2026-13-45'")
    assert parse_history_window(sql, "datum") is None
    result = qualify_run(_run(data_transformation_sql=[sql]), _benchmark())
    assert result.relevant and "history window" in result.reason


def test_filter_column_must_not_match_a_longer_column_name():
    sql = "SELECT * FROM df WHERE subtyp = 'Influenza A+B'"
    assert not has_equality_filter(sql, "typ", "Influenza A+B")


def test_filter_with_or_is_not_a_single_series():
    sql = "SELECT * FROM df WHERE typ = 'Influenza A+B' OR typ = 'RSV A'"
    assert not has_equality_filter(sql, "typ", "Influenza A+B")


def test_filter_value_is_matched_case_sensitively():
    sql = "SELECT * FROM df WHERE typ = 'influenza a+b'"
    assert not has_equality_filter(sql, "typ", "Influenza A+B")


def test_filter_value_with_apostrophe_matches_sql_escape():
    sql = "SELECT * FROM df WHERE name = 'O''Brien'"
    assert has_equality_filter(sql, "name", "O'Brien")


def test_filter_value_must_not_match_a_prefix_of_a_longer_literal():
    sql = "SELECT * FROM df WHERE name = 'A''B'"
    assert not has_equality_filter(sql, "name", "A")


import pytest


def test_real_catalog_sql_still_parses():
    assert parse_history_window(SQL, "datum") == HistoryWindow(date(2024, 1, 3), date(2026, 3, 25))
    assert has_equality_filter(SQL, "typ", "Influenza A+B")


@pytest.mark.parametrize("clause", [
    "datum <= DATE '2021-12-31' + INTERVAL 1 YEAR",
    "datum <= DATE '2021-12-31' - 5",
    "datum <= DATE '2021-12-31' * 2",
    "datum <= DATE '2021-12-31' || 'x'",
    "datum <= DATE '2021-12-31' OR datum >= DATE '2023-01-01'",
    "NOT (datum <= DATE '2021-12-31')",
    "datum <= DATE '2021-12-31' UNION ALL SELECT * FROM df",
    "datum <= DATE '2021-12-31' EXCEPT SELECT * FROM df",
    "datum <= DATE '2021-12-31' INTERSECT SELECT * FROM df",
    "datum <= DATE '2021-12-31' AND datum >= DATE '2022-06-01'",
])
def test_unsafe_sql_shapes_give_no_window(clause):
    assert parse_history_window(_window_sql(clause), "datum") is None


def test_column_names_containing_or_and_not_do_not_block_parsing():
    sql = "SELECT origin, notification FROM df WHERE datum <= DATE '2021-12-31' ORDER BY origin"
    assert parse_history_window(sql, "datum") == HistoryWindow(None, date(2021, 12, 31))


def test_equality_filter_rejects_not_and_case():
    assert not has_equality_filter("SELECT * FROM df WHERE NOT typ = 'A'", "typ", "A")
    assert not has_equality_filter(
        "SELECT CASE WHEN typ = 'A' THEN 1 END FROM df WHERE typ = 'A'", "typ", "A"
    )
    assert has_equality_filter("SELECT notification FROM df WHERE typ = 'A'", "typ", "A")
