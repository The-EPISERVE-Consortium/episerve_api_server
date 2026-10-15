"""Tests for assembling and ranking a leaderboard from fake loaders."""

from datetime import date

import pandas as pd
import pytest

from app.leaderboard import service
from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError

BENCH = Benchmark(
    dataset_qid="Q1", target_column="v", date_column="datum",
    test_start=date(2022, 1, 1), test_end=date(2022, 1, 31),
)
SQL = "SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2021-12-31'"
DATES = pd.to_datetime(["2022-01-05", "2022-01-12"])
TRUTH = pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]})


def _run(qid, model, **overrides):
    base = {
        "qid": qid, "model_name": model, "docker_tag": "latest", "status": "success",
        "input_dataset_qids": ["Q1"], "data_transformation_sql": [SQL],
        "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": f"res-{qid}",
    }
    return {**base, **overrides}


def _pred(values, q10=None, q90=None):
    frame = pd.DataFrame({"date": DATES, "forecast": values})
    if q10 is not None:
        frame["q10"], frame["q90"] = q10, q90
    return frame


def _build(runs, predictions, **kwargs):
    def load_predictions(run, benchmark):
        value = predictions[run["qid"]]
        if isinstance(value, Exception):
            raise value
        return value

    return service.build_leaderboard(
        BENCH, runs, {"last_modified": "2026-10-14T00:00:00Z"},
        load_truth=lambda dataset, benchmark: TRUTH,
        load_predictions=load_predictions,
        load_params=lambda qid: {"history_length": 91},
        **kwargs,
    )


def test_ranks_by_chosen_metric_lowest_first():
    runs = [_run("Q10", "a"), _run("Q11", "b")]
    preds = {"Q10": _pred([14.0, 24.0]), "Q11": _pred([11.0, 21.0])}
    board = _build(runs, preds, rank_by="mae")
    assert [e.model_name for e in board.entries] == ["b", "a"]
    assert board.entries[0].metrics["mae"] == pytest.approx(1.0)
    assert board.truth_points == 2 and board.truth_as_of == "2026-10-14T00:00:00Z"


def test_coverage_ranks_by_closeness_to_eighty_percent():
    runs = [_run("Q10", "full"), _run("Q11", "near")]
    preds = {
        "Q10": _pred([10.0, 20.0], q10=[0.0, 0.0], q90=[99.0, 99.0]),   # coverage 1.0
        "Q11": _pred([10.0, 20.0], q10=[9.0, 25.0], q90=[11.0, 30.0]),  # coverage 0.5
    }
    board = _build(runs, preds, rank_by="coverage")
    assert [e.model_name for e in board.entries] == ["full", "near"]  # |1.0-0.8| < |0.5-0.8|


def test_missing_metric_sorts_last():
    runs = [_run("Q10", "no-q"), _run("Q11", "with-q")]
    preds = {"Q10": _pred([10.0, 20.0]), "Q11": _pred([10.0, 20.0], q10=[9.0, 19.0], q90=[11.0, 21.0])}
    board = _build(runs, preds, rank_by="interval_score")
    assert [e.model_name for e in board.entries] == ["with-q", "no-q"]


def test_best_run_per_model_by_default_and_all_runs_on_request():
    runs = [_run("Q10", "m"), _run("Q11", "m")]
    preds = {"Q10": _pred([14.0, 24.0]), "Q11": _pred([11.0, 21.0])}
    assert [e.run_qid for e in _build(runs, preds, rank_by="mae").entries] == ["Q11"]
    assert len(_build(runs, preds, rank_by="mae", best_per_model=False).entries) == 2


def test_partial_forecast_is_not_scorable():
    runs = [_run("Q10", "short")]
    preds = {"Q10": _pred([10.0, 20.0]).iloc[:1]}
    board = _build(runs, preds)
    assert board.entries == []
    assert "covers 1 of 2" in board.not_scorable[0].reason


def test_load_error_and_disqualified_runs_are_listed_with_reasons():
    runs = [
        _run("Q10", "bad-preds"),
        _run("Q11", "failed", status="failed"),
        _run("Q12", "other", input_dataset_qids=["Q9"]),
    ]
    board = _build(runs, {"Q10": LoadError("predictions have no column 'v'")})
    reasons = {s.run_qid: s.reason for s in board.not_scorable}
    assert "no column" in reasons["Q10"]
    assert "failed" in reasons["Q11"] or "status" in reasons["Q11"]
    assert "Q12" not in reasons


def test_entry_carries_params_history_and_points():
    runs = [_run("Q10", "a")]
    board = _build(runs, {"Q10": _pred([11.0, 19.0], q10=[9.0, 18.0], q90=[12.0, 22.0])})
    entry = board.entries[0]
    assert entry.params == {"history_length": 91}
    assert entry.history_end == date(2021, 12, 31) and entry.history_start is None
    assert entry.points[0] == {"date": "2022-01-05", "forecast": 11.0, "q10": 9.0, "q90": 12.0, "truth": 10.0}
