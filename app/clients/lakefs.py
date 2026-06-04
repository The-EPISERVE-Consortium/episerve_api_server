import json
import lakefs
from lakefs import Client
from app.config import settings


def _doip_url(qid: str) -> str:
    return f"{settings.doip_url.rstrip('/')}/doip/retrieve/{qid}"


def _doip_component_url(qid: str, component_id: str) -> str:
    return f"{settings.doip_url.rstrip('/')}/doip/retrieve/{qid}/{component_id}"


def _shard_qid(qid: str) -> str:
    digits = qid.upper().lstrip("Q").zfill(6)
    return f"{digits[0:2]}/{digits[2:4]}/{digits[4:6]}/{qid.upper()}"


def _parse_run_fdo(qid: str, fdo: dict) -> dict:
    profile    = fdo.get("profile",    {})
    kernel     = fdo.get("kernel",     {})
    provenance = fdo.get("provenance", {})

    attribution = provenance.get("prov:wasAttributedTo", "")
    docker_tag  = attribution.rsplit(":", 1)[-1] if ":" in attribution else ""

    input_files, output_files = [], []
    for comp in kernel.get("fdo:hasComponent", []):
        comp_id = comp.get("@id", "")
        if comp_id.startswith("components/input/"):
            input_files.append(comp_id)
        elif comp_id.startswith("components/output/"):
            output_files.append(comp_id)

    return {
        "run_id":           qid,
        "qid":              qid,
        "model_name":       profile.get("name",     ""),
        "docker_tag":       docker_tag,
        "status":           "",
        "run_timestamp":    kernel.get("modified",  ""),
        "computation_time": "",
        "input_files":      input_files,
        "output_files":     output_files,
        "doip_url":         _doip_url(qid),
    }


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
        if obj.path.endswith(".fdo.json"):
            continue
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
        qid = fdo.get("@id", "")
        profile = fdo.get("profile", {})
        fdo_dir = obj.path.rsplit("/", 1)[0]
        base    = f"lakefs://{settings.lakefs_processed_repo}/{settings.lakefs_branch}/{fdo_dir}"
        components = [
            {
                "name": c["componentId"],
                "url": _doip_component_url(qid, c["componentId"]),
                "lakefs_path": f"{base}/{c['@id']}",
            }
            for c in fdo.get("kernel", {}).get("fdo:hasComponent", [])
            if c.get("componentId") and c.get("@id")
        ]
        data_path = components[0]["lakefs_path"] if components else ""
        datasets.append({
            "qid": qid,
            "name": profile.get("name", ""),
            "description": profile.get("description", ""),
            "source_url": profile.get("url", ""),
            "lakefs_path": f"lakefs://{settings.lakefs_processed_repo}/{settings.lakefs_branch}/{obj.path}",
            "data_path": data_path,
            "last_modified": str(obj.mtime),
            "doip_url": _doip_url(qid) if qid else "",
            "components": components,
        })
    return datasets


def list_model_runs() -> list[dict]:
    client = _client()
    repo   = lakefs.Repository(settings.lakefs_model_runs_repo, client=client)
    branch = repo.branch(settings.lakefs_branch)

    runs = []
    for obj in branch.objects(max_amount=1000):
        if not obj.path.endswith(".fdo.json"):
            continue
        qid = obj.path.split("/")[-1].removesuffix(".fdo.json")
        content = branch.object(obj.path).reader().read()
        runs.append(_parse_run_fdo(qid, json.loads(content)))
    return runs


def get_model_run(run_id: str) -> dict | None:
    client = _client()
    repo   = lakefs.Repository(settings.lakefs_model_runs_repo, client=client)
    branch = repo.branch(settings.lakefs_branch)
    try:
        path    = f"{_shard_qid(run_id)}/{run_id.upper()}.fdo.json"
        content = branch.object(path).reader().read()
        return _parse_run_fdo(run_id, json.loads(content))
    except Exception:
        return None
