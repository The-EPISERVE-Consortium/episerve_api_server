"""Tests for leaderboard table formatting."""

from datetime import date

from app.leaderboard.entries import Benchmark
from app.leaderboard.present import (
    chart_option, filter_entries, history_days, history_window, point_rows, summary, table_rows,
)
from app.leaderboard.service import Entry, Leaderboard

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 12, 31))


def _entry(qid, name, **metrics):
    base = {"mae": None, "rmse": None, "coverage": None, "interval_score": None}
    return Entry(qid, name, "latest", {"history_length": 91}, date(2021, 1, 1), date(2021, 12, 31),
                 "t", 52, 52, {**base, **metrics}, [])


def _board(entries):
    return Leaderboard(BENCH, "mae", 52, "2026-10-14", entries, [])


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


def test_summary_names_best_entry_and_setup():
    text = summary(_board([_entry("Q1", "a", mae=1.0)]))
    assert "v" in text and "2022-01-01 → 2022-12-31" in text and "a" in text and "52" in text


def test_summary_without_entries_says_so():
    assert "No scorable runs" in summary(_board([]))


def _hist(qid, name, start, end=date(2021, 12, 31), **metrics):
    e = _entry(qid, name, **metrics)
    e.history_start, e.history_end = start, end
    return e


def test_rows_carry_raw_values_for_sorting():
    rows = table_rows(_board([_entry("Q1", "a", mae=1.234, coverage=0.78), _entry("Q2", "b", mae=2.0)]))
    assert rows[0]["raw"] == {"mae": 1.234, "rmse": None, "coverage": 0.78, "interval_score": None}
    assert rows[1]["raw"]["coverage"] is None and rows[1]["coverage"] == "—"


def test_ties_all_get_best_flag():
    board = _board([
        _entry("Q1", "a", mae=1.0, coverage=0.70),
        _entry("Q2", "b", mae=1.0, coverage=0.90),
        _entry("Q3", "c", mae=2.0, coverage=0.50),
    ])
    rows = table_rows(board)
    assert [r["best"]["mae"] for r in rows] == [True, True, False]
    assert [r["best"]["coverage"] for r in rows] == [True, True, False]   # |0.7-0.8| == |0.9-0.8|
    assert not any(r["best"]["rmse"] for r in rows)


def test_history_days_is_inclusive_and_none_without_start():
    assert history_days(_hist("Q1", "a", date(2021, 12, 30))) == 2
    assert history_days(_hist("Q1", "a", date(2021, 12, 31))) == 1
    assert history_days(_hist("Q1", "a", None)) is None
    assert history_window(_hist("Q1", "a", None)) == "… → 2021-12-31"


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


def test_rows_for_filtered_entries_rerank_and_rehighlight():
    a = _entry("Q1", "a", mae=1.0)
    b = _entry("Q2", "b", mae=2.0)
    rows = table_rows(_board([a, b]), [b])
    assert [r["run_qid"] for r in rows] == ["Q2"] and rows[0]["rank"] == 1 and rows[0]["best"]["mae"] is True


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


def test_chart_band_is_stacked_with_gaps_for_missing_quantiles():
    e = _with_points([
        {"date": "d1", "truth": 1.0, "forecast": 1.0, "q10": 8.0, "q90": 12.0},
        {"date": "d2", "truth": 1.0, "forecast": 1.0, "q10": None, "q90": 12.0},
        {"date": "d3", "truth": 1.0, "forecast": 1.0, "q10": 8.0, "q90": None},
    ])
    option = chart_option(e)
    series = {s["name"]: s for s in option["series"]}
    assert set(series) == {"Observed", "Forecast", "q10 base", "q10–q90"}
    assert series["q10 base"]["stack"] == series["q10–q90"]["stack"]
    assert series["q10 base"]["data"] == [8.0, None, None]
    assert series["q10–q90"]["data"] == [4.0, None, None]
    assert series["q10–q90"]["areaStyle"]["color"].startswith("rgba")
    assert series["q10 base"]["areaStyle"]["opacity"] == 0
    assert "q10 base" not in option["legend"]["data"]
    assert "q10" in option["tooltip"][":formatter"]
