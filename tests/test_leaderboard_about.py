"""The generated Data and Evaluation descriptions of a benchmark."""

from datetime import date

from app.leaderboard.about import evaluation_facts, overview_facts, training_facts, METRIC_DOCS, is_baseline
from app.leaderboard.metrics import ALPHA

COMMIT = "0123abcd" * 8


def test_interval_score_doc_uses_the_alpha_of_the_scoring_code():
    meaning = dict((n, m) for n, m, _ in METRIC_DOCS)["interval score"]
    assert f"{2 / ALPHA:g}" in meaning and f"{ALPHA:g}" in meaning


def test_baselines_are_recognised_by_name():
    assert is_baseline("model__prediction__generic__baseline-nullmodel")
    assert is_baseline("Seasonal-Baseline")
    assert not is_baseline("model__prediction__generic__timesfm")


def test_each_fact_is_shown_in_one_place_only():
    row = {"dataset_qid": "Q1", "target_column": "v", "date_column": "datum", "filter_column": "typ",
           "filter_value": "a", "test_start": "2026-04-01", "test_end": "2026-09-30", "history_end": None,
           "prediction_length": 8, "truth_points": 8, "data_commit": "c" * 40, "finalized_at": "2026-10-16",
           "entry_count": 2, "component_name": "d.parquet", "input_filename": "input.parquet"}
    groups = [dict(f(row)) for f in (overview_facts, training_facts, evaluation_facts)]
    keys = [k for g in groups for k in g]
    assert len(keys) == len(set(keys))
    assert groups[0]["Series"] == "v · typ = a" and groups[0]["Runs"] == "2"
    assert groups[1]["Input range"] == "first date → last date before the test start"
    assert groups[2]["Test window"] == "2026-04-01 → 2026-09-30" and groups[2]["Data commit"] == "c" * 40
