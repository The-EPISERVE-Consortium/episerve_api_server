"""The FDO objects of a benchmark's generated files.

Each generated file (input, reference) is a digital object with its own QID, stored in the lakeFS repository
``benchmarks`` in the layout every other object uses (``pp/qq/rr/<QID>/<QID>.fdo.json`` and
``pp/qq/rr/<QID>/components/<file>``), so the DOIP server serves it and the sync registers it in CKAN.
"""

import json
import random
import time
from datetime import datetime, timezone

from app.benchmarks.definition import Benchmark


def shard(qid: str) -> str:
    """The sharded directory of a QID, e.g. ``17/91/23/Q1791234567890``."""
    digits = qid.upper().lstrip("Q").zfill(6)
    return f"{digits[0:2]}/{digits[2:4]}/{digits[4:6]}/{qid.upper()}"


def mint_qids(count: int) -> list[str]:
    """New distinct QIDs of the form ``Q<unix seconds><3 random digits>``."""
    seconds = int(time.time())
    qids: list[str] = []
    while len(qids) < count:
        qid = f"Q{seconds}{random.randint(0, 999):03d}"
        if qid not in qids:
            qids.append(qid)
    return qids


def component_path(qid: str, component: str) -> str:
    """Branch-relative path of a component file."""
    return f"{shard(qid)}/components/{component}"


def fdo_path(qid: str) -> str:
    """Branch-relative path of the object's FDO description."""
    return f"{shard(qid)}/{qid}.fdo.json"


def build_fdo(
    *, qid: str, benchmark_id: int, benchmark_name: str, role: str, component: str, sha256: str, rows: int,
    columns: list[str], benchmark: Benchmark, source_qids: list[str], description: str,
) -> bytes:
    """The FDO description (JSON) of one generated file.

    Args:
        qid: The object's QID.
        role: ``input`` (what models read) or ``reference`` (what forecasts are scored against).
        benchmark: The frozen definition; its pinned commit goes into the provenance.
        source_qids: The datasets the file was generated from.
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
                {"@id": f"#{component}", "componentId": component, "mediaType": "application/vnd.apache.parquet"},
            ],
        },
        "profile": {
            "@context": "https://schema.org/",
            "@type": "Dataset",
            "@id": qid,
            "name": f"benchmark-{benchmark_id}-{role}",
            "display_name": f"{benchmark_name} ({role} data)",
            "description": description,
            "url": "",
            "additionalType": "benchmarks",
            "accessRights": "open",
            "episerveBenchmark": {
                "id": benchmark_id, "role": role, "rows": rows, "columns": columns, "sha256": sha256,
                "source_qids": source_qids, "data_commit": benchmark.data_commit,
            },
        },
        "provenance": {
            "prov:generatedAtTime": now,
            "prov:wasAttributedTo": "EPISERVE benchmark designer",
            "prov:wasDerivedFrom": source_qids,
        },
    }
    return json.dumps(document, indent=2).encode()
