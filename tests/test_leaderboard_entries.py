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


def test_parse_history_window_without_upper_bound_is_none():
    assert parse_history_window("SELECT * FROM df WHERE typ = 'x'", "datum") is None


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
    result = qualify_run(_run(), _benchmark(filters=(("typ", "RSV A"),)))
    assert result.relevant and "filter" in result.reason


def _window_sql(clause):
    return f"SELECT * FROM df WHERE {clause}"


def test_date_column_must_not_match_a_longer_column_name():
    # 'meldedatum' contains 'datum' and must not be read as the date column.
    assert parse_history_window(_window_sql("meldedatum <= '2020-01-01'"), "datum") is None


def test_impossible_date_gives_no_window_and_does_not_raise():
    sql = _window_sql("datum <= '2026-13-45'")
    assert parse_history_window(sql, "datum") is None
    result = qualify_run(_run(data_transformation_sql=[sql]), _benchmark())
    assert result.relevant and "history window" in result.reason


def test_filter_with_or_is_not_a_single_series():
    sql = "SELECT * FROM df WHERE typ = 'Influenza A+B' OR typ = 'RSV A'"
    assert not has_equality_filter(sql, "typ", "Influenza A+B")


import pytest


@pytest.mark.parametrize("clause", [
    "datum <= DATE '2021-12-31' + INTERVAL 1 YEAR",
    "datum <= DATE '2021-12-31' OR datum >= DATE '2023-01-01'",
    "NOT (datum <= DATE '2021-12-31')",
    "datum <= DATE '2021-12-31' UNION ALL SELECT * FROM df",
    "datum <= DATE '2021-12-31' AND datum >= DATE '2022-06-01'",
])
def test_unsafe_sql_shapes_give_no_window(clause):
    assert parse_history_window(_window_sql(clause), "datum") is None


def test_equality_filter_rejects_not_and_case():
    assert not has_equality_filter("SELECT * FROM df WHERE NOT typ = 'A'", "typ", "A")
    assert not has_equality_filter(
        "SELECT CASE WHEN typ = 'A' THEN 1 END FROM df WHERE typ = 'A'", "typ", "A"
    )
    assert has_equality_filter("SELECT notification FROM df WHERE typ = 'A'", "typ", "A")


from dataclasses import replace

from app.leaderboard.run_inputs import RunInput

PIN = "ab" * 32


def _strict(**overrides):
    return replace(_benchmark(), data_commit=PIN, **overrides)


def _input(**overrides):
    base = dict(dataset_qid="Q9493814122238", filename="x.parquet", commit=PIN, sql=SQL)
    return RunInput(**{**base, **overrides})


def test_strict_run_with_the_pinned_commit_qualifies_using_the_provenance_sql():
    run = _run(data_transformation_sql=["SELECT garbage"])        # CKAN extra is ignored in strict mode
    result = qualify_run(run, _strict(), inputs=[_input()])
    assert result.reason is None and result.window == HistoryWindow(date(2024, 1, 3), date(2026, 3, 25))


def test_strict_run_with_another_commit_is_refused_with_both_ids():
    result = qualify_run(_run(), _strict(), inputs=[_input(commit="cd" * 32)])
    assert result.relevant and "input was read at commit cdcdcdcdcdcd" in result.reason
    assert "pinned to abababababab" in result.reason


def test_strict_run_without_readable_provenance_or_matching_input():
    assert "provenance could not be read" in qualify_run(_run(), _strict(), inputs=None).reason
    reason = qualify_run(_run(), _strict(), inputs=[_input(dataset_qid="Q5")]).reason
    assert "no input from this benchmark's dataset" in reason


def test_strict_run_still_applies_status_leakage_and_filter_rules():
    assert "failed" in qualify_run(_run(status="failed"), _strict(), inputs=[_input()]).reason
    leaky = _input(sql="SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2026-04-10'")
    assert "test start" in qualify_run(_run(), _strict(), inputs=[leaky]).reason
    missing_filter = replace(_strict(), filters=(("typ", "RSV A"),))
    assert "filter" in qualify_run(_run(), missing_filter, inputs=[_input()]).reason
    assert qualify_run(_run(input_dataset_qids=["Q5"]), _strict(), inputs=[_input()]).relevant is False


def test_strict_run_listing_the_dataset_twice_is_refused_even_if_one_is_pinned():
    pinned, unpinned = _input(), _input(commit="cd" * 32, sql="SELECT * FROM df")
    for inputs in ([pinned, unpinned], [unpinned, pinned], [pinned, _input()]):
        reason = qualify_run(_run(), _strict(), inputs=inputs).reason
        assert "several inputs from this benchmark's dataset" in reason and "exactly one" in reason


def test_every_filter_of_a_benchmark_must_be_in_the_runs_sql():
    sql = "SELECT * FROM df WHERE typ = 'RSV A' AND CAST(datum AS DATE) <= DATE '2021-12-31'"
    run = _run(data_transformation_sql=[sql])
    one = qualify_run(run, _benchmark(filters=(("typ", "RSV A"),)))
    assert one.reason is None
    two = qualify_run(run, _benchmark(filters=(("typ", "RSV A"), ("age", "35-59"))))
    assert two.relevant and "filter age = '35-59' not found" in two.reason
    both = sql.replace("WHERE typ = 'RSV A'", "WHERE typ = 'RSV A' AND age = '35-59'")
    assert qualify_run(_run(data_transformation_sql=[both]),
                       _benchmark(filters=(("typ", "RSV A"), ("age", "35-59")))).reason is None


def test_a_run_of_a_benchmark_with_covariates_must_read_exactly_the_generated_input_file():
    manifest = {"repo": "benchmarks", "branch": "main", "commit": "ee" * 32,
                "input": {"path": "3/input.parquet", "first_date": "2021-01-06", "last_date": "2021-12-29"}}
    uri = "lakefs://benchmarks/main/3/input.parquet"
    run = {"qid": "Q10", "status": "success", "input_sources": [f"{uri}?version={'ee' * 32}"]}
    ok = qualify_run(run, _benchmark(), manifest=manifest)
    assert ok.relevant and ok.reason is None and ok.window == HistoryWindow(date(2021, 1, 6), date(2021, 12, 29))
    stale = qualify_run({**run, "input_sources": [f"{uri}?version={'dd' * 32}"]}, _benchmark(), manifest=manifest)
    assert stale.relevant and "dddddddddddd" in stale.reason and "eeeeeeeeeeee" in stale.reason
    extra = qualify_run({**run, "input_sources": [*run["input_sources"], "https://doip/doip/retrieve/Q1/x"]},
                        _benchmark(), manifest=manifest)
    assert extra.relevant and "exactly that file" in extra.reason
    assert qualify_run({**run, "status": "failed"}, _benchmark(), manifest=manifest).reason.startswith("run status is failed")
    unrelated = qualify_run({"qid": "Q11", "status": "success", "input_sources": ["lakefs://benchmarks/main/4/input.parquet"]},
                            _benchmark(), manifest=manifest)
    assert unrelated.relevant is False
    assert qualify_run({"qid": "Q12", "status": "success"}, _benchmark(), manifest=manifest).relevant is False
