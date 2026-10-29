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


def _fake_catalog(monkeypatch, models):
    monkeypatch.setattr(models_router.ckan_client, "list_models", lambda: models)


def test_check_config_fills_defaults(monkeypatch):
    _fake_catalog(monkeypatch, [_FAKE_MODEL])
    resp = client.post(f"/models/{_FAKE_MODEL['name']}/check-config",
                       json={"config": {"history_length": 10, "prediction_length": 4}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True and body["problems"] == []
    assert body["config"] == {"history_length": 10, "prediction_length": 4, "prediction_offset": 0}
    assert body["qid"] == "Q3303530955313"


def test_check_config_reports_problems_and_finds_the_model_by_qid(monkeypatch):
    _fake_catalog(monkeypatch, [_FAKE_MODEL])
    resp = client.post("/models/q3303530955313/check-config",
                       json={"config": {"history_length": 2, "prediction_length": 2000}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any(p.startswith("history_length:") for p in body["problems"])
    assert any(p.startswith("prediction_length:") for p in body["problems"])


def test_check_config_unknown_model_is_404(monkeypatch):
    _fake_catalog(monkeypatch, [_FAKE_MODEL])
    resp = client.post("/models/nope/check-config", json={"config": {}})
    assert resp.status_code == 404


def test_check_config_without_declared_parameters_checks_nothing(monkeypatch):
    _fake_catalog(monkeypatch, [{**_FAKE_MODEL, "parameters": None}])
    resp = client.post(f"/models/{_FAKE_MODEL['name']}/check-config", json={"config": {"x": 1}})
    assert resp.status_code == 200
    assert resp.json()["valid"] is None and resp.json()["config"] == {"x": 1}


def test_check_config_catalog_down_is_502(monkeypatch):
    def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(models_router.ckan_client, "list_models", boom)
    assert client.post("/models/x/check-config", json={"config": {}}).status_code == 502


def test_check_config_needs_a_config_object(monkeypatch):
    _fake_catalog(monkeypatch, [_FAKE_MODEL])
    assert client.post(f"/models/{_FAKE_MODEL['name']}/check-config", json={"history_length": 3}).status_code == 422
    assert client.post(f"/models/{_FAKE_MODEL['name']}/check-config", content=b"{").status_code == 400
