"""Tests for designing, checking and finalizing benchmarks with every external system faked."""

from datetime import date, timedelta
from io import BytesIO

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.benchmarks import actions
from app.benchmarks.definition import DefinitionError
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


def _row(**overrides):
    base = {"id": 3, "name": "Flu", "description": None, "dataset_qid": "Q1", "target_column": "v",
            "date_column": "datum", "filter_column": "typ", "filter_value": "Influenza A+B",
            "test_start": date(2026, 4, 1), "test_end": date(2026, 9, 30), "history_start": None,
            "history_end": None, "input_filename": "input.parquet", "component_name": None,
            "prediction_length": None, "truth_points": None, "data_commit": None, "created_by": 7,
            "created_at": "2026-10-16 10:00:00", "finalized_at": None, "retired_at": None, "entry_count": 0}
    return {**base, **overrides}


@pytest.fixture
def world(monkeypatch):
    log = {"created": [], "updated": [], "finalized": [], "retired": [], "reads": []}
    state = {"row": _row(), "data": _parquet()}
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", lambda: [DATASET])
    monkeypatch.setattr(actions.lakefs_client, "processed_head_commit", lambda: COMMIT)

    def read(commit, qid, filename, max_bytes):
        log["reads"].append((commit, qid, filename))
        return state["data"]

    monkeypatch.setattr(actions.lakefs_client, "read_processed_component", read)
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
                test_start=date(2026, 4, 1), test_end=date(2026, 9, 30), filter_column="typ",
                filter_value="Influenza A+B")
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
    actions.update_draft(3, name="Renamed", history_start=date(2025, 12, 3), filter_column=None, filter_value=None)
    bid, name, desc, b = world["log"]["updated"][0]
    assert (bid, name) == (3, "Renamed") and b.history_start == date(2025, 12, 3)
    assert b.filter_column is None and b.target_column == "v" and b.test_end == date(2026, 9, 30)


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
    assert "<= DATE '2026-03-25'" in report.input_sql and "\"typ\" = 'Influenza A+B'" in report.input_sql
    assert world["log"]["reads"] == [(COMMIT, "Q1", "d.parquet")]


def test_check_reports_data_problems_instead_of_raising(world):
    world["state"]["row"] = _row(test_start=date(2030, 1, 1), test_end=date(2030, 2, 1))
    report = actions.check(3)
    assert not report.ok and any("no observations" in p for p in report.problems)


def test_check_flags_a_filter_value_the_qualification_rules_cannot_accept(world):
    world["state"]["data"] = _parquet(typ="NOT AVAILABLE")
    world["state"]["row"] = _row(filter_value="NOT AVAILABLE")
    report = actions.check(3)
    assert not report.ok and any("qualification rules" in p for p in report.problems)


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
                               "prediction_length": 27, "history_end": date(2026, 3, 25)}


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


def test_run_spec_for_a_final_benchmark_pins_the_input_url(world, monkeypatch):
    world["state"]["row"] = _final()
    monkeypatch.setattr(actions.settings, "doip_url", "https://doip.example/")
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", lambda: (_ for _ in ()).throw(AssertionError("no CKAN call")))
    spec = actions.run_spec(3)
    assert spec["input_data_files"] == [[f"https://doip.example/doip/retrieve/Q1/d.parquet?version={COMMIT}", "input.parquet"]]
    assert spec["data_transformation_sql"][0].endswith('ORDER BY "datum"') and "<= DATE '2026-03-25'" in spec["data_transformation_sql"][0]
    assert spec["config"] == {"prediction_length": 27, "prediction_offset": 0}
    assert (spec["data_commit"], spec["test_start"], spec["test_end"]) == (COMMIT, "2026-04-01", "2026-09-30")


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
    assert not any("qualification rules" in p for p in report.problems)


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

