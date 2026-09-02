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
    "additional_properties": [
        {"@type": "PropertyValue", "name": "history_length", "valueRequired": True, "minValue": 3},
        {"@type": "PropertyValue", "name": "prediction_length", "valueRequired": True, "minValue": 1, "maxValue": 1024},
        {"@type": "PropertyValue", "name": "prediction_offset", "valueRequired": False, "minValue": 0, "value": 0},
    ],
}


def test_list_models_exposes_additional_properties(monkeypatch):
    """GET /models must not strip additional_properties / docker_image_created
    from the CKAN client's output (regression: the Model schema omitted them,
    so the response_model dropped the fields)."""
    monkeypatch.setattr(models_router.ckan_client, "list_models", lambda: [_FAKE_MODEL])

    resp = client.get("/models")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    m = body[0]

    assert [p["name"] for p in m["additional_properties"]] == [
        "history_length",
        "prediction_length",
        "prediction_offset",
    ]
    assert m["additional_properties"][1]["maxValue"] == 1024
    assert m["docker_image_created"] == "2026-09-02T19:51:56Z"


def test_list_models_defaults_additional_properties_to_empty(monkeypatch):
    """A model with no declared parameters serialises as an empty list, not null."""
    bare = {**_FAKE_MODEL}
    bare.pop("additional_properties")
    bare.pop("docker_image_created")
    monkeypatch.setattr(models_router.ckan_client, "list_models", lambda: [bare])

    resp = client.get("/models")
    assert resp.status_code == 200
    m = resp.json()[0]
    assert m["additional_properties"] == []
    assert m["docker_image_created"] == ""
