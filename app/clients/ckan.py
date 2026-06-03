import ckanapi
from app.config import settings


def _client() -> ckanapi.RemoteCKAN:
    return ckanapi.RemoteCKAN(settings.ckan_url, apikey=settings.ckan_api_token)


def list_models() -> list[dict]:
    """Return all CKAN datasets tagged as 'model'."""
    ckan = _client()
    results = ckan.action.package_search(fq="tags:model", rows=1000)
    models = []
    for pkg in results.get("results", []):
        extras = {e["key"]: e["value"] for e in pkg.get("extras", [])}
        models.append({
            "name": pkg.get("name", ""),
            "docker_image": extras.get("docker_image", ""),
            "docker_tag": extras.get("docker_tag", ""),
            "description": pkg.get("notes", ""),
        })
    return models
