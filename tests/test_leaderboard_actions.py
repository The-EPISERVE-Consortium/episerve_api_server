"""Tests for the leaderboard write path with every external system faked."""

from datetime import date

import pandas as pd
import pytest
import requests

from app.leaderboard import actions
from app.leaderboard.run_inputs import RunInput
from app.leaderboard.scoring import NotScorable
from app.leaderboard.service import Entry

COMMIT = "0123abcd" * 8
_real_truth_from_pin = actions.loaders.truth_from_pin
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
    """Fake CKAN, loaders and store; record what is written."""
    log = {"stored": [], "removed": [], "truth_calls": []}
    monkeypatch.setattr(actions.ckan_client, "list_model_runs", lambda: [RUN])
    monkeypatch.setattr(actions.ckan_client, "get_datastore_records",
                        lambda rid: [{"datum": "2022-01-05", "v": "11"}, {"datum": "2022-01-12", "v": "21"}])
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: [RunInput("Q1", "d.parquet", COMMIT, SQL)])
    monkeypatch.setattr(actions.bstore, "get", lambda bid: dict(FINAL_ROW) if bid == 3 else None)
    def truth_from_pin(qid, component_name, commit, bench):
        log["truth_calls"].append((qid, component_name, commit))
        return pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]})

    def no_ckan(*args, **kwargs):
        raise AssertionError("scoring must not read the live CKAN dataset record")

    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", no_ckan)
    monkeypatch.setattr(actions.loaders, "truth_from_pin", truth_from_pin)
    monkeypatch.setattr(actions.loaders, "fetch_run_params", lambda qid, base: {"history_length": 91})
    monkeypatch.setattr(actions.store, "list_entries", lambda bid: list(log["stored"]))

    def add(bid, entry):
        log["stored"].append(entry)
        return 42

    monkeypatch.setattr(actions.store, "add_entry", add)
    monkeypatch.setattr(actions.store, "remove_entry", lambda bid, eid: log["removed"].append(eid) or eid == 9)
    return log


def test_add_run_scores_against_the_pinned_commit_and_stores(world):
    assert actions.add_run(3, "Q10") == 42
    entry = world["stored"][0]
    assert world["truth_calls"] == [("Q1", "d.parquet", COMMIT)]
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
        raise AssertionError("no external call expected before the duplicate check")

    monkeypatch.setattr(actions.ckan_client, "list_model_runs", must_not_run)
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", must_not_run)
    monkeypatch.setattr(actions.loaders, "truth_from_pin", must_not_run)
    monkeypatch.setattr(actions, "_load_inputs", must_not_run)
    with pytest.raises(actions.Conflict):
        actions.add_run(3, "Q10")


def test_add_run_race_is_a_conflict(world, monkeypatch):
    def duplicate(bid, entry):
        raise actions.store.DuplicateEntry("exists")

    monkeypatch.setattr(actions.store, "add_entry", duplicate)
    with pytest.raises(actions.Conflict):
        actions.add_run(3, "Q10")


def test_add_run_stores_nothing_when_the_truth_cannot_be_read(world, monkeypatch):
    def boom(qid, component_name, commit, bench):
        raise RuntimeError("lakeFS down")

    monkeypatch.setattr(actions.loaders, "truth_from_pin", boom)
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


def test_remove_entry(world):
    actions.remove_entry(3, 9)
    with pytest.raises(actions.NotFound):
        actions.remove_entry(3, 10)
    assert world["removed"] == [9, 10]


def test_read_leaderboard_uses_only_the_database(world, monkeypatch):
    def external(*args, **kwargs):
        raise AssertionError("reads must not call CKAN, DOIP or lakeFS")

    monkeypatch.setattr(actions.ckan_client, "list_model_runs", external)
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", external)
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


def test_read_leaderboard_works_for_a_retired_benchmark(world, monkeypatch):
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "retired_at": "2026-10-01 00:00:00"})
    world["stored"].append(Entry("Q10", "a", "", {}, None, date(2021, 12, 31), "", 2, 2,
                                 {"mae": 1.0, "rmse": None, "coverage": None, "interval_score": None}, [],
                                 truth_as_of="2026-10-14"))
    board = actions.read_leaderboard(3, "mae")
    assert [e.run_qid for e in board.entries] == ["Q10"]


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


def test_add_run_reads_the_pinned_file_bytes_from_lakefs_not_the_catalog(world, monkeypatch):
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    reads = []
    buffer = io.BytesIO()
    pq.write_table(pa.table({"datum": ["2022-01-05", "2022-01-12"], "v": [10.0, 20.0]}), buffer)

    def read(commit, qid, filename, max_bytes):
        reads.append((commit, qid, filename, max_bytes))
        return buffer.getvalue()

    monkeypatch.setattr(actions.loaders, "truth_from_pin", _real_truth_from_pin)
    monkeypatch.setattr(actions.bstore, "get", lambda bid: dict(FINAL_ROW, component_name="pinned.parquet"))
    monkeypatch.setattr(actions.ckan_client, "list_model_runs", lambda: [RUN])
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", lambda: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(actions.ckan_client, "get_datastore_records",
                        lambda rid: [{"datum": "2022-01-05", "v": "11"}, {"datum": "2022-01-12", "v": "21"}])
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: [RunInput("Q1", "pinned.parquet", COMMIT, SQL)])
    monkeypatch.setattr(actions.store, "list_entries", lambda bid: [])
    monkeypatch.setattr(actions.store, "add_entry", lambda bid, entry: 1)
    monkeypatch.setattr(actions.loaders, "fetch_run_params", lambda qid, base: {})
    monkeypatch.setattr(actions.loaders.lakefs_client, "read_processed_component", read)
    assert actions.add_run(3, "Q10") == 1
    assert reads == [(COMMIT, "Q1", "pinned.parquet", actions.loaders.MAX_DOWNLOAD_BYTES)]


def test_add_run_with_the_pinned_file_missing_is_a_load_error(world, monkeypatch):
    def missing(*args):
        raise actions.loaders.NotFoundException(404, "nf", "")

    monkeypatch.setattr(actions.loaders, "truth_from_pin", _real_truth_from_pin)
    monkeypatch.setattr(actions.loaders.lakefs_client, "read_processed_component", missing)
    with pytest.raises(actions.LoadError, match="does not exist at the pinned commit"):
        actions.add_run(3, "Q10")
    assert world["stored"] == []


def test_read_leaderboard_hides_a_retired_draft_but_not_a_retired_final(world, monkeypatch):
    monkeypatch.setattr(actions.bstore, "get",
                        lambda bid: {**FINAL_ROW, "finalized_at": None, "data_commit": None, "retired_at": "t"})
    with pytest.raises(actions.NotFound):
        actions.read_leaderboard(3)
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "retired_at": "t"})
    assert actions.read_leaderboard(3).benchmark_id == 3
