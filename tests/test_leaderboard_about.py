"""The generated Data and Evaluation descriptions of a benchmark."""

from app.leaderboard.about import METRIC_DOCS, is_baseline, overview_facts
from app.leaderboard.metrics import ALPHA

COMMIT = "0123abcd" * 8


def test_interval_score_doc_uses_the_alpha_of_the_scoring_code():
    meaning = dict((n, m) for n, m, _ in METRIC_DOCS)["interval score"]
    assert f"{2 / ALPHA:g}" in meaning and f"{ALPHA:g}" in meaning


def test_baselines_are_recognised_by_name():
    assert is_baseline("model__prediction__generic__baseline-nullmodel")
    assert is_baseline("Seasonal-Baseline")
    assert not is_baseline("model__prediction__generic__timesfm")


def test_overview_facts_name_the_series_the_horizon_and_the_runs():
    row = {"target_column": "v", "filter_column": "typ", "filter_value": "a", "prediction_length": 8, "entry_count": 2}
    assert overview_facts(row) == [("Series", "v · typ = a"), ("Forecast horizon", "8 steps"), ("Runs", "2")]
    bare = {"target_column": "v", "filter_column": None, "prediction_length": None}
    assert overview_facts(bare) == [("Series", "v"), ("Forecast horizon", "—")]
