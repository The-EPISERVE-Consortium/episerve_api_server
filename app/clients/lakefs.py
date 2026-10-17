import json
import re
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


def list_models() -> list[dict]:
    client = _client()
    repo   = lakefs.Repository(settings.lakefs_models_repo, client=client)
    branch = repo.branch(settings.lakefs_branch)

    models = []
    for obj in branch.objects(max_amount=1000):
        if not obj.path.endswith(".fdo.json"):
            continue
        qid     = obj.path.split("/")[-1].removesuffix(".fdo.json")
        content = branch.object(obj.path).reader().read()
        fdo     = json.loads(content)
        profile = fdo.get("profile", {})
        models.append({
            "qid":          qid,
            "name":         profile.get("name",            ""),
            "description":  profile.get("description",     ""),
            "docker_image": profile.get("url",             ""),
            "docker_tag":   profile.get("softwareVersion", ""),
        })
    return models


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


_QID = re.compile(r"Q[0-9]+")
_FILENAME = re.compile(r"[A-Za-z0-9_.\-]+")
_COMMIT = re.compile(r"[0-9a-f]{40,64}")
_BENCHMARK_PATH = re.compile(r"\d\d/\d\d/\d\d/Q\d+(/components)?/[A-Za-z0-9_.\-]+")


def processed_head_commit() -> str:
    """Return the commit id of the processed-data repository's configured branch head."""
    repo = lakefs.Repository(settings.lakefs_processed_repo, client=_client())
    return repo.branch(settings.lakefs_branch).head.id


def read_processed_component(commit: str, qid: str, filename: str, max_bytes: int) -> bytes:
    """Read a dataset component of the processed-data repository at a pinned commit.

    Args:
        commit: lakeFS commit id (hex).
        qid: Dataset QID, e.g. ``Q9493814122238``; the path uses the QID sharding.
        filename: Component file name (no directories).
        max_bytes: Largest object accepted.

    Returns:
        The object's bytes.

    Raises:
        ValueError: If an argument is unsafe or the object exceeds ``max_bytes``.
    """
    if (
        not _COMMIT.fullmatch(commit or "") or not _QID.fullmatch(qid or "")
        or not _FILENAME.fullmatch(filename or "") or filename in (".", "..")
    ):
        raise ValueError("Invalid commit, dataset or file name.")
    path = f"{_shard_qid(qid)}/components/{filename}"
    repo = lakefs.Repository(settings.lakefs_processed_repo, client=_client())
    data = repo.ref(commit).object(path).reader().read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("The dataset file exceeds the size limit.")
    return data


def write_benchmark_objects(files: dict[str, bytes], message: str) -> tuple[str, str]:
    """Store generated files in the benchmarks repository and commit them in one commit.

    Args:
        files: Branch-relative path (``pp/qq/rr/Q…/…``, as built by ``app.benchmarks.fdo``) to content.
        message: The commit message.

    Returns:
        ``(branch, commit id)``; the files are read back at that commit.

    Raises:
        ValueError: If a path is unsafe.
    """
    if any(not _BENCHMARK_PATH.fullmatch(path) or ".." in path for path in files):
        raise ValueError("Invalid file path.")
    repo = lakefs.Repository(settings.lakefs_benchmarks_repo, client=_client())
    branch = repo.branch(settings.lakefs_branch)
    for path, data in files.items():
        branch.object(path).upload(data=data, mode="wb", pre_sign=False)
    branch.commit(message=message)
    return settings.lakefs_branch, branch.head.id


def read_benchmark_component(commit: str, qid: str, filename: str, max_bytes: int) -> bytes:
    """Read a component of a generated benchmark object at a commit of the benchmarks repository.

    Raises:
        ValueError: If an argument is unsafe or the file exceeds ``max_bytes``.
    """
    if not _COMMIT.fullmatch(commit or "") or not _QID.fullmatch(qid or "") \
            or not _FILENAME.fullmatch(filename or "") or filename in (".", ".."):
        raise ValueError("Invalid commit, object or file name.")
    repo = lakefs.Repository(settings.lakefs_benchmarks_repo, client=_client())
    data = repo.ref(commit).object(f"{_shard_qid(qid)}/components/{filename}").reader().read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("The file exceeds the size limit.")
    return data
