"""Config validation against the model's declared parameters, and its use by POST /model-runs."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN
from tests.fakes import bearer
from app.main import app
from app.model_config import config_problems, config_template
from app.routers import model_runs as router

client = TestClient(app)
AUTH = bearer(ADMIN)

PARAMS = {
    "type": "object",
    "required": ["history_length", "prediction_length"],
    "additionalProperties": False,
    "properties": {
        "history_length": {"type": "integer", "minimum": 3, "description": "rows of history"},
        "prediction_length": {"type": "integer", "minimum": 1, "maximum": 512, "description": "steps ahead"},
        "prediction_offset": {"type": "integer", "minimum": 0, "default": 0, "description": "rows skipped"},
    },
}
IMAGE = "ghcr.io/the-episerve-consortium/model__prediction__generic__timesfm"
MODEL = {"qid": "Q1", "name": "timesfm", "docker_image": IMAGE, "docker_tag": "latest",
         "parameters": PARAMS}
BODY = {"model_image": IMAGE, "model_tag": "latest", "input_data_files": [["https://x/y.parquet", "input.parquet"]],
        "config": {"history_length": 233, "prediction_length": 8}}


def test_missing_required_key_is_reported():
    problems = config_problems({"prediction_length": 8}, PARAMS)
    assert problems == ["'history_length' is a required property"]


def test_null_is_not_a_value():
    problems = config_problems({"history_length": None, "prediction_length": 8}, PARAMS)
    assert problems == ["history_length: None is not of type 'integer'"]


@pytest.mark.parametrize("value,expected", [
    (2, "history_length: 2 is less than the minimum of 3"),
    (600, None),
    ("10", "history_length: '10' is not of type 'integer'"),
])
def test_bounds_and_types_are_checked(value, expected):
    problems = config_problems({"history_length": value, "prediction_length": 8}, PARAMS)
    assert problems == ([expected] if expected else [])


def test_unknown_key_is_reported():
    problems = config_problems({"history_length": 10, "prediction_length": 8, "horizon": 3}, PARAMS)
    assert len(problems) == 1 and "'horizon' was unexpected" in problems[0]


def test_config_template_has_defaults_and_nulls():
    assert config_template(PARAMS) == {"history_length": None, "prediction_length": None, "prediction_offset": 0}
    assert config_template(None) == {}


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
    assert triggered[0]["executed_by"] == "episerve_user_1"      # the pseudonym of the logged-in user, nothing else


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


def test_model_without_parameters_is_not_blocked(monkeypatch, triggered):
    monkeypatch.setattr(router.ckan_client, "list_models", lambda: [{**MODEL, "parameters": None}])
    body = {**BODY, "config": {}}
    assert client.post("/model-runs", json=body, headers=AUTH).status_code == 202
    assert len(triggered) == 1
