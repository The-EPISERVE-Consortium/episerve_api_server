"""Tests for scoring one run against a benchmark."""

from datetime import date

import pandas as pd
import pytest

from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable, candidate_runs, score_run

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 1, 31))
URI = "https://doip.example/doip/retrieve/Q1791000000001/input.parquet"
COMMIT = "0123abcd" * 8
MANIFEST = {"repo": "benchmarks", "branch": "main", "commit": COMMIT,
            "input": {"qid": "Q1791000000001", "component": "input.parquet", "first_date": "2020-01-01", "last_date": "2021-12-31"}}
DATES = pd.to_datetime(["2022-01-05", "2022-01-12"])
TRUTH = pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]})


def _run(qid="Q10", **overrides):
    base = {
        "qid": qid, "model_name": "timesfm", "docker_tag": "latest", "status": "success",
        "input_sources": [f"{URI}?version={COMMIT}"],
        "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "res",
    }
    return {**base, **overrides}


def _score(run, predictions, **kw):
    return score_run(
        BENCH, run, TRUTH, truth_commit=COMMIT, truth_as_of="2026-10-14", manifest=MANIFEST,
        load_predictions=lambda r, b: predictions, load_params=lambda qid: {"history_length": 91}, **kw,
    )


def test_scores_a_run_and_records_pins_params_and_points():
    forecast = pd.DataFrame({"date": DATES, "forecast": [11.0, 19.0], "q10": [9.0, 18.0], "q90": [12.0, 22.0]})
    entry = _score(_run(), forecast)
    assert entry.metrics["mae"] == pytest.approx(1.0) and entry.metrics["coverage"] == 1.0
    assert (entry.truth_commit, entry.truth_as_of) == (COMMIT, "2026-10-14")
    assert entry.params == {"history_length": 91}
    assert entry.history_end == date(2021, 12, 31) and entry.history_start == date(2020, 1, 1)
    assert entry.points[0] == {"date": "2022-01-05", "forecast": 11.0, "q10": 9.0, "q90": 12.0, "truth": 10.0}
    assert (entry.n_scored, entry.n_expected) == (2, 2)


def test_non_finite_metrics_are_stored_as_none():
    forecast = pd.DataFrame({"date": DATES, "forecast": [11.0, float("inf")]})
    entry = _score(_run(), forecast)
    assert entry.points[1]["forecast"] is None
    assert entry.metrics["mae"] is None and entry.metrics["rmse"] is None
    assert all(v is None or abs(v) != float("inf") for v in entry.metrics.values())


@pytest.mark.parametrize("run,fragment", [
    (_run(status="failed"), "failed"),
    (_run(input_sources=["https://doip.example/doip/retrieve/Q1791000000009/input.parquet"]), "did not read"),
    (_run(input_sources=[f"{URI}?version={'dd' * 32}"]), "version"),
])
def test_unqualified_runs_are_cannot_be_scored_with_a_reason(run, fragment):
    with pytest.raises(NotScorable, match=fragment):
        _score(run, pd.DataFrame({"date": DATES, "forecast": [1.0, 2.0]}))


def test_partial_forecast_is_cannot_be_scored():
    with pytest.raises(NotScorable, match="covers 1 of 2"):
        _score(_run(), pd.DataFrame({"date": DATES[:1], "forecast": [1.0]}))


def test_candidate_runs_explain_why_a_run_cannot_be_added():
    runs = [
        _run("Q10", run_timestamp="2026-01-02T00:00:00Z"),
        _run("Q11", status="failed", run_timestamp="2026-01-03T00:00:00Z"),
        _run("Q12", input_sources=["https://doip.example/doip/retrieve/Q1791000000009/input.parquet"]),
        _run("Q13", run_timestamp="2026-01-01T00:00:00Z"),
    ]
    result = candidate_runs(runs, {"Q13"}, MANIFEST)
    assert [c["qid"] for c in result] == ["Q11", "Q10", "Q13"]          # other input omitted; newest first
    reasons = {c["qid"]: c["reason"] for c in result}
    assert reasons["Q10"] is None
    assert "failed" in reasons["Q11"]
    assert reasons["Q13"] == "already on this leaderboard"


def test_unreadable_predictions_are_cannot_be_scored():
    def boom(run, benchmark):
        raise LoadError("predictions have no column 'v'")

    with pytest.raises(NotScorable, match="no column"):
        score_run(BENCH, _run(), TRUTH, truth_commit=COMMIT, truth_as_of="", load_predictions=boom,
                  load_params=lambda qid: {}, manifest=MANIFEST)


def test_candidates_carry_tag_history_and_parameters_where_they_are_cheap_to_know():
    runs = [_run("Q10", docker_tag="v2"), _run("Q11", status="failed"), _run("Q13"), _run("Q14")]
    asked = []

    def params(qid):
        asked.append(qid)
        if qid == "Q14":
            raise RuntimeError("DOIP down")
        return {"history_length": 91}

    result = {c["qid"]: c for c in candidate_runs(runs, {"Q13"}, MANIFEST, load_params=params)}
    assert result["Q10"]["docker_tag"] == "v2" and result["Q10"]["params"] == {"history_length": 91}
    assert result["Q10"]["history_end"] == date(2021, 12, 31)                # from the manifest
    assert result["Q14"]["params"] == {}                                     # a failing loader gives no parameters
    assert result["Q11"]["params"] is None and result["Q13"]["params"] is None
    assert sorted(asked) == ["Q10", "Q14"]                                   # only for runs that can be added
