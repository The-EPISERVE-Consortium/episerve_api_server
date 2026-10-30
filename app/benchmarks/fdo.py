"""The FDO objects of a benchmark's generated files.

The generated files (input, reference) of a benchmark are ONE digital object with its own QID and one component per file, stored in the lakeFS repository
``benchmarks`` in the layout every other object uses (``pp/qq/rr/<QID>/<QID>.fdo.json`` and
``pp/qq/rr/<QID>/components/<file>``), so the DOIP server serves it and the sync registers it in CKAN.
"""

import json
import random
import time
from datetime import datetime, timezone

from app.benchmarks.definition import Benchmark
from app.config import settings


def shard(qid: str) -> str:
    """The sharded directory of a QID, e.g. ``17/91/23/Q1791234567890``."""
    digits = qid.upper().lstrip("Q").zfill(6)
    return f"{digits[0:2]}/{digits[2:4]}/{digits[4:6]}/{qid.upper()}"


def mint_qid() -> str:
    """A new QID of the form ``Q<unix seconds><3 random digits>``."""
    return f"Q{int(time.time())}{random.randint(0, 999):03d}"


def component_path(qid: str, component: str) -> str:
    """Branch-relative path of a component file."""
    return f"{shard(qid)}/components/{component}"


def fdo_path(qid: str) -> str:
    """Branch-relative path of the object's FDO description."""
    return f"{shard(qid)}/{qid}.fdo.json"


def derived_from(source_qids: list[str], sources: list[dict] | None, commit: str | None) -> list[dict]:
    """The ``prov:wasDerivedFrom`` entries: each source file at the pinned commit of ``data-processed``.

    One entry per file read (``sources``: ``{"role", "qid", "component"}``), with the commit as its version and the
    DOIP URL of exactly that file version. Without ``sources`` (older manifests) one entry per dataset QID.
    """
    base = f"{settings.doip_url.rstrip('/')}/doip/retrieve"
    query = f"?version={commit}" if commit else ""
    entries, seen = [], set()
    for source in sources or []:
        key = (source.get("qid"), source.get("component"))
        if not source.get("qid") or key in seen:
            continue
        seen.add(key)
        entry = {"@id": source["qid"]}
        if source.get("component"):
            entry["schema:name"] = source["component"]
        if source.get("role"):
            entry["schema:roleName"] = source["role"]
        if commit:
            entry["schema:version"] = commit
        path = f"{source['qid']}/{source['component']}" if source.get("component") else source["qid"]
        entry["schema:url"] = f"{base}/{path}{query}"
        entries.append(entry)
    for qid in source_qids:
        if qid not in {e["@id"] for e in entries}:
            entries.append({"@id": qid, **({"schema:version": commit} if commit else {}),
                            "schema:url": f"{base}/{qid}{query}"})
    return entries


def build_fdo(
    *, qid: str, benchmark_id: int, benchmark_name: str, files: dict[str, dict], benchmark: Benchmark,
    source_qids: list[str], description: str, sources: list[dict] | None = None,
) -> bytes:
    """The FDO description (JSON) of a benchmark's generated files: one object, one component per file.

    Args:
        qid: The object's QID.
        files: Role (``input``: what models read; ``reference``: what forecasts are scored against) to
            ``{"component", "sha256", "rows", "columns"}``.
        benchmark: The frozen definition; its pinned commit goes into the provenance.
        source_qids: The datasets the files were generated from.
        sources: The files read (``{"role", "qid", "component"}``); with the pinned commit they become the
            versioned ``prov:wasDerivedFrom`` entries.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = {
        "@context": [
            "https://w3id.org/fdo/context/v1",
            {"schema": "https://schema.org/", "prov": "http://www.w3.org/ns/prov#",
             "fdo": "https://w3id.org/fdo/vocabulary/"},
        ],
        "@id": qid,
        "@type": "DigitalObject",
        "kernel": {
            "@id": qid,
            "digitalObjectType": "https://schema.org/Dataset",
            "primaryIdentifier": qid,
            "kernelVersion": "v1",
            "immutable": True,
            "modified": now,
            "fdo:hasComponent": [
                {"@id": f"#{info['component']}", "componentId": info["component"],
                 "mediaType": "application/vnd.apache.parquet"}
                for info in files.values()
            ],
        },
        "profile": {
            "@context": "https://schema.org/",
            "@type": "Dataset",
            "@id": qid,
            "name": f"benchmark-{benchmark_id}",
            "display_name": f"Benchmark {benchmark_id}: {benchmark_name}",
            "description": description,
            "url": "",
            "additionalType": "benchmarks",
            "accessRights": "open",
            "episerveBenchmark": {
                "id": benchmark_id, "files": files, "source_qids": source_qids,
                "data_commit": benchmark.data_commit,
            },
        },
        "provenance": {
            "prov:generatedAtTime": now,
            "prov:wasAttributedTo": "EPISERVE benchmark designer",
            "prov:wasDerivedFrom": derived_from(source_qids, sources, benchmark.data_commit),
        },
    }
    return json.dumps(document, indent=2).encode()
