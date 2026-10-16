"""Tests for the leaderboard write path with every external system faked."""

from datetime import date

import pandas as pd
import pytest
import requests

from app.leaderboard import actions
from app.leaderboard.run_inputs import RunInput
from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable
from app.leaderboard.service import Entry

COMMIT = "0123abcd" * 8
DATES = pd.to_datetime(["2022-01-05", "2022-01-12"])
ROW = {"id": 3, "name": "Flu", "dataset_qid": "Q1", "target_column": "v", "date_column": "datum",
       "filter_column": None, "filter_value": None, "test_start": date(2022, 1, 1),
       "test_end": date(2022, 1, 31), "truth_points": 2, "created_at": "t", "retired_at": None,
       "entry_count": 0}
FINAL_ROW = {**ROW, "finalized_at": "2026-10-16 11:00:00", "data_commit": COMMIT, "component_name": "d.parquet",
             "history_start": None, "history_end": date(2021, 12, 31), "input_filename": "input.parquet",
             "prediction_length": 2, "description": None}
SQL = "SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2021-12-31'"
RUN = {"qid": "Q10", "model_name": "timesfm", "docker_tag": "latest", "status": "success",
       "input_dataset_qids": ["Q1"], "data_transformation_sql": [SQL],
       "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "res"}


@pytest.fixture
def world(monkeypatch):
    """Fake CKAN, lakeFS, loaders and store; record what is written."""
    log = {"stored": [], "created": [], "removed": [], "retired": []}
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets",
                        lambda: [{"qid": "Q1", "name": "Wastewater", "last_modified": "2026-10-14",
                                  "components": [{"name": "d.parquet", "url": "u"}]}])
    monkeypatch.setattr(actions.ckan_client, "list_model_runs", lambda: [RUN])
    monkeypatch.setattr(actions.ckan_client, "get_datastore_records",
                        lambda rid: [{"datum": "2022-01-05", "v": "11"}, {"datum": "2022-01-12", "v": "21"}])
    def no_head():
        raise AssertionError("add_run must not read the lakeFS head")

    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", no_head)
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: [RunInput("Q1", "d.parquet", COMMIT, SQL)])
    monkeypatch.setattr(actions.bstore, "get", lambda bid: dict(FINAL_ROW) if bid == 3 else None)
    monkeypatch.setattr(actions.loaders, "truth_at_commit",
                        lambda dataset, bench, commit: pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]}))
    monkeypatch.setattr(actions.loaders, "fetch_run_params", lambda qid, base: {"history_length": 91})
    monkeypatch.setattr(actions.store, "get_benchmark", lambda bid: dict(ROW) if bid == 3 else None)
    monkeypatch.setattr(actions.store, "list_entries", lambda bid: list(log["stored"]))

    def create(name, benchmark, truth_points):
        log["created"].append((name, benchmark, truth_points))
        return 8

    def add(bid, entry):
        log["stored"].append(entry)
        return 42

    monkeypatch.setattr(actions.store, "create_benchmark", create)
    monkeypatch.setattr(actions.store, "add_entry", add)
    monkeypatch.setattr(actions.store, "remove_entry", lambda bid, eid: log["removed"].append(eid) or eid == 9)
    monkeypatch.setattr(actions.store, "retire_benchmark", lambda bid: log["retired"].append(bid) or True)
    return log


@pytest.fixture
def creating(world, monkeypatch):
    """The world, with the lakeFS head readable (only create_benchmark still reads it)."""
    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", lambda: COMMIT)
    return world


def _create(**kw):
    args = dict(name=" Flu 2022 ", dataset_qid="Q1", target_column="v", date_column="datum",
                test_start=date(2022, 1, 1), test_end=date(2022, 1, 31))
    return actions.create_benchmark(**{**args, **kw})


def test_create_benchmark_stores_the_task_with_the_observation_count(creating):
    assert _create() == 8
    name, benchmark, points = creating["created"][0]
    assert name == "Flu 2022" and points == 2
    assert benchmark == Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 1, 31))


@pytest.mark.parametrize("kw,message", [
    ({"name": "  "}, "name"),
    ({"test_start": date(2022, 2, 1)}, "test start"),
    ({"filter_column": "typ"}, "filter value"),
])
def test_create_benchmark_rejects_invalid_input(creating, kw, message):
    with pytest.raises(LoadError, match=message):
        _create(**kw)
    assert creating["created"] == []


def test_create_benchmark_unknown_dataset(creating):
    with pytest.raises(actions.NotFound):
        _create(dataset_qid="Q404")


def test_create_benchmark_stores_nothing_when_lakefs_fails(creating, monkeypatch):
    def boom():
        raise RuntimeError("lakeFS down")

    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", boom)
    with pytest.raises(RuntimeError):
        _create()
    assert creating["created"] == []


def test_add_run_scores_against_the_pinned_commit_and_stores(world):
    assert actions.add_run(3, "Q10") == 42
    entry = world["stored"][0]
    assert entry.run_qid == "Q10" and entry.truth_commit == COMMIT and entry.truth_as_of == "2026-10-16 11:00:00"
    assert entry.params == {"history_length": 91} and entry.metrics["mae"] == pytest.approx(1.0)


def test_add_run_unknown_benchmark_retired_benchmark_and_unknown_run(world, monkeypatch):
    with pytest.raises(actions.NotFound):
        actions.add_run(99, "Q10")
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "retired_at": "2026-10-01 00:00:00"})
    with pytest.raises(actions.NotFound):
        actions.add_run(3, "Q10")
    monkeypatch.setattr(actions.bstore, "get", lambda bid: dict(FINAL_ROW))
    with pytest.raises(actions.NotFound):
        actions.add_run(3, "Q404")


def test_add_run_duplicate_is_a_conflict_before_any_heavy_work(world, monkeypatch):
    world["stored"].append(Entry("Q10", "timesfm", "", {}, None, date(2021, 12, 31), "", 2, 2, {}, []))

    def must_not_run(*args, **kwargs):
        raise AssertionError("no lakeFS call expected")

    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", must_not_run)
    with pytest.raises(actions.Conflict):
        actions.add_run(3, "Q10")


def test_add_run_race_is_a_conflict(world, monkeypatch):
    def duplicate(bid, entry):
        raise actions.store.DuplicateEntry("exists")

    monkeypatch.setattr(actions.store, "add_entry", duplicate)
    with pytest.raises(actions.Conflict):
        actions.add_run(3, "Q10")


def test_add_run_stores_nothing_when_the_truth_cannot_be_read(world, monkeypatch):
    def boom(dataset, bench, commit):
        raise RuntimeError("lakeFS down")

    monkeypatch.setattr(actions.loaders, "truth_at_commit", boom)
    with pytest.raises(RuntimeError):
        actions.add_run(3, "Q10")
    assert world["stored"] == []


def test_add_run_cannot_be_scored_propagates_and_stores_nothing(world, monkeypatch):
    monkeypatch.setattr(actions.ckan_client, "list_model_runs", lambda: [{**RUN, "status": "failed"}])
    with pytest.raises(NotScorable, match="failed"):
        actions.add_run(3, "Q10")
    assert world["stored"] == []


def test_datastore_failure_is_cannot_be_scored_not_a_crash(world, monkeypatch):
    def boom(rid):
        raise requests.HTTPError("404")

    monkeypatch.setattr(actions.ckan_client, "get_datastore_records", boom)
    with pytest.raises(NotScorable, match="DataStore"):
        actions.add_run(3, "Q10")


def test_remove_entry_and_retire(world):
    actions.remove_entry(3, 9)
    with pytest.raises(actions.NotFound):
        actions.remove_entry(3, 10)
    actions.retire_benchmark(3)
    assert world["retired"] == [3]


def test_retire_unknown_benchmark(world):
    with pytest.raises(actions.NotFound):
        actions.retire_benchmark(99)


def test_read_leaderboard_uses_only_the_database(world, monkeypatch):
    def external(*args, **kwargs):
        raise AssertionError("reads must not call CKAN, DOIP or lakeFS")

    monkeypatch.setattr(actions.ckan_client, "list_model_runs", external)
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", external)
    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", external)
    low = Entry("Q10", "a", "", {}, None, date(2021, 12, 31), "", 2, 2,
                {"mae": 1.0, "rmse": None, "coverage": None, "interval_score": None}, [],
                truth_as_of="2026-10-14", truth_commit=COMMIT)
    high = Entry("Q11", "b", "", {}, None, date(2021, 12, 31), "", 2, 2,
                 {"mae": 3.0, "rmse": None, "coverage": None, "interval_score": None}, [], truth_as_of="2026-10-10")
    world["stored"].extend([high, low])
    board = actions.read_leaderboard(3, "mae")
    assert [e.run_qid for e in board.entries] == ["Q10", "Q11"]
    assert (board.name, board.benchmark_id, board.truth_points, board.truth_as_of) == ("Flu", 3, 2, "2026-10-14")


def test_read_leaderboard_unknown_benchmark(world):
    with pytest.raises(actions.NotFound):
        actions.read_leaderboard(99)


def test_candidates_mark_runs_already_on_the_board(world):
    world["stored"].append(Entry("Q10", "timesfm", "", {}, None, date(2021, 12, 31), "", 2, 2, {}, []))
    (candidate,) = actions.candidates(3)
    assert candidate["qid"] == "Q10" and candidate["reason"] == "already on this leaderboard"


@pytest.mark.parametrize("kw,limit", [
    ({"name": "n" * 201}, "200"), ({"target_column": "t" * 201}, "200"), ({"date_column": "d" * 201}, "200"),
    ({"filter_column": "f" * 201, "filter_value": "x"}, "200"),
    ({"filter_column": "f", "filter_value": "x" * 501}, "500"),
])
def test_create_benchmark_rejects_values_longer_than_the_columns(creating, kw, limit):
    with pytest.raises(LoadError, match=limit):
        _create(**kw)
    assert creating["created"] == []


def test_create_benchmark_accepts_values_at_the_limits(creating):
    _create(name="n" * 200, target_column="t" * 200, date_column="d" * 200,
            filter_column="f" * 200, filter_value="x" * 500)
    assert len(creating["created"]) == 1


def test_read_leaderboard_works_for_a_retired_benchmark(world, monkeypatch):
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "retired_at": "2026-10-01 00:00:00"})
    world["stored"].append(Entry("Q10", "a", "", {}, None, date(2021, 12, 31), "", 2, 2,
                                 {"mae": 1.0, "rmse": None, "coverage": None, "interval_score": None}, [],
                                 truth_as_of="2026-10-14"))
    board = actions.read_leaderboard(3, "mae")
    assert [e.run_qid for e in board.entries] == ["Q10"]


def test_retire_benchmark_not_found_when_the_store_reports_it_was_not_active(world, monkeypatch):
    monkeypatch.setattr(actions.store, "retire_benchmark", lambda bid: False)
    with pytest.raises(actions.NotFound):
        actions.retire_benchmark(3)


def test_a_removed_run_can_be_added_again(world, monkeypatch):
    stored = {}

    def add(bid, entry):
        stored[7] = entry
        return 7

    monkeypatch.setattr(actions.store, "add_entry", add)
    monkeypatch.setattr(actions.store, "list_entries", lambda bid: list(stored.values()))

    def remove(bid, eid):
        return stored.pop(eid, None) is not None

    monkeypatch.setattr(actions.store, "remove_entry", remove)
    assert actions.add_run(3, "Q10") == 7
    with pytest.raises(actions.Conflict):
        actions.add_run(3, "Q10")
    actions.remove_entry(3, 7)
    assert stored == {}
    assert actions.add_run(3, "Q10") == 7
    assert stored[7].run_qid == "Q10"


def test_add_run_requires_a_final_benchmark(world, monkeypatch):
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "finalized_at": None, "data_commit": None})
    with pytest.raises(actions.NotFound):
        actions.add_run(3, "Q10")                                    # draft
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "retired_at": "t"})
    with pytest.raises(actions.NotFound):
        actions.add_run(3, "Q10")                                    # retired


def test_add_run_scores_against_the_benchmarks_commit_and_stamps_the_freeze_time(world):
    assert actions.add_run(3, "Q10") == 42
    entry = world["stored"][0]
    assert entry.truth_commit == COMMIT and entry.truth_as_of == "2026-10-16 11:00:00"


def test_add_run_refuses_a_run_whose_input_commit_differs(world, monkeypatch):
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: [RunInput("Q1", "d.parquet", "cd" * 32, SQL)])
    with pytest.raises(NotScorable, match="pinned to"):
        actions.add_run(3, "Q10")
    assert world["stored"] == []


def test_add_run_refuses_a_run_with_unreadable_provenance(world, monkeypatch):
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: None)
    with pytest.raises(NotScorable, match="provenance could not be read"):
        actions.add_run(3, "Q10")


def test_load_inputs_swallows_errors(monkeypatch):
    def boom(qid, base):
        raise RuntimeError("DOIP down")

    monkeypatch.setattr(actions.run_inputs, "fetch_run_inputs", boom)
    assert actions._load_inputs("Q10") is None


def test_candidates_use_the_provenance_of_the_benchmarks_pin(world, monkeypatch):
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: [RunInput("Q1", "d.parquet", "cd" * 32, SQL)])
    (candidate,) = actions.candidates(3)
    assert "pinned to" in candidate["reason"]


def test_read_leaderboard_hides_drafts_but_shows_retired_boards(world, monkeypatch):
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "finalized_at": None, "data_commit": None})
    with pytest.raises(actions.NotFound):
        actions.read_leaderboard(3)
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "retired_at": "t"})
    assert actions.read_leaderboard(3).name == FINAL_ROW["name"]
