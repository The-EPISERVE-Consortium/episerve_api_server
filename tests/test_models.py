from fastapi.testclient import TestClient

from app.main import app
from app.routers import models as models_router

client = TestClient(app)


_FAKE_MODEL = {
    "qid": "Q3303530955313",
    "name": "model__prediction__generic__chronos2-small",
    "docker_image": "ghcr.io/the-episerve-consortium/model__prediction__generic__chronos2-small",
    "docker_tag": "latest",
    "description": "desc",
    "docker_image_created": "2026-09-02T19:51:56Z",
    "doip_url": "https://doip.example/doip/retrieve/Q3303530955313",
    "git_repo": "https://github.com/The-EPISERVE-Consortium/model__prediction__generic__chronos2-small",
    "parameters": {
        "type": "object",
        "required": ["history_length", "prediction_length"],
        "properties": {
            "history_length": {"type": "integer", "minimum": 3, "description": "rows of history"},
            "prediction_length": {"type": "integer", "minimum": 1, "maximum": 1024, "description": "steps ahead"},
            "prediction_offset": {"type": "integer", "minimum": 0, "default": 0, "description": "rows skipped"},
        },
    },
    "inputs": [{"file": "input.parquet", "columns": [{"position": 0, "role": "time"}]}],
    "card_valid": False,
    "card_issues": [{"severity": "warning", "path": "license", "message": "missing"}],
}


def test_list_models_exposes_the_model_card(monkeypatch):
    """GET /models must not strip the model card sections / docker_image_created from the CKAN
    client's output (the response_model drops fields the Model schema does not declare)."""
    monkeypatch.setattr(models_router.ckan_client, "list_models", lambda: [_FAKE_MODEL])

    resp = client.get("/models")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    m = body[0]

    assert list(m["parameters"]["properties"]) == ["history_length", "prediction_length", "prediction_offset"]
    assert m["parameters"]["properties"]["prediction_length"]["maximum"] == 1024
    assert m["inputs"][0]["file"] == "input.parquet"
    assert m["outputs"] is None
    assert m["card_valid"] is False
    assert m["card_issues"][0]["path"] == "license"
    assert m["docker_image_created"] == "2026-09-02T19:51:56Z"


def test_ckan_client_reads_the_card_extras(monkeypatch):
    import json
    from app.clients import ckan
    pkg = {"title": "m", "notes": "", "url": "", "extras": [
        {"key": "model_qid", "value": "Q1"},
        {"key": "docker_image", "value": "ghcr.io/x/m"},
        {"key": "model_parameters", "value": json.dumps(_FAKE_MODEL["parameters"])},
        {"key": "model_task", "value": json.dumps({"type": "forecast"})},
        {"key": "model_card_valid", "value": "true"},
        {"key": "model_card_validation", "value": json.dumps({"valid": True, "issues": []})},
    ]}
    monkeypatch.setattr(ckan, "_get", lambda action, params: {"results": [pkg]})
    m = ckan.list_models()[0]
    assert m["parameters"] == _FAKE_MODEL["parameters"]
    assert m["task"] == {"type": "forecast"}
    assert m["inputs"] is None
    assert m["card_valid"] is True and m["card_issues"] == []


def test_ckan_client_without_card_extras(monkeypatch):
    from app.clients import ckan
    pkg = {"title": "m", "notes": "", "url": "", "extras": [{"key": "docker_image", "value": "ghcr.io/x/m"}]}
    monkeypatch.setattr(ckan, "_get", lambda action, params: {"results": [pkg]})
    m = ckan.list_models()[0]
    assert m["parameters"] is None and m["card_valid"] is None and m["card_issues"] == []
