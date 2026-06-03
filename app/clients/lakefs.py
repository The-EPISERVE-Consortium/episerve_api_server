import json
import lakefs
from lakefs import Client
from app.config import settings


def _client() -> Client:
    return Client(
        host=settings.lakefs_url,
        username=settings.lakefs_access_key,
        password=settings.lakefs_secret_key,
    )


def list_raw_objects() -> list[dict]:
    client = _client()
    repo = lakefs.Repository(settings.lakefs_raw_repo, client=client)
    results = []
    for obj in repo.branch(settings.lakefs_branch).objects(max_amount=1000):
        results.append({
            "path": obj.path,
            "last_modified": str(obj.mtime),
            "size_bytes": obj.size_bytes,
        })
    return results


def list_processed_datasets() -> list[dict]:
    """Return entries from data-processed that have an .fdo.json sidecar."""
    client = _client()
    repo = lakefs.Repository(settings.lakefs_processed_repo, client=client)
    branch = repo.branch(settings.lakefs_branch)

    fdo_objects = [
        obj for obj in branch.objects(max_amount=1000)
        if obj.path.endswith(".fdo.json")
    ]

    datasets = []
    for obj in fdo_objects:
        content = branch.object(obj.path).reader().read()
        fdo = json.loads(content)
        datasets.append({
            "qid": fdo.get("@id", ""),
            "name": fdo.get("name", obj.path),
            "description": fdo.get("description", ""),
            "source_url": fdo.get("url", ""),
            "lakefs_path": f"lakefs://{settings.lakefs_processed_repo}/{settings.lakefs_branch}/{obj.path}",
            "last_modified": str(obj.mtime),
        })
    return datasets


def list_model_runs() -> list[dict]:
    client = _client()
    repo = lakefs.Repository(settings.lakefs_model_runs_repo, client=client)
    branch = repo.branch(settings.lakefs_branch)

    metadata_objects = [
        obj for obj in branch.objects(max_amount=1000)
        if obj.path.endswith("/metadata.json")
    ]

    runs = []
    for obj in metadata_objects:
        content = branch.object(obj.path).reader().read()
        metadata = json.loads(content)
        run_id = obj.path.split("/")[0]
        runs.append({
            "run_id": run_id,
            "model_name": metadata.get("model_name", ""),
            "docker_tag": metadata.get("docker_tag", ""),
            "status": metadata.get("status", ""),
            "run_timestamp": metadata.get("run_timestamp", ""),
            "computation_time": metadata.get("computation_time", ""),
            "input_files": metadata.get("input_files", []),
            "output_files": metadata.get("output_files", []),
        })
    return runs


def get_model_run(run_id: str) -> dict | None:
    client = _client()
    repo = lakefs.Repository(settings.lakefs_model_runs_repo, client=client)
    branch = repo.branch(settings.lakefs_branch)
    try:
        content = branch.object(f"{run_id}/metadata.json").reader().read()
        metadata = json.loads(content)
        return {
            "run_id": run_id,
            "model_name": metadata.get("model_name", ""),
            "docker_tag": metadata.get("docker_tag", ""),
            "status": metadata.get("status", ""),
            "run_timestamp": metadata.get("run_timestamp", ""),
            "computation_time": metadata.get("computation_time", ""),
            "input_files": metadata.get("input_files", []),
            "output_files": metadata.get("output_files", []),
        }
    except Exception:
        return None
