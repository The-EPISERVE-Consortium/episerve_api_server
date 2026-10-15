"""Tests for GET /leaderboard with the CKAN and loaders mocked."""

import pandas as pd
from fastapi.testclient import TestClient

from app.leaderboard.loaders import LoadError
from app.main import app
from app.routers import leaderboard as router

client = TestClient(app)

DATES = pd.to_datetime(["2022-01-05", "2022-01-12"])
PARAMS = {
    "dataset_qid": "Q1", "target": "v", "date_column": "datum",
    "test_start": "2022-01-01", "test_end": "2022-01-31",
}
RUN = {
    "qid": "Q10", "model_name": "m", "docker_tag": "latest", "status": "success",
    "input_dataset_qids": ["Q1"], "run_timestamp": "t", "predictions_resource_id": "res",
    "data_transformation_sql": ["SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2021-12-31'"],
}


def _patch(monkeypatch, *, datasets=None, runs=None, truth=None, predictions=None):
    monkeypatch.setattr(router.ckan_client, "list_processed_datasets",
                        lambda: [{"qid": "Q1", "last_modified": "2026-10-14"}] if datasets is None else datasets)
    monkeypatch.setattr(router.ckan_client, "list_model_runs", lambda: [RUN] if runs is None else runs)
    monkeypatch.setattr(router, "default_loaders", lambda: {
        "load_truth": truth or (lambda d, b: pd.DataFrame({"date": DATES, "truth": [10.0, 20.0]})),
        "load_predictions": predictions or (lambda r, b: pd.DataFrame({"date": DATES, "forecast": [11.0, 19.0]})),
        "load_params": lambda qid: {"history_length": 91},
    })


def test_returns_ranked_entries(monkeypatch):
    _patch(monkeypatch)
    resp = client.get("/leaderboard", params={**PARAMS, "rank_by": "mae"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["best"] == "m" and body["entries"][0]["rank"] == 1
    assert body["entries"][0]["metrics"]["mae"] == 1.0
    assert body["entries"][0]["history_end"] == "2021-12-31"
    assert body["truth_as_of"] == "2026-10-14"


def test_unknown_dataset_is_404(monkeypatch):
    _patch(monkeypatch, datasets=[])
    assert client.get("/leaderboard", params=PARAMS).status_code == 404


def test_reversed_window_is_400(monkeypatch):
    _patch(monkeypatch)
    resp = client.get("/leaderboard", params={**PARAMS, "test_start": "2022-02-01", "test_end": "2022-01-01"})
    assert resp.status_code == 400


def test_unknown_metric_is_422(monkeypatch):
    _patch(monkeypatch)
    assert client.get("/leaderboard", params={**PARAMS, "rank_by": "nope"}).status_code == 422


def test_unscorable_benchmark_is_422(monkeypatch):
    def no_truth(dataset, benchmark):
        raise LoadError("the dataset has no observations in the test window")

    _patch(monkeypatch, truth=no_truth)
    resp = client.get("/leaderboard", params=PARAMS)
    assert resp.status_code == 422
    assert "no observations" in resp.json()["detail"]


def test_catalog_failure_is_502(monkeypatch):
    _patch(monkeypatch)

    def boom():
        raise RuntimeError("ckan down")

    monkeypatch.setattr(router.ckan_client, "list_model_runs", boom)
    assert client.get("/leaderboard", params=PARAMS).status_code == 502
