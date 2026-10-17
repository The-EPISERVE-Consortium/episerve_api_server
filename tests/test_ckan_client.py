"""Tests for CKAN client helpers that need no network."""

from app.clients import ckan


def test_list_model_runs_exposes_predictions_resource_id(monkeypatch):
    package = {
        "extras": [{"key": "qid", "value": "Q1"}, {"key": "status", "value": "success"}],
        "resources": [
            {"id": "cfg", "name": "config.json", "description": "Input file", "url": "u"},
            {"id": "pred-1", "name": "predictions.tsv", "description": "Output file", "url": "u2"},
        ],
    }
    monkeypatch.setattr(ckan, "_get", lambda action, params: {"results": [package]})
    run = ckan.list_model_runs()[0]
    assert run["predictions_resource_id"] == "pred-1"
    assert run["started_by"] == ""                                           # older runs have none


def test_list_model_runs_exposes_who_started_the_run(monkeypatch):
    package = {"extras": [{"key": "qid", "value": "Q1"}, {"key": "started_by", "value": "episerve_user_18"}],
               "resources": []}
    monkeypatch.setattr(ckan, "_get", lambda action, params: {"results": [package]})
    assert ckan.list_model_runs()[0]["started_by"] == "episerve_user_18"


def test_get_datastore_records_paginates_and_strips_ids(monkeypatch):
    pages = {
        0: [{"_id": 1, "datum": "a"}, {"_id": 2, "datum": "b"}],
        2: [{"_id": 3, "datum": "c"}],
    }
    calls = []

    def fake_get(action, params):
        calls.append((action, params["offset"]))
        return {"records": pages[params["offset"]]}

    monkeypatch.setattr(ckan, "_get", fake_get)
    rows = ckan.get_datastore_records("res", page_size=2)
    assert rows == [{"datum": "a"}, {"datum": "b"}, {"datum": "c"}]
    assert calls == [("datastore_search", 0), ("datastore_search", 2)]
