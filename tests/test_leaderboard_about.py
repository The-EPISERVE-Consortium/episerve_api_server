"""The generated Data and Evaluation descriptions of a benchmark."""

from datetime import date

from app.leaderboard.about import METRIC_DOCS, data_facts, entry_rules, is_baseline
from app.leaderboard.metrics import ALPHA, METRICS
from app.leaderboard.present import LABELS

COMMIT = "0123abcd" * 8
ROW = {"id": 3, "name": "Flu", "dataset_qid": "Q1", "target_column": "v", "date_column": "datum",
       "filter_column": "typ", "filter_value": "SARS-CoV-2", "test_start": date(2026, 8, 19),
       "test_end": date(2026, 10, 7), "history_start": None, "history_end": date(2026, 8, 12),
       "input_filename": "input.parquet", "component_name": "d.parquet", "prediction_length": 8,
       "truth_points": 8, "data_commit": COMMIT, "finalized_at": "2026-10-16 22:46:17"}
SPEC = {"input_data_files": [["https://doip.example/doip/retrieve/Q1/d.parquet?version=" + COMMIT, "input.parquet"]],
        "data_transformation_sql": ["SELECT 1"], "config": {"prediction_length": 8, "prediction_offset": 0}}


def test_data_facts_describe_the_series_and_the_pin():
    facts = dict(data_facts(ROW))
    assert facts["Dataset"] == "Q1"
    assert facts["Component"] == "d.parquet"
    assert facts["Target column"] == "v" and facts["Date column"] == "datum"
    assert facts["Filter"] == "typ = SARS-CoV-2"
    assert facts["Input range"] == "first date → 2026-08-12"
    assert facts["Test window"] == "2026-08-19 → 2026-10-07"
    assert facts["Observations in the test window"] == "8"
    assert facts["Forecast length"] == "8 steps"
    assert facts["Input file name"] == "input.parquet"
    assert facts["Data commit"] == COMMIT
    assert facts["Pinned"] == "2026-10-16 22:46:17"


def test_data_facts_without_a_filter_or_pin():
    row = {**ROW, "filter_column": None, "filter_value": None, "data_commit": None, "finalized_at": None,
           "truth_points": None, "prediction_length": None, "component_name": None, "history_end": None}
    facts = dict(data_facts(row))
    assert facts["Filter"] == "none"
    assert facts["Input range"] == "first date → last date before the test start"
    assert facts["Data commit"] == "—" and facts["Pinned"] == "—"
    assert facts["Observations in the test window"] == "—" and facts["Forecast length"] == "—"
    assert facts["Component"] == "—"


def test_every_ranking_metric_is_documented_with_its_direction():
    assert [name for name, *_ in METRIC_DOCS] == [LABELS[m] for m in METRICS]
    docs = {name: (meaning, better) for name, meaning, better in METRIC_DOCS}
    assert docs["MAE"][1] == "lower"
    assert docs["80% coverage"][1] == "closest to 80%"
    assert docs["interval score"][1] == "lower"


def test_interval_score_doc_uses_the_alpha_of_the_scoring_code():
    meaning = dict((n, m) for n, m, _ in METRIC_DOCS)["interval score"]
    assert f"{2 / ALPHA:g}" in meaning and f"{ALPHA:g}" in meaning


def test_entry_rules_mention_the_pinned_commit_and_the_filter():
    rules = entry_rules(ROW)
    assert any(COMMIT[:12] in r for r in rules)
    assert any("typ = SARS-CoV-2" in r for r in rules)
    assert any("2026-08-19" in r for r in rules)


def test_entry_rules_without_a_filter_have_no_filter_rule():
    rules = entry_rules({**ROW, "filter_column": None, "filter_value": None})
    assert not any("filter" in r.lower() for r in rules)


def test_baselines_are_recognised_by_name():
    assert is_baseline("model__prediction__generic__baseline-nullmodel")
    assert is_baseline("Seasonal-Baseline")
    assert not is_baseline("model__prediction__generic__timesfm")
