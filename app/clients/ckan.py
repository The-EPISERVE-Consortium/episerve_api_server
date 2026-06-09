import json
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


def _doip_url(qid: str) -> str:
    return f"{settings.doip_url.rstrip('/')}/doip/retrieve/{qid}"


def list_models() -> list[dict]:
    """Return all CKAN datasets in the type-model group."""
    result = _get("package_search", {"fq": "groups:type-model", "rows": 1000})
    models = []
    for pkg in result.get("results", []):
        extras = {e["key"]: e["value"] for e in pkg.get("extras", [])}
        qid          = extras.get("model_qid", "")
        docker_image = extras.get("docker_image", "")
        git_repo     = pkg.get("url", "") or (
            f"https://github.com/{docker_image[len('ghcr.io/'):]}"
            if docker_image.startswith("ghcr.io/") else ""
        )
        raw_params = extras.get("model_parameters", "")
        additional_properties = json.loads(raw_params) if raw_params else []
        models.append({
            "qid":                  qid,
            "name":                 pkg.get("title", ""),
            "docker_image":         docker_image,
            "docker_tag":           extras.get("docker_tag", ""),
            "description":          pkg.get("notes", ""),
            "docker_image_created": extras.get("docker_image_created", ""),
            "doip_url":             _doip_url(qid) if qid else "",
            "git_repo":             git_repo,
            "additional_properties": additional_properties,
        })
    return models


def list_processed_datasets() -> list[dict]:
    """Return processed datasets from CKAN (type-raw-data group)."""
    result = _get("package_search", {"fq": "groups:type-raw-data", "rows": 1000})
    datasets = []
    for pkg in result.get("results", []):
        extras = {e["key"]: e["value"] for e in pkg.get("extras", [])}
        qid = extras.get("qid", "")
        components = [
            {"name": r["name"], "url": r["url"]}
            for r in pkg.get("resources", [])
            if r.get("format", "").upper() == "PARQUET"
        ]
        data_path = components[0]["url"] if components else ""
        datasets.append({
            "qid": qid,
            "name": pkg.get("title", ""),
            "description": pkg.get("notes", ""),
            "source_url": pkg.get("url", ""),
            "data_path": data_path,
            "last_modified": extras.get("modified", ""),
            "metadata_created": pkg.get("metadata_created", ""),
            "doip_url": _doip_url(qid) if qid else "",
            "components": components,
            "additional_type": extras.get("additional_type", ""),
        })
    return datasets


def list_raw_datasets() -> list[dict]:
    """Return raw datasets from CKAN (type-raw-data group), shaped for the raw table."""
    result = _get("package_search", {"fq": "groups:type-raw-data", "rows": 1000})
    datasets = []
    for pkg in result.get("results", []):
        extras = {e["key"]: e["value"] for e in pkg.get("extras", [])}
        parquet = next(
            (r for r in pkg.get("resources", []) if r.get("format", "").upper() == "PARQUET"),
            None,
        )
        datasets.append({
            "path": pkg.get("title", ""),
            "size_bytes": parquet.get("size") if parquet else None,
            "last_modified": extras.get("modified", ""),
        })
    return datasets


def list_model_runs() -> list[dict]:
    """Return model runs from CKAN (type-model-run group)."""
    result = _get("package_search", {"fq": "groups:type-model-run", "rows": 1000})
    runs = []
    for pkg in result.get("results", []):
        extras = {e["key"]: e["value"] for e in pkg.get("extras", [])}
        qid = extras.get("qid", "")
        input_files  = [r["url"] for r in pkg.get("resources", []) if r.get("description") == "Input file"]
        output_files = [r["url"] for r in pkg.get("resources", []) if r.get("description") == "Output file"]
        raw_qids = extras.get("input_dataset_qids", "")
        try:
            input_dataset_qids = json.loads(raw_qids) if raw_qids else []
        except Exception:
            input_dataset_qids = []
        raw_sql = extras.get("data_transformation_sql", "")
        try:
            data_transformation_sql = json.loads(raw_sql) if raw_sql else []
        except Exception:
            data_transformation_sql = []
        runs.append({
            "run_id":              qid,
            "qid":                 qid,
            "model_name":          extras.get("model", ""),
            "docker_tag":          extras.get("docker_tag", ""),
            "status":              extras.get("status", ""),
            "run_timestamp":       extras.get("run_timestamp", ""),
            "computation_time":    extras.get("computation_time", ""),
            "input_files":             input_files,
            "output_files":            output_files,
            "input_dataset_qids":      input_dataset_qids,
            "data_transformation_sql": data_transformation_sql,
            "doip_url":                _doip_url(qid) if qid else "",
        })
    return runs
