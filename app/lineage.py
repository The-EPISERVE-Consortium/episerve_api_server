"""Upstream lineage of a platform object as OpenLineage RunEvents, built from the FDO records.

Every FDO describes something that has finished, so each becomes one ``COMPLETE`` event:

- a **dataset** (``data-processed``): the download from its primary source to its files;
- a **benchmark**'s generated files: the source files at the pinned commit (``prov:wasDerivedFrom``)
  to ``input.parquet`` and ``reference.parquet``;
- a **model run**: the files it read (``prov:used``) to its output files.

Starting from one QID, the links are followed upstream (run -> benchmark -> source datasets -> external
source). Nothing is stored: the events are built when asked. Datasets read through the DOIP server are
named ``<QID>/<file>`` in the namespace of the DOIP base URL, so the output of one event and the input of
the next have the same name; the lakeFS commit is the dataset version. When a run or benchmark read a
dataset at a commit, the dataset's FDO is read at that commit, so its download event is the download that
produced exactly that version (a dataset read at two versions gives two download events).
"""

import re
import uuid
from collections import deque
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from app.config import settings

PRODUCER = "https://github.com/The-EPISERVE-Consortium/episerve_api_server"
RUN_EVENT_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_FACETS = "https://openlineage.io/spec/facets"
VERSION_FACET = f"{_FACETS}/1-0-1/DatasetVersionDatasetFacet.json#/$defs/DatasetVersionDatasetFacet"
DATASOURCE_FACET = f"{_FACETS}/1-0-1/DatasourceDatasetFacet.json#/$defs/DatasourceDatasetFacet"
NOMINAL_TIME_FACET = f"{_FACETS}/1-0-1/NominalTimeRunFacet.json#/$defs/NominalTimeRunFacet"
JOB_TYPE_FACET = f"{_FACETS}/2-0-3/JobTypeJobFacet.json#/$defs/JobTypeJobFacet"
EPISERVE_FACET = f"{PRODUCER}#lineage"

JOB_NAMESPACE = "episerve"
MAX_OBJECTS = 50

_QID = re.compile(r"\bQ\d{6,}\b")
_COMMIT = re.compile(r"[0-9a-f]{40,64}")
# Component files of a model run that are not its output.
_RUN_BOOKKEEPING = {"config.json", "config_prefect.json", "run.log", "ro-crate-metadata.json"}

# fetch(qid, version): the FDO of an object, at a lakeFS commit when ``version`` is given (None: the current one).
Fetch = Callable[[str, str | None], dict | None]


def _doip_namespace() -> str:
    return settings.doip_url.rstrip("/")


def _facet(schema: str, **fields) -> dict:
    return {"_producer": PRODUCER, "_schemaURL": schema, **fields}


def _dataset(qid: str, file: str | None = None, version: str | None = None) -> dict:
    """A dataset served by the DOIP server: ``<QID>/<file>`` (or the whole object), optionally at a commit."""
    entry = {"namespace": _doip_namespace(), "name": f"{qid}/{file}" if file else qid}
    if version:
        entry["facets"] = {"version": _facet(VERSION_FACET, datasetVersion=version)}
    return entry


def _external(url: str) -> dict:
    """A source outside the platform (e.g. an RKI file on GitHub): namespace = scheme and host, name = path."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return {"namespace": "external", "name": url}
    name = parts.path.strip("/") or "/"
    return {"namespace": f"{parts.scheme}://{parts.netloc}", "name": name,
            "facets": {"dataSource": _facet(DATASOURCE_FACET, name=parts.netloc, uri=url)}}


def parse_input(ref) -> tuple[dict, str | None]:
    """Turn one provenance reference into an OpenLineage input dataset and the QID it points to (if any).

    Handles the platform's forms: a DOIP retrieve URL (``.../doip/retrieve/<QID>/<file>?version=<commit>``),
    a ``lakefs://<repo>/<ref>/<path>`` URI, an object ``{"@id": ..., "schema:name", "schema:version"}``
    (benchmark sources) and a bare QID.
    """
    if isinstance(ref, dict):
        target = ref.get("@id") or ""
        if _QID.fullmatch(target):
            return _dataset(target, ref.get("schema:name"), ref.get("schema:version")), target
        return parse_input(target)
    ref = str(ref or "").strip()
    if _QID.fullmatch(ref):
        return _dataset(ref), ref
    parts = urlsplit(ref)
    if "/doip/retrieve/" in parts.path:
        rest = parts.path.split("/doip/retrieve/", 1)[1].strip("/")
        qid, _, file = rest.partition("/")
        version = (parse_qs(parts.query).get("version") or [None])[0]
        if _QID.fullmatch(qid):
            return _dataset(qid, file or None, version), qid
    if parts.scheme == "lakefs":
        repo, path = parts.netloc, parts.path.lstrip("/")
        ref_, _, inner = path.partition("/")
        qid = next(iter(_QID.findall(inner)), None)
        # The model runner pins a branch URI with ?version=<commit>; a commit as the ref is a version too.
        version = (parse_qs(parts.query).get("version") or [None])[0] or (ref_ if _COMMIT.fullmatch(ref_) else None)
        if qid and "/components/" in inner:
            return _dataset(qid, inner.split("/components/", 1)[1], version), qid
        entry = {"namespace": f"lakefs://{repo}", "name": inner or path}
        if version:
            entry["facets"] = {"version": _facet(VERSION_FACET, datasetVersion=version)}
        return entry, qid
    return _external(ref), None


def _components(fdo: dict) -> list[str]:
    names = []
    for comp in (fdo.get("kernel") or {}).get("fdo:hasComponent") or []:
        name = comp.get("componentId") or str(comp.get("@id", "")).rsplit("/", 1)[-1].lstrip("#")
        if name:
            names.append(name)
    return names


def kind_of(fdo: dict) -> str:
    """``run``, ``benchmark``, ``model`` or ``dataset``."""
    provenance = fdo.get("provenance") or {}
    profile = fdo.get("profile") or {}
    if provenance.get("@type") == "prov:Activity" or "prov:used" in provenance:
        return "run"
    if profile.get("additionalType") == "benchmarks" or "episerveBenchmark" in profile:
        return "benchmark"
    if profile.get("@type") == "SoftwareApplication" or str((fdo.get("kernel") or {}).get("digitalObjectType", "")).endswith("SoftwareApplication"):
        return "model"
    return "dataset"


def _event(qid: str, kind: str, job: str, job_type: str, event_time: str, inputs: list[dict],
           outputs: list[dict], run_facets: dict | None = None, extra: dict | None = None) -> dict:
    return {
        "eventType": "COMPLETE",
        "eventTime": event_time,
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA,
        "run": {
            "runId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{PRODUCER}#{qid}@{event_time}")),
            "facets": {
                **(run_facets or {}),
                "episerve": _facet(EPISERVE_FACET, qid=qid, kind=kind,
                                   fdo=f"{_doip_namespace()}/doip/retrieve/{qid}", **(extra or {})),
            },
        },
        "job": {
            "namespace": JOB_NAMESPACE,
            "name": job,
            "facets": {"jobType": _facet(JOB_TYPE_FACET, processingType="BATCH", integration="EPISERVE",
                                         jobType=job_type)},
        },
        "inputs": inputs,
        "outputs": outputs,
    }


def _parents(parsed: list[tuple[dict, str | None]]) -> list[tuple[str, str | None]]:
    """(QID, version read) of every input that is a platform object."""
    return [(qid, ((entry.get("facets") or {}).get("version") or {}).get("datasetVersion")) for entry, qid in parsed if qid]


def event_for(qid: str, fdo: dict, version: str | None = None) -> tuple[dict | None, list[tuple[str, str | None]]]:
    """The event that produced this object, and what it was made from as ``(QID, version read)`` (to follow upstream).

    ``version`` is the lakeFS commit the FDO was read at (None: the current FDO); a dataset's files get it as
    their version. A model's FDO describes software, not something produced from data: it has no event.
    """
    kind = kind_of(fdo)
    profile = fdo.get("profile") or {}
    provenance = fdo.get("provenance") or {}
    kernel = fdo.get("kernel") or {}
    when = provenance.get("prov:endedAtTime") or provenance.get("prov:generatedAtTime") or kernel.get("modified") or ""
    if kind == "model":
        return None, []

    if kind == "run":
        agent = provenance.get("prov:wasAssociatedWith") or {}
        model = agent.get("schema:name") or profile.get("name") or "unknown-model"
        used = provenance.get("prov:used") or []
        parsed = [parse_input(u) for u in (used if isinstance(used, list) else [used])]
        outputs = [_dataset(qid, name) for name in _components(fdo) if name not in _RUN_BOOKKEEPING]
        run_facets = {}
        if provenance.get("prov:startedAtTime") or provenance.get("prov:endedAtTime"):
            times = {"nominalStartTime": provenance.get("prov:startedAtTime") or when}
            if provenance.get("prov:endedAtTime"):
                times["nominalEndTime"] = provenance["prov:endedAtTime"]
            run_facets["nominalTime"] = _facet(NOMINAL_TIME_FACET, **times)
        extra = {"model_image": agent.get("@id", "")}
        user = (provenance.get("schema:agent") or {}).get("@id")
        if user:
            extra["executed_by"] = user
        return (_event(qid, kind, f"model-runner/{model}", "MODEL_RUN", when, [p[0] for p in parsed], outputs,
                       run_facets, extra),
                _parents(parsed))

    if kind == "benchmark":
        info = profile.get("episerveBenchmark") or {}
        commit = info.get("data_commit")
        sources = provenance.get("prov:wasDerivedFrom") or info.get("source_qids") or []
        parsed = []
        for source in sources if isinstance(sources, list) else [sources]:
            if isinstance(source, str) and _QID.fullmatch(source):
                source = {"@id": source, "schema:version": commit}   # older benchmarks: QIDs only
            parsed.append(parse_input(source))
        outputs = [_dataset(qid, name) for name in _components(fdo)]
        return (_event(qid, kind, f"benchmark/{info.get('id', qid)}", "BENCHMARK", when,
                       [p[0] for p in parsed], outputs),
                _parents(parsed))

    derived = provenance.get("prov:wasDerivedFrom") or {}
    derived = derived[0] if isinstance(derived, list) and derived else derived
    source = derived.get("prov:hadPrimarySource") if isinstance(derived, dict) else None
    source = source or profile.get("url")
    inputs = [_external(source)] if source else []
    extra = {"raw": derived.get("@id")} if isinstance(derived, dict) and derived.get("@id") else {}
    if version:
        extra["version"] = version
    outputs = [_dataset(qid, name, version) for name in _components(fdo)]
    job = f"dataset-downloader/{profile.get('name') or qid}"
    return _event(qid, kind, job, "DOWNLOAD", when, inputs, outputs, extra=extra), []


def upstream(qid: str, fetch: Fetch, max_objects: int = MAX_OBJECTS) -> dict:
    """The events of ``qid`` and of everything it was made from, nearest first.

    Args:
        qid: The object to start from.
        fetch: Returns the FDO of a QID, or None when there is none.
        max_objects: Most FDOs read; the rest is listed in ``truncated``.

    Returns:
        ``{"object", "kind", "events", "unresolved", "truncated"}``; ``kind`` is None when ``qid`` has no FDO.
    """
    qid = qid.upper()
    events, unresolved, truncated, seen = [], [], [], {(qid, None)}
    files: dict[str, list[str]] = {}
    queue = deque([(qid, None)])
    root_kind = None
    reads = 0
    while queue:
        current, version = queue.popleft()
        if reads >= max_objects:
            truncated.append(current)
            continue
        reads += 1
        fdo, read_at = None, None
        if version:
            fdo, read_at = fetch(current, version), version
        if fdo is None:            # no version, or the FDO is not in data-processed at that commit
            fdo, read_at = fetch(current, None), None
        if fdo is None:
            if current != qid:
                unresolved.append(current)
            continue
        files[current] = _components(fdo)
        if current == qid and version is None:
            root_kind = kind_of(fdo)
        event, parents = event_for(current, fdo, read_at)
        if event and not any(e["run"]["runId"] == event["run"]["runId"] for e in events):
            events.append(event)
        for parent, parent_version in parents:
            key = (parent.upper(), parent_version)
            if key not in seen:
                seen.add(key)
                queue.append(key)
    _name_whole_objects(events, files)
    return {"object": qid, "kind": root_kind, "events": events, "unresolved": unresolved, "truncated": truncated}


def _name_whole_objects(events: list[dict], files: dict[str, list[str]]) -> None:
    """Give an input that names only an object (older benchmarks list bare QIDs) the object's file, when it has
    exactly one, so it matches the output name of the event that produced the object."""
    namespace = _doip_namespace()
    for event in events:
        for entry in event["inputs"]:
            components = files.get(entry["name"])
            if entry["namespace"] == namespace and components and len(components) == 1:
                entry["name"] = f"{entry['name']}/{components[0]}"
