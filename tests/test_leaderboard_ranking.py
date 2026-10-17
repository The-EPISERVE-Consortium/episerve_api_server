"""Tests for ranking stored entries."""

from datetime import date


from app.leaderboard.service import Entry, rank_entries


def _e(qid, model, **metrics):
    base = {"mae": None, "rmse": None, "coverage": None, "interval_score": None}
    return Entry(qid, model, "latest", {}, None, date(2021, 12, 31), "", 2, 2, {**base, **metrics}, [])


def test_lowest_metric_first_and_missing_last():
    entries = [_e("a", "m1", mae=3.0), _e("b", "m2", mae=1.0), _e("c", "m3")]
    assert [e.run_qid for e in rank_entries(entries, "mae", best_per_model=False)] == ["b", "a", "c"]


def test_coverage_ranks_by_closeness_to_eighty_percent():
    entries = [_e("a", "m1", coverage=1.0), _e("b", "m2", coverage=0.5), _e("c", "m3", coverage=0.79)]
    assert [e.run_qid for e in rank_entries(entries, "coverage", best_per_model=False)] == ["c", "a", "b"]


def test_best_run_per_model_keeps_the_best_under_the_chosen_metric():
    entries = [_e("a", "m", mae=5.0), _e("b", "m", mae=2.0), _e("c", "other", mae=3.0)]
    assert [e.run_qid for e in rank_entries(entries, "mae")] == ["b", "c"]
    assert len(rank_entries(entries, "mae", best_per_model=False)) == 3
