"""Tests for the leaderboard write path with every external system faked."""

from datetime import date

import pandas as pd
import pytest

from app.leaderboard import actions
from app.leaderboard.run_inputs import RunInput
from app.leaderboard.scoring import NotScorable
from app.leaderboard.service import Entry

COMMIT = "0123abcd" * 8
_real_truth_from_pin = actions.loaders.truth_from_pin
DATES = pd.to_datetime(["2022-01-05", "2022-01-12"])
ROW = {"id": 3, "name": "Flu", "dataset_qid": "Q1", "target_column": "v", "date_column": "datum",
       "filters": None, "test_start": date(2022, 1, 1),
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


def test_add_run_refuses_a_run_whose_input_commit_differs(world, monkeypatch):
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: [RunInput("Q1", "d.parquet", "cd" * 32, SQL)])
    with pytest.raises(NotScorable, match="pinned to"):
        actions.add_run(3, "Q10")
    assert world["stored"] == []


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


def test_best_entries_ranks_by_interval_score_and_falls_back_to_mae(monkeypatch):
    from app.leaderboard import actions, store
    from app.leaderboard.service import Entry

    def entry(model, **metrics):
        return Entry(run_qid="", model_name=model, docker_tag="", params={}, history_start=None,
                     history_end=None, run_timestamp="", n_scored=0, n_expected=0,
                     metrics={"mae": None, "rmse": None, "coverage": None, "interval_score": None, **metrics})

    monkeypatch.setattr(store, "list_scores", lambda ids: {
        1: [entry("a", mae=1.0, interval_score=5.0), entry("b", mae=9.0, interval_score=3.0)],
        2: [entry("c", mae=2.0), entry("d", mae=1.5)],
        3: [entry("e")],
    })
    best = actions.best_entries([1, 2, 3])
    assert best[1] == {"model_name": "b", "metric": "interval_score", "value": 3.0}
    assert best[2] == {"model_name": "d", "metric": "mae", "value": 1.5}
    assert 3 not in best


MANIFEST = {"version": 1, "repo": "benchmarks", "branch": "main", "commit": "ee" * 32,
            "input": {"path": "3/input.parquet", "columns": ["datum", "v"], "first_date": "2021-01-06",
                      "last_date": "2021-12-29"},
            "reference": {"path": "3/reference.parquet", "sha256": "ab" * 32, "rows": 2}}
SOURCE = "lakefs://benchmarks/main/3/input.parquet?version=" + "ee" * 32
MATERIALIZED_RUN = {**RUN, "input_dataset_qids": [], "data_transformation_sql": [], "input_sources": [SOURCE]}


def test_a_benchmark_with_covariates_scores_against_its_reference_file_and_needs_no_provenance(world, monkeypatch):
    import json
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "materialized": json.dumps(MANIFEST)})
    monkeypatch.setattr(actions.ckan_client, "list_model_runs", lambda: [MATERIALIZED_RUN])
    monkeypatch.setattr(actions, "_load_inputs", lambda qid: (_ for _ in ()).throw(AssertionError("no provenance fetch")))
    monkeypatch.setattr(actions.loaders, "truth_from_pin", lambda *a: (_ for _ in ()).throw(AssertionError("not the data repo")))
    seen = []
    monkeypatch.setattr(actions.loaders, "truth_from_reference",
                        lambda manifest, bench: seen.append(manifest) or pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]}))
    assert actions.add_run(3, "Q10") == 42
    entry = world["stored"][0]
    assert seen == [MANIFEST] and entry.truth_commit == "ee" * 32                  # the benchmarks repo commit
    assert (entry.history_start, entry.history_end) == (date(2021, 1, 6), date(2021, 12, 29))
    candidates = actions.candidates(3)
    assert [(c["qid"], c["reason"]) for c in candidates] == [("Q10", "already on this leaderboard")]


def test_a_run_that_read_another_input_is_not_added_to_a_benchmark_with_covariates(world, monkeypatch):
    import json
    monkeypatch.setattr(actions.bstore, "get", lambda bid: {**FINAL_ROW, "materialized": json.dumps(MANIFEST)})
    other = {**MATERIALIZED_RUN, "input_sources": [SOURCE, "https://doip.example/doip/retrieve/Q1/d.parquet"]}
    monkeypatch.setattr(actions.ckan_client, "list_model_runs", lambda: [other])
    monkeypatch.setattr(actions.loaders, "truth_from_reference",
                        lambda manifest, bench: pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]}))
    from app.leaderboard.scoring import NotScorable
    with pytest.raises(NotScorable, match="exactly that file"):
        actions.add_run(3, "Q10")
    assert world["stored"] == []

