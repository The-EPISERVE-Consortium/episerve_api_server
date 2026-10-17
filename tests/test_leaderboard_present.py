"""Tests for leaderboard table formatting."""

from datetime import date

from app.leaderboard.entries import Benchmark
from app.leaderboard.present import filter_entries, point_rows, summary, table_rows
from app.leaderboard.service import Entry, Leaderboard

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 12, 31))


def _entry(qid, name, **metrics):
    base = {"mae": None, "rmse": None, "coverage": None, "interval_score": None}
    return Entry(qid, name, "latest", {"history_length": 91}, date(2021, 1, 1), date(2021, 12, 31),
                 "t", 52, 52, {**base, **metrics}, [])


def _board(entries):
    return Leaderboard(BENCH, "mae", 52, "2026-10-14", entries)


def test_rows_format_values_and_mark_best_per_column():
    board = _board([
        _entry("Q1", "a", mae=1.234, rmse=2.0, coverage=0.78, interval_score=60.0),
        _entry("Q2", "b", mae=2.0, rmse=1.5, coverage=0.5, interval_score=None),
    ])
    rows = table_rows(board)
    assert rows[0]["rank"] == 1 and rows[0]["mae"] == "1.23" and rows[0]["coverage"] == "78%"
    assert rows[1]["interval_score"] == "—"
    assert rows[0]["best"]["mae"] is True and rows[1]["best"]["mae"] is False
    assert rows[1]["best"]["rmse"] is True            # b has the lower RMSE
    assert rows[0]["best"]["coverage"] is True        # 0.78 is nearest to 0.8
    assert rows[0]["history"] == "2021-01-01 → 2021-12-31"
    assert rows[0]["params"] == "history_length=91"


def test_summary_names_the_best_entry_and_the_ranking_only():
    text = summary(_board([_entry("Q1", "a", mae=1.0)]))
    assert text == "Best entry: a, ranked by MAE."


def _hist(qid, name, start, end=date(2021, 12, 31), **metrics):
    e = _entry(qid, name, **metrics)
    e.history_start, e.history_end = start, end
    return e


def test_filter_entries_by_model_and_min_history():
    a = _hist("Q1", "a", date(2021, 1, 1))          # 365 days
    b = _hist("Q2", "b", date(2021, 12, 1))         # 31 days
    c = _hist("Q3", "a", None)                      # unknown
    all_ = [a, b, c]
    assert filter_entries(all_) == all_
    assert filter_entries(all_, model="a") == [a, c]
    assert filter_entries(all_, min_history_days=100) == [a]
    assert filter_entries(all_, min_history_days=31) == [a, b]
    assert filter_entries(all_, model="a", min_history_days=1) == [a]
    assert filter_entries(all_, min_history_days=None) == all_


def _with_points(points):
    e = _entry("Q1", "a")
    e.points = points
    return e


def test_point_rows_format_and_error():
    e = _with_points([
        {"date": "2022-01-05", "truth": 10.0, "forecast": 11.5, "q10": 9.0, "q90": None},
        {"date": "2022-01-12", "truth": None, "forecast": 3.0, "q10": None, "q90": None},
    ])
    rows = point_rows(e)
    assert rows[0] == {"date": "2022-01-05", "observed": "10.00", "forecast": "11.50",
                       "q10": "9.00", "q90": "—", "error": "1.50"}
    assert rows[1]["observed"] == "—" and rows[1]["error"] == "—"


def test_summary_flags_mixed_data_versions():
    import dataclasses

    entries = [dataclasses.replace(_entry("Q1", "a", mae=1.0), truth_commit="aaaaaaaa"),
               dataclasses.replace(_entry("Q2", "b", mae=2.0), truth_commit="bbbbbbbb")]
    assert "2 different data versions" in summary(_board(entries))


def _run(qid, name, params=None, timestamp="2026-01-01T00:00:00Z"):
    return Entry(qid, name, "latest", params or {"history_length": 91}, date(2021, 1, 1), date(2021, 12, 31),
                 timestamp, 52, 52, {"mae": 1.0, "rmse": 1.0, "coverage": None, "interval_score": None}, [])


def test_rows_carry_the_run_date_and_a_baseline_flag():
    rows = table_rows(_board([_run("Q10", "timesfm", timestamp="2026-10-16T22:48:17Z"), _run("Q11", "x__baseline-naive")]))
    by_qid = {r["run_qid"]: r for r in rows}
    assert by_qid["Q10"]["date"] == "2026-10-16 22:48"
    assert by_qid["Q10"]["baseline"] is False and by_qid["Q11"]["baseline"] is True
