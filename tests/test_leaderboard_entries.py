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


def test_unparseable_sql_is_not_scorable():
    result = qualify_run(_run(data_transformation_sql=["SELECT * FROM df"]), _benchmark())
    assert result.relevant and "history window" in result.reason


def test_missing_filter_in_sql_is_not_scorable():
    result = qualify_run(_run(), _benchmark(filter_column="typ", filter_value="RSV A"))
    assert result.relevant and "filter" in result.reason


def test_matching_filter_passes():
    result = qualify_run(_run(), _benchmark(filter_column="typ", filter_value="Influenza A+B"))
    assert result.reason is None


def test_sql_list_not_aligned_with_datasets_is_not_scorable():
    run = _run(input_dataset_qids=["Q9493814122238", "Q2"], data_transformation_sql=[SQL])
    assert "history window" in qualify_run(run, _benchmark()).reason
