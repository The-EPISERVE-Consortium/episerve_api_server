"""Config validation against the model's declared parameters, and its use by POST /model-runs."""

import pytest
from fastapi.testclient import TestClient

from app.auth import _daily_token
from app.main import app
from app.model_config import config_problems
from app.routers import model_runs as router

client = TestClient(app)
AUTH = {"Authorization": f"Bearer {_daily_token()}"}

PARAMS = [
    {"name": "history_length", "valueRequired": True, "minValue": 3},
    {"name": "prediction_length", "valueRequired": True, "minValue": 1, "maxValue": 512},
    {"name": "prediction_offset", "valueRequired": False, "minValue": 0, "value": 0},
]
IMAGE = "ghcr.io/the-episerve-consortium/model__prediction__generic__timesfm"
MODEL = {"qid": "Q1", "name": "timesfm", "docker_image": IMAGE, "docker_tag": "latest",
         "additional_properties": PARAMS}
BODY = {"model_image": IMAGE, "model_tag": "latest", "input_data_files": [["https://x/y.parquet", "input.parquet"]],
        "config": {"history_length": 233, "prediction_length": 8}}


def test_missing_required_key_is_reported():
    problems = config_problems({"prediction_length": 8}, PARAMS)
    assert problems == ["missing required parameter 'history_length'"]


def test_null_counts_as_missing():
    problems = config_problems({"history_length": None, "prediction_length": 8}, PARAMS)
    assert problems == ["missing required parameter 'history_length'"]


@pytest.mark.parametrize("value,expected", [
    (2, "parameter 'history_length' is 2, below the minimum 3"),
    (600, None),
])
def test_bounds_are_checked(value, expected):
    problems = config_problems({"history_length": value, "prediction_length": 8}, PARAMS)
    assert problems == ([expected] if expected else [])


@pytest.fixture
def triggered(monkeypatch):
    calls = []
    monkeypatch.setattr(router.prefect_client, "trigger_model_run",
                        lambda **kw: calls.append(kw) or {"prefect_flow_run_id": "abc", "status": "SCHEDULED"})
    return calls


def test_run_with_missing_required_key_is_rejected_before_prefect(monkeypatch, triggered):
    monkeypatch.setattr(router.ckan_client, "list_models", lambda: [MODEL])
    body = {**BODY, "config": {"prediction_length": 8}}
    resp = client.post("/model-runs", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert "history_length" in resp.json()["detail"]
    assert triggered == []


def test_valid_run_is_triggered(monkeypatch, triggered):
    monkeypatch.setattr(router.ckan_client, "list_models", lambda: [MODEL])
    resp = client.post("/model-runs", json=BODY, headers=AUTH)
    assert resp.status_code == 202
    assert len(triggered) == 1


def test_unknown_model_is_not_blocked(monkeypatch, triggered):
    monkeypatch.setattr(router.ckan_client, "list_models", lambda: [])
    body = {**BODY, "config": {}}
    assert client.post("/model-runs", json=body, headers=AUTH).status_code == 202
    assert len(triggered) == 1


def test_catalog_failure_does_not_block_the_run(monkeypatch, triggered):
    def boom():
        raise RuntimeError("ckan down")
    monkeypatch.setattr(router.ckan_client, "list_models", boom)
    body = {**BODY, "config": {}}
    assert client.post("/model-runs", json=body, headers=AUTH).status_code == 202
    assert len(triggered) == 1
