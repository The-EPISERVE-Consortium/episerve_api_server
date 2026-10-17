"""The generated Data and Evaluation descriptions of a benchmark."""

from datetime import date

from app.leaderboard.about import METRIC_DOCS, is_baseline
from app.leaderboard.metrics import ALPHA

COMMIT = "0123abcd" * 8


def test_interval_score_doc_uses_the_alpha_of_the_scoring_code():
    meaning = dict((n, m) for n, m, _ in METRIC_DOCS)["interval score"]
    assert f"{2 / ALPHA:g}" in meaning and f"{ALPHA:g}" in meaning


def test_baselines_are_recognised_by_name():
    assert is_baseline("model__prediction__generic__baseline-nullmodel")
    assert is_baseline("Seasonal-Baseline")
    assert not is_baseline("model__prediction__generic__timesfm")
