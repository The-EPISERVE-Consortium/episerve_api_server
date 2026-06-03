import requests
from app.config import settings


def _get(action: str, params: dict) -> dict:
    response = requests.get(
        f"{settings.ckan_url}/api/3/action/{action}",
        params=params,
        headers={"Authorization": settings.ckan_api_token},
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
        models.append({
            "name": pkg.get("name", ""),
            "docker_image": extras.get("docker_image", ""),
            "docker_tag": extras.get("docker_tag", ""),
            "description": pkg.get("notes", ""),
        })
    return models
