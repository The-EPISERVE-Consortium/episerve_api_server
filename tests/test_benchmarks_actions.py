"""Tests for designing, checking and finalizing benchmarks with every external system faked."""

import json
from datetime import date, timedelta
from io import BytesIO

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.benchmarks import actions
from app.benchmarks.definition import DefinitionError, input_uri
from app.leaderboard.loaders import LoadError
from tests.fakes import make_user

COMMIT = "0123abcd" * 8
CREATOR = make_user(7, ("benchmarks",), email="creator@example.org")
OTHER = make_user(8, ("benchmarks",), email="other@example.org")
ADMIN_USER = make_user(9, ("admin",), email="admin@example.org")
DATASET = {"qid": "Q1", "name": "Wastewater", "last_modified": "2026-10-14",
           "components": [{"name": "d.parquet", "url": "https://doip.example/doip/retrieve/Q1/d.parquet"}]}


def _parquet(typ="Influenza A+B"):
    dates = [date(2025, 10, 1) + timedelta(days=7 * i) for i in range(53)]         # weekly Wednesdays
    frame = pd.DataFrame({"datum": [d.isoformat() for d in dates], "v": [float(i) for i in range(53)], "typ": typ})
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame), buffer)
    return buffer.getvalue()


WEATHER_DATASET = {"qid": "Q2", "name": "Weather", "last_modified": "2026-10-14",
                   "components": [{"name": "w.parquet", "url": "https://doip.example/doip/retrieve/Q2/w.parquet"}]}


def _weather():
    days = [date(2025, 10, 1) + timedelta(days=i) for i in range(400)]
    frame = pd.DataFrame({"day": [d.isoformat() for d in days], "temp": [float(i) for i in range(400)]})
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame), buffer)
    return buffer.getvalue()


COVARIATES = [{"name": "wetter", "dataset_qid": "Q2", "date_column": "day", "columns": ["temp"],
               "aggregation": "mean"}]


def _row(**overrides):
    base = {"id": 3, "name": "Flu", "description": None, "dataset_qid": "Q1", "target_column": "v",
            "date_column": "datum", "filters": '[["typ", "Influenza A+B"]]',
            "test_start": date(2026, 4, 1), "test_end": date(2026, 9, 30), "history_start": None,
            "history_end": None, "input_filename": "input.parquet", "component_name": None,
            "prediction_length": None, "truth_points": None, "data_commit": None, "created_by": 7,
            "created_at": "2026-10-16 10:00:00", "finalized_at": None, "retired_at": None, "entry_count": 0}
    return {**base, **overrides}


@pytest.fixture
def world(monkeypatch):
    log = {"created": [], "updated": [], "finalized": [], "retired": [], "reads": []}
    state = {"row": _row(), "data": _parquet(), "weather": _weather()}
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", lambda: [DATASET, WEATHER_DATASET])
    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", lambda: COMMIT)

    def read(commit, qid, filename, max_bytes):
        log["reads"].append((commit, qid, filename))
        return state["weather"] if qid == "Q2" else state["data"]

    monkeypatch.setattr(actions.lakefs_client, "read_processed_component", read)
    log["written"] = []

    def write(files, message):
        log["written"].append((files, message))
        return "main", "ee" * 32

    monkeypatch.setattr(actions.lakefs_client, "write_benchmark_objects", write)
    monkeypatch.setattr(actions.store, "get", lambda bid: dict(state["row"]) if bid == 3 else None)
    monkeypatch.setattr(actions.store, "create_draft",
                        lambda name, desc, b, created_by=None: log["created"].append((name, desc, b, created_by)) or 8)

    def update(bid, name, desc, b):
        log["updated"].append((bid, name, desc, b))
        return True

    monkeypatch.setattr(actions.store, "update_draft", update)
    monkeypatch.setattr(actions.store, "finalize", lambda bid, **kw: log["finalized"].append((bid, kw)) or True)
    monkeypatch.setattr(actions.store, "retire", lambda bid: log["retired"].append(bid) or True)
    return {"log": log, "state": state}


def _create(**kw):
    args = dict(name=" Flu 2026 ", description=None, dataset_qid="Q1", target_column="v", date_column="datum",
                test_start=date(2026, 4, 1), test_end=date(2026, 9, 30), filters=[("typ", "Influenza A+B")])
    return actions.create_draft(**{**args, **kw})


def test_create_draft_validates_and_stores_a_trimmed_name_and_the_creator(world):
    assert _create(created_by=CREATOR.id) == 8
    name, _, benchmark, creator = world["log"]["created"][0]
    assert name == "Flu 2026" and benchmark.dataset_qid == "Q1" and benchmark.history_end is None
    assert creator == CREATOR.id


def test_create_draft_rejects_invalid_definitions_and_unknown_datasets(world):
    with pytest.raises(DefinitionError, match="test start"):
        _create(test_start=date(2026, 10, 1))
    with pytest.raises(actions.NotFound):
        _create(dataset_qid="Q404")
    assert world["log"]["created"] == []


def test_update_draft_merges_only_the_given_fields(world):
    actions.update_draft(3, name="Renamed", history_start=date(2025, 12, 3), filters=None)
    bid, name, desc, b = world["log"]["updated"][0]
    assert (bid, name) == (3, "Renamed") and b.history_start == date(2025, 12, 3)
    assert b.filters == () and b.target_column == "v" and b.test_end == date(2026, 9, 30)


def test_update_draft_refuses_final_unknown_and_invalid(world):
    with pytest.raises(actions.NotFound):
        actions.update_draft(99, name="x")
    with pytest.raises(DefinitionError):
        actions.update_draft(3, history_end=date(2026, 4, 5))
    assert world["log"]["updated"] == []
    world["state"]["row"] = _row(finalized_at="2026-10-16 11:00:00", data_commit=COMMIT)
    with pytest.raises(actions.NotDraft):
        actions.update_draft(3, name="x")


def test_check_resolves_the_defaults_and_builds_the_run_inputs(world):
    report = actions.check(3)
    assert report.ok and report.problems == []
    assert (report.data_commit, report.component_name, report.truth_points) == (COMMIT, "d.parquet", 27)
    assert report.history_end == date(2026, 3, 25) and report.last_history_date == date(2026, 3, 25)
    assert (report.prediction_length, report.frequency_days) == (27, 7)
    assert report.columns == ["datum", "v"] and report.input_rows == 26     # history only, the target series
    assert world["log"]["reads"] == [(COMMIT, "Q1", "d.parquet")]


def test_check_reports_data_problems_instead_of_raising(world):
    world["state"]["row"] = _row(test_start=date(2030, 1, 1), test_end=date(2030, 2, 1))
    report = actions.check(3)
    assert not report.ok and any("no observations" in p for p in report.problems)


def test_check_only_works_on_drafts(world):
    with pytest.raises(actions.NotFound):
        actions.check(99)
    world["state"]["row"] = _row(finalized_at="t", data_commit=COMMIT)
    with pytest.raises(actions.NotDraft):
        actions.check(3)


def test_finalize_pins_the_commit_read_during_the_check(world):
    actions.finalize(3, by=CREATOR)
    bid, kw = world["log"]["finalized"][0]
    assert bid == 3 and kw == {"data_commit": COMMIT, "component_name": "d.parquet", "truth_points": 27,
                               "prediction_length": 27, "history_end": date(2026, 3, 25),
                               "materialized": kw["materialized"]}
    manifest = kw["materialized"]
    assert manifest["input"]["columns"] == ["datum", "v"] and manifest["reference"]["rows"] == 27
    assert manifest["commit"] == "ee" * 32


def test_finalize_with_a_different_expected_commit_conflicts_and_stores_nothing(world):
    with pytest.raises(actions.Conflict, match="checked deadbeefdead, now 0123abcd0123"):
        actions.finalize(3, expected_commit="deadbeef" * 8, by=CREATOR)
    assert world["log"]["finalized"] == [] and world["log"]["reads"] == []


def test_finalize_stores_nothing_when_the_check_finds_problems_or_lakefs_fails(world, monkeypatch):
    world["state"]["row"] = _row(test_start=date(2030, 1, 1), test_end=date(2030, 2, 1))
    with pytest.raises(LoadError, match="no observations"):
        actions.finalize(3, by=CREATOR)
    world["state"]["row"] = _row()

    def boom():
        raise RuntimeError("lakeFS down")

    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", boom)
    with pytest.raises(RuntimeError):
        actions.finalize(3, by=CREATOR)
    assert world["log"]["finalized"] == []


def test_finalize_refuses_a_final_benchmark(world):
    world["state"]["row"] = _row(finalized_at="t", data_commit=COMMIT)
    with pytest.raises(actions.NotDraft):
        actions.finalize(3, by=CREATOR)


def _final(**kw):
    return _row(finalized_at="2026-10-16 11:00:00", data_commit=COMMIT, component_name="d.parquet",
                history_end=date(2026, 3, 25), prediction_length=27, truth_points=27, **kw)


MANIFEST = {"version": 2, "repo": "benchmarks", "branch": "main", "commit": "ee" * 32,
            "input": {"qid": "Q1791000000001", "component": "input.parquet", "columns": ["datum", "v"],
                      "first_date": "2025-10-01", "last_date": "2026-03-25"},
            "reference": {"qid": "Q1791000000002", "component": "reference.parquet", "sha256": "ab" * 32, "rows": 27}}


def test_run_spec_for_a_final_benchmark_names_the_generated_input_file(world, monkeypatch):
    world["state"]["row"] = _final(materialized=json.dumps(MANIFEST))
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", lambda: (_ for _ in ()).throw(AssertionError("no CKAN call")))
    spec = actions.run_spec(3)
    assert spec["input_data_files"] == [[input_uri(MANIFEST), "input.parquet"]]
    assert "/doip/retrieve/Q1791000000001/input.parquet?version=" + "ee" * 32 in spec["input_data_files"][0][0]
    assert spec["data_transformation_sql"] == [""] and spec["input_columns"] == ["datum", "v"]
    assert spec["config"] == {"prediction_length": 27, "prediction_offset": 0}
    assert (spec["data_commit"], spec["test_start"], spec["test_end"]) == (COMMIT, "2026-04-01", "2026-09-30")


def test_run_spec_needs_the_generated_files(world):
    world["state"]["row"] = _final()
    with pytest.raises(actions.NotFound, match="generated input file"):
        actions.run_spec(3)


def test_a_final_benchmark_without_files_gets_them_at_its_pinned_commit(world, monkeypatch):
    world["state"]["row"] = _final()
    saved = {}
    monkeypatch.setattr(actions.store, "set_materialized", lambda bid, m: saved.update(id=bid, manifest=m))
    manifest = actions.materialize_final(3)
    assert saved == {"id": 3, "manifest": manifest} and manifest["reference"]["rows"] == 27
    assert world["log"]["reads"] == [(COMMIT, "Q1", "d.parquet")] and world["log"]["finalized"] == []
    world["state"]["row"] = _final(materialized=json.dumps({**MANIFEST, "version": 2}))
    assert actions.materialize_final(3)["version"] == 3                         # an older layout is replaced
    world["state"]["row"] = _final(materialized=json.dumps({**MANIFEST, "version": 3}))
    with pytest.raises(actions.Conflict):
        actions.materialize_final(3)


def test_run_spec_only_for_final_benchmarks(world):
    with pytest.raises(actions.NotFound):
        actions.run_spec(3)                                  # draft
    world["state"]["row"] = _final(retired_at="t")
    with pytest.raises(actions.NotFound):
        actions.run_spec(3)                                  # retired
    with pytest.raises(actions.NotFound):
        actions.run_spec(99)


def test_get_hides_drafts_unless_asked(world):
    with pytest.raises(actions.NotFound):
        actions.get(3)
    assert actions.get(3, include_draft=True)["id"] == 3
    world["state"]["row"] = _final()
    assert actions.get(3)["id"] == 3


def test_copy_as_draft_keeps_the_definition_with_a_new_name(world):
    world["state"]["row"] = _final(name="N" * 195, history_start=date(2025, 12, 3))
    assert actions.copy_as_draft(3, created_by=OTHER.id) == 8
    name, _, b, creator = world["log"]["created"][0]
    assert creator == OTHER.id                              # the copy belongs to whoever copied it
    assert name.endswith("(copy)") and len(name) <= 200
    assert b.history_start == date(2025, 12, 3) and b.data_commit is None and b.prediction_length is None
    with pytest.raises(actions.NotFound):
        actions.copy_as_draft(99)


def test_finalize_conflicts_when_the_draft_changes_meanwhile(world, monkeypatch):
    rows = iter([_row(), _row(name="Other")])
    monkeypatch.setattr(actions.store, "get", lambda bid: next(rows))
    with pytest.raises(actions.Conflict):
        actions.finalize(3, by=CREATOR)
    assert world["log"]["finalized"] == []


def test_get_hides_a_retired_draft_from_anonymous_readers(world):
    world["state"]["row"] = _row(retired_at="2026-10-17 00:00:00")        # finalized_at stays NULL
    with pytest.raises(actions.NotFound):
        actions.get(3)
    assert actions.get(3, include_draft=True)["id"] == 3
    world["state"]["row"] = _final(retired_at="2026-10-17 00:00:00")
    assert actions.get(3)["id"] == 3                                      # retired finals stay readable


def test_check_rejects_a_history_start_after_the_last_observation(world):
    world["state"]["row"] = _row(history_end=date(2026, 3, 25), history_start=date(2026, 3, 30))
    report = actions.check(3)
    assert not report.ok and any("no observations in the input range" in p for p in report.problems)
    world["state"]["row"] = _row(history_start=date(2026, 3, 30))
    report = actions.check(3)
    assert not report.ok and any("no observations in the input range" in p for p in report.problems)


def test_only_the_creator_may_finalize_a_draft(world):
    with pytest.raises(actions.Forbidden, match="Only the creator"):
        actions.finalize(3, by=OTHER)
    with pytest.raises(actions.Forbidden):
        actions.finalize(3, by=ADMIN_USER)                    # not even an admin, if the draft has a creator
    assert world["log"]["finalized"] == [] and world["log"]["reads"] == []
    actions.finalize(3, by=CREATOR)
    assert len(world["log"]["finalized"]) == 1


def test_a_draft_without_a_recorded_creator_can_only_be_finalized_by_an_admin(world):
    world["state"]["row"] = _row(created_by=None)
    with pytest.raises(actions.Forbidden, match="no recorded creator"):
        actions.finalize(3, by=CREATOR)
    actions.finalize(3, by=ADMIN_USER)
    assert len(world["log"]["finalized"]) == 1


def test_create_and_update_a_draft_with_several_filters(world):
    _create(filters=[("typ", "Influenza A+B"), ("age", "35-59")], created_by=CREATOR.id)
    assert world["log"]["created"][0][2].filters == (("typ", "Influenza A+B"), ("age", "35-59"))
    actions.update_draft(3, filters=[{"column": "typ", "value": "x"}, {"column": "age", "value": "1"}])
    assert world["log"]["updated"][0][3].filters == (("typ", "x"), ("age", "1"))
    actions.update_draft(3, filters=[])
    assert world["log"]["updated"][1][3].filters == ()
    actions.update_draft(3, filters=None)                                  # null removes them as well
    assert world["log"]["updated"][2][3].filters == ()


def test_check_builds_the_input_of_a_benchmark_with_covariates_and_reports_the_columns(world):
    world["state"]["row"] = _row(covariates=json.dumps(COVARIATES))
    report = actions.check(3)
    assert report.ok and report.columns == ["datum", "v", "wetter__temp"]
    assert report.input_rows == 26 and report.missing == {"wetter__temp": 0.0}
    assert ("0123abcd" * 8, "Q2", "w.parquet") in world["log"]["reads"]    # the covariate is read at the same commit


def test_check_reports_a_covariate_that_cannot_be_used(world):
    bad = [{**COVARIATES[0], "columns": ["nope"]}]
    world["state"]["row"] = _row(covariates=json.dumps(bad))
    report = actions.check(3)
    assert not report.ok and any("no column 'nope'" in p for p in report.problems)
    world["state"]["row"] = _row(covariates=json.dumps([{**COVARIATES[0], "dataset_qid": "Q404"}]))
    assert any("Q404" in p for p in actions.check(3).problems)


def test_finalize_stores_one_fdo_object_with_two_components_and_records_the_manifest(world):
    world["state"]["row"] = _row(covariates=json.dumps(COVARIATES))
    actions.finalize(3, by=CREATOR)
    files, _ = world["log"]["written"][0]
    _, kw = world["log"]["finalized"][0]
    manifest = kw["materialized"]
    assert manifest["version"] == 3 and manifest["repo"] == "benchmarks" and manifest["commit"] == "ee" * 32
    qid = manifest["qid"]
    assert manifest["input"]["qid"] == manifest["reference"]["qid"] == qid       # one object, two components
    assert manifest["input"]["columns"] == ["datum", "v", "wetter__temp"]
    assert sorted(f.rsplit("/", 1)[-1] for f in files) == sorted([f"{qid}.fdo.json", "input.parquet", "reference.parquet"])
    assert len(manifest["input"]["sha256"]) == 64 and manifest["reference"]["rows"] == 27
    description = json.loads(files[f"{actions.fdo.shard(qid)}/{qid}.fdo.json"])
    assert description["@id"] == qid and description["profile"]["accessRights"] == "open"
    assert sorted(description["profile"]["episerveBenchmark"]["files"]) == ["input", "reference"]
    assert description["profile"]["episerveBenchmark"]["source_qids"] == ["Q1", "Q2"]
    assert description["profile"]["episerveBenchmark"]["data_commit"] == COMMIT
    info = manifest["recipe"]                                             # kept with the benchmark, as it ran
    assert info["commit"] == COMMIT and [s["role"] for s in info["sources"]] == ["target", "wetter"]
    assert len(info["sql"]) == 3 and [m["covariate"] for m in info["merge"]] == ["wetter"]


def test_a_restricted_source_dataset_blocks_the_benchmark(world, monkeypatch):
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets",
                        lambda: [{**DATASET, "access": "restricted"}, WEATHER_DATASET])
    report = actions.check(3)
    assert not report.ok and "restricted" in report.problems[0]
    world["state"]["row"] = _row(covariates=json.dumps(COVARIATES))
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets",
                        lambda: [DATASET, {**WEATHER_DATASET, "access": "restricted"}])
    report = actions.check(3)
    assert not report.ok and any("restricted" in p for p in report.problems)


def test_nothing_is_stored_when_the_covariates_cannot_be_built(world, monkeypatch):
    monkeypatch.setattr(actions.lakefs_client, "write_benchmark_objects",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not write")))
    world["state"]["row"] = _row(covariates=json.dumps([{**COVARIATES[0], "columns": ["nope"]}]))
    with pytest.raises(LoadError, match="no column 'nope'"):
        actions.finalize(3, by=CREATOR)
    assert world["log"]["finalized"] == []


def test_run_spec_of_a_benchmark_with_covariates_points_at_the_generated_file_and_needs_no_sql(world):
    manifest = {**MANIFEST, "input": {**MANIFEST["input"], "columns": ["datum", "v", "wetter__temp"]}}
    world["state"]["row"] = {**_final(covariates=json.dumps(COVARIATES)), "materialized": json.dumps(manifest)}
    spec = actions.run_spec(3)
    assert spec["input_data_files"] == [[input_uri(manifest), "input.parquet"]]
    assert spec["data_transformation_sql"] == [""]
    assert spec["input_columns"] == ["datum", "v", "wetter__temp"] and spec["covariate_columns"] == ["wetter__temp"]
    assert spec["target_column"] == "v" and spec["config"]["prediction_length"] == 27


def test_create_and_update_a_draft_with_covariates(world):
    _create(covariates=COVARIATES, created_by=CREATOR.id)
    cov = world["log"]["created"][0][2].covariates
    assert [(c.name, c.dataset_qid, c.columns, c.aggregation) for c in cov] == [("wetter", "Q2", ("temp",), "mean")]
    actions.update_draft(3, covariates=[{**COVARIATES[0], "name": "w2", "columns": "temp, rain"}])
    assert world["log"]["updated"][0][3].covariates[0].columns == ("temp", "rain")
    actions.update_draft(3, covariates=None)
    assert world["log"]["updated"][1][3].covariates == ()
    with pytest.raises(actions.NotFound):
        _create(covariates=[{**COVARIATES[0], "dataset_qid": "Q404"}])
    with pytest.raises(DefinitionError, match="aggregation"):
        _create(covariates=[{**COVARIATES[0], "aggregation": "median"}])


def test_the_recipe_can_be_stored_for_a_benchmark_finalized_earlier(world, monkeypatch):
    world["state"]["row"] = _final(materialized=json.dumps({**MANIFEST, "qid": "Q1791000000001"}))
    saved = {}
    monkeypatch.setattr(actions.store, "set_materialized", lambda bid, m: saved.update(id=bid, manifest=m))
    manifest = actions.store_recipe(3)
    assert saved["id"] == 3 and manifest["sources"] == [{"role": "target", "qid": "Q1", "component": "d.parquet"}]
    assert len(manifest["recipe"]["sql"]) == 3 and manifest["recipe"]["merge"] == []
    world["state"]["row"] = _final()
    with pytest.raises(actions.NotFound):
        actions.store_recipe(3)
