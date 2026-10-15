"""Tests for assembling and ranking a leaderboard from fake loaders."""

import math
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


@pytest.fixture(autouse=True)
def _clear_caches():
    service.clear_caches()
    yield
    service.clear_caches()


def test_non_finite_values_become_none():
    runs = [_run("Q10", "a")]
    board = _build(runs, {"Q10": _pred([float("inf"), 19.0])})
    entry = board.entries[0]
    assert entry.points[0]["forecast"] is None
    for value in entry.metrics.values():
        assert value is None or math.isfinite(value)


@pytest.fixture
def wired(monkeypatch):
    calls = {"truth": 0, "records": 0, "params": 0}
    state = {"params": {"history_length": 91}}

    def load_truth(dataset, benchmark, base):
        calls["truth"] += 1
        return TRUTH.copy()

    def records(resource_id):
        calls["records"] += 1
        return [{"x": 1}]

    def load_predictions(rows, benchmark):
        return _pred([11.0, 21.0])

    def fetch(qid, base):
        calls["params"] += 1
        return state["params"]

    monkeypatch.setattr(service.loaders, "load_truth", load_truth)
    monkeypatch.setattr(service.ckan_client, "get_datastore_records", records)
    monkeypatch.setattr(service.loaders, "load_predictions", load_predictions)
    monkeypatch.setattr(service.loaders, "fetch_run_params", fetch)
    return calls, state, service.default_loaders("http://doip")


def test_default_loaders_cache_each_call(wired):
    calls, _, fns = wired
    ds = {"qid": "Q1", "last_modified": "t1"}
    for _ in range(2):
        fns["load_truth"](ds, BENCH)
        fns["load_predictions"](_run("Q10", "a"), BENCH)
        fns["load_params"]("Q10")
    assert calls == {"truth": 1, "records": 1, "params": 1}


def test_changed_last_modified_reloads_truth(wired):
    calls, _, fns = wired
    fns["load_truth"]({"qid": "Q1", "last_modified": "t1"}, BENCH)
    fns["load_truth"]({"qid": "Q1", "last_modified": "t2"}, BENCH)
    assert calls["truth"] == 2


def test_returned_values_are_copies(wired):
    _, state, fns = wired
    state["params"] = {"nested": {"a": 1}}
    ds = {"qid": "Q1", "last_modified": "t1"}
    frame = fns["load_truth"](ds, BENCH)
    frame.loc[0, "truth"] = -1.0
    assert fns["load_truth"](ds, BENCH).loc[0, "truth"] == 10.0
    run = _run("Q10", "a")
    frame = fns["load_predictions"](run, BENCH)
    frame.loc[0, "forecast"] = -1.0
    assert fns["load_predictions"](run, BENCH).loc[0, "forecast"] == 11.0
    fns["load_params"]("Q10")["nested"]["a"] = 99
    assert fns["load_params"]("Q10") == {"nested": {"a": 1}}


def test_run_without_predictions_resource_raises(wired):
    _, _, fns = wired
    with pytest.raises(LoadError):
        fns["load_predictions"](_run("Q10", "a", predictions_resource_id=None), BENCH)


def test_empty_params_are_not_cached(wired):
    calls, state, fns = wired
    state["params"] = {}
    assert fns["load_params"]("Q10") == {}
    state["params"] = {"history_length": 7}
    assert fns["load_params"]("Q10") == {"history_length": 7}
    assert calls["params"] == 2


def test_forecast_past_last_truth_date_is_ranked_on_observed_dates():
    longer = pd.DataFrame({
        "date": pd.to_datetime(["2022-01-05", "2022-01-12", "2022-01-19"]),
        "forecast": [11.0, 21.0, 999.0],
    })
    board = _build([_run("Q10", "a")], {"Q10": longer}, rank_by="mae")
    assert len(board.entries) == 1
    assert board.entries[0].n_scored == 2 and board.entries[0].n_expected == 2
    assert board.entries[0].metrics["mae"] == pytest.approx(1.0)


def test_datastore_failure_lists_run_as_not_scorable_and_keeps_others(monkeypatch):
    import requests

    service.clear_caches()

    def records(resource_id):
        if resource_id == "res-Q10":
            raise requests.HTTPError("404 no datastore table")
        return [{"datum": "2022-01-05", "v": "11"}, {"datum": "2022-01-12", "v": "21"}]

    monkeypatch.setattr(service.ckan_client, "get_datastore_records", records)
    fns = service.default_loaders("http://doip")
    board = service.build_leaderboard(
        BENCH, [_run("Q10", "a"), _run("Q11", "b")], {"last_modified": "t"},
        load_truth=lambda dataset, benchmark: TRUTH,
        load_predictions=fns["load_predictions"],
        load_params=lambda qid: {},
        rank_by="mae",
    )
    assert [e.model_name for e in board.entries] == ["b"]
    assert [(s.run_qid, s.reason) for s in board.not_scorable] == [
        ("Q10", "predictions could not be read from the CKAN DataStore")
    ]
    service.clear_caches()


def test_runtime_error_from_ckan_is_also_a_load_error(monkeypatch):
    service.clear_caches()

    def records(resource_id):
        raise RuntimeError("CKAN datastore_search failed")

    monkeypatch.setattr(service.ckan_client, "get_datastore_records", records)
    fns = service.default_loaders("http://doip")
    with pytest.raises(LoadError):
        fns["load_predictions"](_run("Q10", "a"), BENCH)
    service.clear_caches()
