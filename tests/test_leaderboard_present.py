"""Tests for leaderboard table formatting."""

from datetime import date

from app.leaderboard.entries import Benchmark
from app.leaderboard.present import summary, table_rows
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
