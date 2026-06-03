import requests
from app.config import settings


def _get(action: str, params: dict) -> dict:
    response = requests.get(
        f"{settings.ckan_url}/api/3/action/{action}",
        params=params,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("success"):
        raise RuntimeError(f"CKAN {action} failed: {body.get('error')}")
    return body["result"]


def list_models() -> list[dict]:
    """Return all CKAN datasets in the type-model group."""
    result = _get("package_search", {"fq": "groups:type-model", "rows": 1000})
    models = []
    for pkg in result.get("results", []):
        extras = {e["key"]: e["value"] for e in pkg.get("extras", [])}
        name = pkg.get("name", "")
        docker_tag = extras.get("docker_image", "")
        models.append({
            "name": name,
            "docker_image": f"ghcr.io/the-episerve-consortium/{name}",
            "docker_tag": docker_tag,
            "description": pkg.get("notes", ""),
        })
    return models
