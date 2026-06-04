import json
import httpx
from app.config import settings


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.prefect_api_key:
        headers["Authorization"] = f"Bearer {settings.prefect_api_key}"
    return headers


def _deployment_id(deployment_name: str) -> str:
    """Resolve 'flow-name/deployment-name' to a Prefect deployment UUID."""
    flow_name, dep_name = deployment_name.split("/", 1)
    url = f"{settings.prefect_api_url}/deployments/name/{flow_name}/{dep_name}"
    response = httpx.get(url, headers=_headers())
    response.raise_for_status()
    return response.json()["id"]


def trigger_model_run(
    input_data_files: list,
    model_image: str,
    config_json: str,
    data_transformation_sql: list = None,
    model_tag: str = "latest",
) -> dict:
    """Create a Prefect flow run for the model-runner deployment."""
    deployment_id = _deployment_id(settings.prefect_model_runner_deployment)
    url = f"{settings.prefect_api_url}/deployments/{deployment_id}/create_flow_run"
    payload = {
        "parameters": {
            "input_data_files": input_data_files,
            "model_image": model_image,
            "model_tag": model_tag,
            "config_json": config_json,
            "data_transformation_sql": data_transformation_sql,
        }
    }
    response = httpx.post(url, headers=_headers(), content=json.dumps(payload))
    response.raise_for_status()
    data = response.json()
    return {
        "prefect_flow_run_id": data["id"],
        "status": data.get("state", {}).get("type", "SCHEDULED"),
    }
