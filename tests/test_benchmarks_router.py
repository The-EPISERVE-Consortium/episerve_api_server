"""Tests for the /benchmarks endpoints with actions and store faked."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN
from tests.fakes import bearer, install_users, make_user
from app.clients import lakefs as lakefs_client
from app.leaderboard import actions
from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable
from app.leaderboard.service import Entry, Leaderboard
from app.main import app
from app.routers import benchmarks as router

client = TestClient(app)
AUTH = bearer(ADMIN)
COMMIT = "0123abcd" * 8
ROW = {"id": 3, "name": "Flu", "description": None, "dataset_qid": "Q1", "target_column": "v",
       "date_column": "datum", "filters": None, "test_start": date(2022, 1, 1),
       "test_end": date(2022, 1, 31), "history_start": None, "history_end": date(2021, 12, 31),
       "input_filename": "input.parquet", "component_name": "d.parquet", "prediction_length": 31,
       "truth_points": 2, "data_commit": COMMIT, "created_at": "2026-10-16 10:00:00",
       "finalized_at": "2026-10-16 11:00:00", "retired_at": None, "entry_count": 1}
FINAL = ROW
DRAFT = {**ROW, "id": 4, "finalized_at": None, "data_commit": None, "truth_points": None,
         "prediction_length": None, "component_name": None, "entry_count": 0}
BODY = {"name": "Flu", "dataset_qid": "Q1", "target_column": "v", "date_column": "datum",
        "test_start": "2022-01-01", "test_end": "2022-01-31"}


def _report():
    return router.bactions.CheckReport(
        ok=True, problems=[], warnings=[], data_commit=COMMIT, component_name="d.parquet", truth_points=27,
        history_end=date(2026, 3, 25), last_history_date=date(2026, 3, 25), prediction_length=27,
        frequency_days=7, input_sql="SELECT 1")


def _board():
    entry = Entry("Q10", "timesfm", "latest", {"history_length": 91}, None, date(2021, 12, 31), "t", 2, 2,
                  {"mae": 1.0, "rmse": 1.0, "coverage": 1.0, "interval_score": None},
                  [{"date": "2022-01-05", "forecast": 11.0, "q10": None, "q90": None, "truth": 10.0}],
                  entry_id=9, truth_commit="0123abcd" * 8, truth_as_of="2026-10-14", scored_at="2026-10-16 10:00:00")
    return Leaderboard(Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 1, 31)), "mae", 2,
                       "2026-10-14", [entry], name="Flu", benchmark_id=3)


@pytest.fixture(autouse=True)
def reads_are_database_only(monkeypatch):
    def external(*args, **kwargs):
        raise AssertionError("read endpoints must not call CKAN, DOIP or lakeFS")

    monkeypatch.setattr(actions.ckan_client, "list_model_runs", external)
    monkeypatch.setattr(actions.ckan_client, "list_processed_datasets", external)
    monkeypatch.setattr(lakefs_client, "processed_head_commit", external)


def test_leaderboard_returns_ranked_entries_with_pins(monkeypatch):
    monkeypatch.setattr(router.bactions, "get", lambda bid, include_draft=False: ROW)
    monkeypatch.setattr(router.lactions, "read_leaderboard", lambda bid, rank_by, best_per_model: _board())
    body = client.get("/benchmarks/3/leaderboard", params={"rank_by": "mae"}).json()
    assert body["best"] == "timesfm" and body["benchmark"]["id"] == 3
    entry = body["entries"][0]
    assert entry["rank"] == 1 and entry["entry_id"] == 9
    assert entry["truth_commit"] == "0123abcd" * 8 and entry["history_end"] == "2021-12-31"
    assert entry["metrics"]["interval_score"] is None


@pytest.mark.parametrize("method,path,kwargs", [
    ("post", "/benchmarks", {"json": BODY}),
    ("post", "/benchmarks/3/entries", {"json": {"run_qid": "Q10"}}),
    ("delete", "/benchmarks/3/entries/9", {}),
    ("post", "/benchmarks/3/retire", {}),
    ("patch", "/benchmarks/3", {"json": {"name": "x"}}),
    ("post", "/benchmarks/3/check", {}),
    ("post", "/benchmarks/3/finalize", {}),
    ("post", "/benchmarks/3/copy", {}),
])
def test_writes_require_the_bearer_token(monkeypatch, method, path, kwargs):
    def must_not_run(*args, **kw):
        raise AssertionError("no work before authentication")

    for name in ("create_draft", "update_draft", "check", "finalize", "copy_as_draft", "retire"):
        monkeypatch.setattr(router.bactions, name, must_not_run)
    for name in ("add_run", "remove_entry"):
        monkeypatch.setattr(router.lactions, name, must_not_run)
    assert getattr(client, method)(path, **kwargs).status_code in (401, 403)
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}, **kwargs).status_code == 401


@pytest.mark.parametrize("error,status", [
    (actions.NotFound("x"), 404), (actions.Conflict("x"), 409), (LoadError("x"), 422),
    (NotScorable("because"), 422), (RuntimeError("lakeFS down"), 502),
])
def test_write_errors_map_to_status_codes(monkeypatch, error, status):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(router.lactions, "add_run", fail)
    resp = client.post("/benchmarks/3/entries", json={"run_qid": "Q10"}, headers=AUTH)
    assert resp.status_code == status
    if status in (404, 409, 422):
        assert resp.json()["detail"] == str(error)


def test_unexpected_errors_are_logged_and_not_leaked(monkeypatch, caplog):
    secret = "Can't connect to MySQL server on 'db.internal'"

    def boom():
        raise RuntimeError(secret)

    monkeypatch.setattr(router.bactions, "list_final", boom)
    with caplog.at_level("ERROR"):
        resp = client.get("/benchmarks")
    assert resp.status_code == 502
    assert "db.internal" not in resp.text and "MySQL" not in resp.text
    assert resp.json()["detail"] == "The benchmarks database or data storage is unavailable."
    assert any(secret in (r.exc_text or "") or secret in r.getMessage() for r in caplog.records)


def test_list_shows_final_benchmarks_only_and_drafts_need_the_token(monkeypatch):
    monkeypatch.setattr(router.bactions, "list_final", lambda: [FINAL])
    monkeypatch.setattr(router.bactions, "list_drafts", lambda: [DRAFT])
    listed = client.get("/benchmarks").json()
    assert [b["state"] for b in listed] == ["final"] and listed[0]["data_commit"] == COMMIT
    assert client.get("/benchmarks/drafts").status_code in (401, 403)
    drafts = client.get("/benchmarks/drafts", headers=AUTH).json()
    assert [b["state"] for b in drafts] == ["draft"]


def test_a_draft_is_hidden_from_anonymous_readers(monkeypatch):
    def get(bid, include_draft=False):
        if not include_draft:
            raise router.bactions.NotFound("hidden")
        return DRAFT

    monkeypatch.setattr(router.bactions, "get", get)
    assert client.get("/benchmarks/4").status_code == 404
    assert client.get("/benchmarks/4", headers=AUTH).json()["state"] == "draft"


def test_a_retired_draft_is_hidden_from_anonymous_readers(monkeypatch):
    retired_draft = {**DRAFT, "retired_at": "2026-10-17 00:00:00"}
    monkeypatch.setattr(router.bactions, "_row", lambda bid: retired_draft)
    assert client.get("/benchmarks/4").status_code == 404
    assert client.get("/benchmarks/4", headers=AUTH).json()["state"] == "retired"


def test_run_spec_is_public_for_final_benchmarks(monkeypatch):
    spec = {"benchmark_id": 3, "dataset_qid": "Q1", "data_commit": COMMIT,
            "input_data_files": [[f"https://doip.example/doip/retrieve/Q1/d.parquet?version={COMMIT}", "input.parquet"]],
            "data_transformation_sql": ["SELECT 1"], "config": {"prediction_length": 27, "prediction_offset": 0},
            "test_start": "2026-04-01", "test_end": "2026-09-30"}
    monkeypatch.setattr(router.bactions, "run_spec", lambda bid: spec)
    assert client.get("/benchmarks/3/run-spec").json() == spec

    def missing(bid):
        raise router.bactions.NotFound("not final")

    monkeypatch.setattr(router.bactions, "run_spec", missing)
    assert client.get("/benchmarks/3/run-spec").status_code == 404


def test_create_patch_check_finalize_copy_and_retire(monkeypatch):
    calls = []
    monkeypatch.setattr(router.bactions, "create_draft", lambda **kw: calls.append(("create", kw)) or 4)
    monkeypatch.setattr(router.bactions, "update_draft", lambda bid, **kw: calls.append(("patch", bid, kw)))
    monkeypatch.setattr(router.bactions, "finalize", lambda bid, by: calls.append(("finalize", bid, by.id)))
    monkeypatch.setattr(router.bactions, "copy_as_draft", lambda bid, created_by: calls.append(("copy", bid, created_by)) or 5)
    monkeypatch.setattr(router.bactions, "retire", lambda bid: calls.append(("retire", bid)))
    monkeypatch.setattr(router.bactions, "get", lambda bid, include_draft=False: DRAFT)
    monkeypatch.setattr(router.bactions, "check", lambda bid: _report())
    created = client.post("/benchmarks", json={**BODY, "description": "d", "history_end": "2026-03-25"}, headers=AUTH)
    assert created.status_code == 201 and calls[0][1]["history_end"] == date(2026, 3, 25)
    assert client.patch("/benchmarks/4", json={"name": "New", "history_start": None}, headers=AUTH).status_code == 200
    assert calls[1] == ("patch", 4, {"name": "New", "history_start": None})        # only the given fields
    report = client.post("/benchmarks/4/check", headers=AUTH).json()
    assert report["ok"] is True and report["prediction_length"] == 27 and report["history_end"] == "2026-03-25"
    assert client.post("/benchmarks/4/finalize", headers=AUTH).status_code == 200
    assert client.post("/benchmarks/4/copy", headers=AUTH).status_code == 201
    assert client.post("/benchmarks/4/retire", headers=AUTH).status_code == 204
    assert [c[0] for c in calls] == ["create", "patch", "finalize", "copy", "retire"]
    assert calls[0][1]["created_by"] == ADMIN.id                                    # the user is recorded as the creator
    assert calls[2] == ("finalize", 4, ADMIN.id) and calls[3] == ("copy", 4, ADMIN.id)


def test_only_an_admin_changes_the_description_of_any_benchmark(monkeypatch):
    saved = []
    monkeypatch.setattr(router.bactions, "set_description", lambda bid, text: saved.append((bid, text)))
    monkeypatch.setattr(router.bactions, "get", lambda bid, include_draft=False: FINAL)
    body = {"description": "Hello"}
    assert client.put("/benchmarks/3/description", json=body).status_code == 401
    member = make_user(5, ("benchmarks",), email="m@example.org")
    install_users(monkeypatch, ADMIN, member)
    assert client.put("/benchmarks/3/description", json=body, headers=bearer(member)).status_code == 403
    assert client.put("/benchmarks/3/description", json=body, headers=AUTH).status_code == 200
    assert saved == [(3, "Hello")]


def test_finalizing_a_draft_of_someone_else_is_forbidden(monkeypatch):
    def refuse(bid, by):
        raise router.bactions.Forbidden("Only the creator of this draft can finalize it.")

    monkeypatch.setattr(router.bactions, "finalize", refuse)
    resp = client.post("/benchmarks/4/finalize", headers=AUTH)
    assert resp.status_code == 403 and "Only the creator" in resp.json()["detail"]


def test_a_draft_can_be_created_with_a_list_of_filters_and_the_answer_lists_them(monkeypatch):
    calls = []
    monkeypatch.setattr(router.bactions, "create_draft", lambda **kw: calls.append(kw) or 4)
    row = {**DRAFT, "filters": '[["Bundesland_id", "5"], ["Altersgruppe", "35-59"]]'}
    monkeypatch.setattr(router.bactions, "get", lambda bid, include_draft=False: row)
    body = {**BODY, "filters": [{"column": "Bundesland_id", "value": "5"}, {"column": "Altersgruppe", "value": "35-59"}]}
    created = client.post("/benchmarks", json=body, headers=AUTH)
    assert created.status_code == 201
    assert [(f.column, f.value) for f in calls[0]["filters"]] == [("Bundesland_id", "5"), ("Altersgruppe", "35-59")]
    out = created.json()
    assert out["filters"] == [{"column": "Bundesland_id", "value": "5"}, {"column": "Altersgruppe", "value": "35-59"}]
    assert out["filter_column"] == "Bundesland_id"                     # the first one stays visible for older clients
    # the older single filter fields still work
    client.post("/benchmarks", json={**BODY, "filter_column": "typ", "filter_value": "a"}, headers=AUTH)
    assert calls[1]["filters"] == [{"column": "typ", "value": "a"}]


def test_patch_accepts_the_older_filter_pair_and_the_new_list(monkeypatch):
    calls = []
    monkeypatch.setattr(router.bactions, "update_draft", lambda bid, **kw: calls.append(kw))
    monkeypatch.setattr(router.bactions, "get", lambda bid, include_draft=False: DRAFT)
    client.patch("/benchmarks/4", json={"filter_column": "typ", "filter_value": "a"}, headers=AUTH)
    client.patch("/benchmarks/4", json={"filter_column": None, "filter_value": None}, headers=AUTH)
    client.patch("/benchmarks/4", json={"filters": [{"column": "a", "value": "1"}, {"column": "b", "value": "2"}]}, headers=AUTH)
    client.patch("/benchmarks/4", json={"name": "Only the name"}, headers=AUTH)
    assert calls[0] == {"filters": [{"column": "typ", "value": "a"}]}
    assert calls[1] == {"filters": []}
    assert [(f["column"], f["value"]) for f in calls[2]["filters"]] == [("a", "1"), ("b", "2")]
    assert calls[3] == {"name": "Only the name"}                      # filters untouched when not sent

