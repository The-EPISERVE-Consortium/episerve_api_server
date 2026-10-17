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


def test_every_leaderboard_column_has_a_help_text_and_the_metrics_say_which_value_is_better():
    from app.leaderboard.about import COLUMN_HELP
    from app.leaderboard.present import LABELS
    for name in ("rank", "model", "tag", "run", "date", "params", "history", "scored", *LABELS):
        assert COLUMN_HELP[name]
    assert COLUMN_HELP["mae"].endswith("Lower is better.")
    assert COLUMN_HELP["coverage"].endswith("The closer to 80% the better.")
    assert "interval width" in COLUMN_HELP["interval_score"] and "Mean absolute error" in COLUMN_HELP["mae"]


def test_overview_names_the_creator_when_known():
    from app.leaderboard.about import overview_facts
    row = {"target_column": "v", "prediction_length": 5, "entry_count": 1}
    assert ("Created by", "Ada") in overview_facts({**row, "created_by_name": "Ada"})
    assert all(caption != "Created by" for caption, _ in overview_facts({**row, "created_by_name": None}))

