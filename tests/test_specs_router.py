import json

from fastapi.testclient import TestClient

from app.main import app
from app.routers import specs as specs_router

client = TestClient(app)

def _minimal_card():
    from importlib import resources
    examples = resources.files("episerve_specs.specs.model_card").joinpath("examples")
    first = sorted(p for p in examples.iterdir() if p.name.endswith(".json"))[0]
    return json.loads(first.read_text())


def test_list_schemas():
    resp = client.get("/schemas")
    assert resp.status_code == 200
    body = resp.json()
    assert "1" in body["model-card"]["versions"]
    assert body["model-card"]["schemas"]["1"] == "https://api.episerve.zib.de/schemas/model-card/v1.json"


def test_get_schema_serves_the_conforms_to_url():
    resp = client.get("/schemas/model-card/v1.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/schema+json")
    assert resp.json()["$id"] == "https://api.episerve.zib.de/schemas/model-card/v1.json"


def test_get_unknown_schema_is_404():
    assert client.get("/schemas/model-card/v99.json").status_code == 404
    assert client.get("/schemas/nope/v1.json").status_code == 404


def test_validate_valid_card():
    resp = client.post("/validate/model-card", json=_minimal_card())
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["spec"] == "model-card" and body["version"] == "1"
    assert body["schema"] == "https://api.episerve.zib.de/schemas/model-card/v1.json"
    assert body["validator"].startswith("episerve-specs ")


def test_validate_invalid_card_is_a_normal_answer():
    card = _minimal_card()
    card["additionalProperty"] = []
    del card["episerve:parameters"]
    resp = client.post("/validate/model-card", json=card)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    paths = [i["path"] for i in body["issues"] if i["severity"] == "error"]
    assert "additionalProperty" in paths


def test_validate_unknown_spec_or_version_is_404():
    assert client.post("/validate/nope", json={}).status_code == 404
    assert client.post("/validate/model-card?version=99", json={}).status_code == 404


def test_validate_rejects_non_json_and_large_bodies(monkeypatch):
    assert client.post("/validate/model-card", content=b"not json").status_code == 400
    monkeypatch.setattr(specs_router, "MAX_DOCUMENT_BYTES", 10)
    resp = client.post("/validate/model-card", content=json.dumps(_minimal_card()).encode())
    assert resp.status_code == 413
