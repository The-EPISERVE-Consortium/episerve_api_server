"""Tests for scoring one run against a benchmark."""

from datetime import date

import pandas as pd
import pytest

from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable, candidate_runs, score_run

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 1, 31))
SQL = "SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2021-12-31'"
DATES = pd.to_datetime(["2022-01-05", "2022-01-12"])
TRUTH = pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]})
COMMIT = "0123abcd" * 8


def _run(qid="Q10", **overrides):
    base = {
        "qid": qid, "model_name": "timesfm", "docker_tag": "latest", "status": "success",
        "input_dataset_qids": ["Q1"], "data_transformation_sql": [SQL],
        "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "res",
    }
    return {**base, **overrides}


def _score(run, predictions, **kw):
    return score_run(
        BENCH, run, TRUTH, truth_commit=COMMIT, truth_as_of="2026-10-14",
        load_predictions=lambda r, b: predictions, load_params=lambda qid: {"history_length": 91}, **kw,
    )


def test_scores_a_run_and_records_pins_params_and_points():
    forecast = pd.DataFrame({"date": DATES, "forecast": [11.0, 19.0], "q10": [9.0, 18.0], "q90": [12.0, 22.0]})
    entry = _score(_run(), forecast)
    assert entry.metrics["mae"] == pytest.approx(1.0) and entry.metrics["coverage"] == 1.0
    assert (entry.truth_commit, entry.truth_as_of) == (COMMIT, "2026-10-14")
    assert entry.params == {"history_length": 91}
    assert entry.history_end == date(2021, 12, 31) and entry.history_start is None
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
    (_run(input_dataset_qids=["Q9"]), "dataset"),
    (_run(data_transformation_sql=["SELECT 1"]), "history window"),
])
def test_unqualified_runs_are_cannot_be_scored_with_a_reason(run, fragment):
    with pytest.raises(NotScorable, match=fragment):
        _score(run, pd.DataFrame({"date": DATES, "forecast": [1.0, 2.0]}))


def test_leakage_is_cannot_be_scored():
    leaky = _run(data_transformation_sql=["SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2022-01-10'"])
    with pytest.raises(NotScorable, match="test start"):
        _score(leaky, pd.DataFrame({"date": DATES, "forecast": [1.0, 2.0]}))


def test_partial_forecast_is_cannot_be_scored():
    with pytest.raises(NotScorable, match="covers 1 of 2"):
        _score(_run(), pd.DataFrame({"date": DATES[:1], "forecast": [1.0]}))


def test_unreadable_predictions_are_cannot_be_scored():
    def boom(run, benchmark):
        raise LoadError("predictions have no column 'v'")

    with pytest.raises(NotScorable, match="no column"):
        score_run(BENCH, _run(), TRUTH, truth_commit=COMMIT, truth_as_of="", load_predictions=boom,
                  load_params=lambda qid: {})


def test_forecast_past_the_last_observation_is_scored_on_observed_dates():
    dates = pd.to_datetime(["2022-01-05", "2022-01-12", "2022-01-19"])
    entry = _score(_run(), pd.DataFrame({"date": dates, "forecast": [11.0, 21.0, 99.0]}))
    assert entry.n_expected == 2 and entry.metrics["mae"] == pytest.approx(1.0)


def test_candidate_runs_explain_why_a_run_cannot_be_added():
    runs = [
        _run("Q10", run_timestamp="2026-01-02T00:00:00Z"),
        _run("Q11", status="failed", run_timestamp="2026-01-03T00:00:00Z"),
        _run("Q12", input_dataset_qids=["Q9"]),
        _run("Q13", run_timestamp="2026-01-01T00:00:00Z"),
    ]
    result = candidate_runs(BENCH, runs, active_run_qids={"Q13"})
    assert [c["qid"] for c in result] == ["Q11", "Q10", "Q13"]          # other dataset omitted; newest first
    reasons = {c["qid"]: c["reason"] for c in result}
    assert reasons["Q10"] is None
    assert "failed" in reasons["Q11"]
    assert reasons["Q13"] == "already on this leaderboard"


from dataclasses import replace

from app.leaderboard.run_inputs import RunInput

PIN = "ab" * 32
STRICT = replace(BENCH, data_commit=PIN)


def test_score_run_in_strict_mode_uses_the_provenance_inputs():
    forecast = pd.DataFrame({"date": DATES, "forecast": [11.0, 19.0]})
    good = [RunInput("Q1", "x.parquet", PIN, SQL)]
    entry = score_run(STRICT, _run(data_transformation_sql=["SELECT 1"]), TRUTH, truth_commit=PIN, truth_as_of="t",
                      load_predictions=lambda r, b: forecast, load_params=lambda q: {}, inputs=good)
    assert entry.truth_commit == PIN
    stale = [RunInput("Q1", "x.parquet", "cd" * 32, SQL)]
    with pytest.raises(NotScorable, match="pinned to"):
        score_run(STRICT, _run(), TRUTH, truth_commit=PIN, truth_as_of="t",
                  load_predictions=lambda r, b: forecast, load_params=lambda q: {}, inputs=stale)


def test_candidates_for_a_strict_benchmark_check_provenance_only_where_needed():
    runs = [_run("Q10"), _run("Q11"), _run("Q12", status="failed"), _run("Q13"), _run("Q14")]
    calls = []

    def inputs_for(qid):
        calls.append(qid)
        if qid == "Q11":
            return [RunInput("Q1", "x.parquet", "cd" * 32, SQL)]
        if qid == "Q10":
            return [RunInput("Q1", "x.parquet", PIN, SQL)]
        raise RuntimeError("DOIP down")

    result = {c["qid"]: c["reason"] for c in candidate_runs(STRICT, runs, {"Q13"}, load_inputs=inputs_for)}
    assert result["Q10"] is None
    assert "pinned to" in result["Q11"]
    assert "failed" in result["Q12"]
    assert result["Q13"] == "already on this leaderboard"
    assert "provenance could not be read" in result["Q14"]
    assert sorted(calls) == ["Q10", "Q11", "Q14"]                       # not for failed or already-added runs


def test_candidates_fetch_provenance_in_parallel_and_keep_the_order():
    import threading

    runs = [_run(f"Q{n}", run_timestamp=f"2026-01-{n - 29:02d}T00:00:00Z") for n in range(30, 40)]
    barrier = threading.Barrier(3, timeout=5)                       # three calls must be in flight together
    seen = []

    def inputs_for(qid):
        seen.append(qid)
        if qid in ("Q30", "Q31", "Q32"):
            barrier.wait()
        if qid == "Q35":
            raise RuntimeError("DOIP down")
        return [RunInput("Q1", "x.parquet", PIN if qid != "Q36" else "cd" * 32, SQL)]

    result = candidate_runs(STRICT, runs, set(), load_inputs=inputs_for)
    assert sorted(seen) == [f"Q{n}" for n in range(30, 40)]
    assert [c["qid"] for c in result] == [f"Q{n}" for n in range(39, 29, -1)]      # newest first, as before
    reasons = {c["qid"]: c["reason"] for c in result}
    assert reasons["Q30"] is None and "pinned to" in reasons["Q36"]
    assert "provenance could not be read" in reasons["Q35"]
