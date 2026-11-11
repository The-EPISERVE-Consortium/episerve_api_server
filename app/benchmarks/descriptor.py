"""Portable finalized benchmark definitions and database-free reconstruction."""

import hashlib
import json
import platform
import re
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timezone

import pandas as pd
import pyarrow as pa

from app.benchmarks import store
from app.benchmarks.definition import DefinitionError, validate

COMPONENT = "benchmark-definition.json"
KIND = "episerve.benchmark-definition"
SCHEMA_VERSION = 1
_COMMIT = re.compile(r"[0-9a-fA-F]{40,64}")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def _json_date(value) -> str:
    """Serialize calendar and timestamp values without coercing arbitrary objects.

    Args:
        value: Date or datetime included in a frozen definition.

    Returns:
        ISO-formatted value.

    Raises:
        TypeError: The value is not a date or datetime.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def build(row: dict, manifest: dict) -> bytes:
    """Serialize a complete scientific definition without a self-referencing commit.

    Args:
        row: Finalized benchmark fields, including its resolved horizon and dates.
        manifest: File metadata and recipe; commit may be absent until lakeFS writes it.

    Returns:
        UTF-8 JSON component. A missing generated_files.commit means its containing commit.
    """
    files = deepcopy({key: value for key, value in manifest.items() if key not in ("recipe", "definition")})
    definition = asdict(store.benchmark_from_row(row))
    document = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": {
            **definition, "id": row["id"], "name": row["name"], "description": row.get("description"),
            "component_name": row["component_name"], "truth_points": row["truth_points"],
            "created_at": row.get("created_at"), "finalized_at": row["finalized_at"],
            "retired_at": row.get("retired_at"),
        },
        "generated_files": files,
        "recipe": deepcopy(manifest["recipe"]),
        "generator": {"name": "episerve-api-server", "materializer_version": 1,
                      "python": platform.python_version(), "pandas": pd.__version__, "pyarrow": pa.__version__},
    }
    return json.dumps(document, default=_json_date, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def file_metadata(qid: str, data: bytes) -> dict:
    """Describe the JSON component for the FDO and database manifest.

    Args:
        qid: Generated benchmark object's identifier.
        data: Exact serialized descriptor bytes.

    Returns:
        Component name, media type, object identifier, and SHA-256 checksum.
    """
    return {"qid": qid, "component": COMPONENT, "media_type": "application/json",
            "sha256": hashlib.sha256(data).hexdigest()}


def rebuild_row(data: bytes, *, object_commit: str) -> dict:
    """Reconstruct a database-shaped row without database, catalog, or source access.

    Args:
        data: Descriptor bytes read from the surviving generated object.
        object_commit: Exact lakeFS commit at which this descriptor was retrieved.

    Returns:
        Row with its original identity, scientific definition, recipe, and object pins.
        Ownership is unset and target images can be regenerated from the frozen files.

    Raises:
        DefinitionError: The version, scientific definition, or essential file metadata is invalid.
    """
    try:
        document = json.loads(data)
        if document.get("kind") != KIND or type(document.get("schema_version")) is not int \
                or document["schema_version"] != SCHEMA_VERSION:
            raise DefinitionError("Unsupported benchmark descriptor kind or schema version")
        if not _COMMIT.fullmatch(object_commit):
            raise DefinitionError("An exact descriptor commit is required")
        saved = document["benchmark"]
        keys = ("id", "name", "description", "dataset_qid", "target_column", "date_column", "filters",
                "covariates", "history_start", "history_end", "test_start", "test_end", "input_filename",
                "prediction_length", "data_commit", "component_name", "truth_points", "created_at",
                "finalized_at", "retired_at")
        row = {key: saved[key] for key in keys}
        for key in ("test_start", "test_end", "history_start", "history_end"):
            row[key] = date.fromisoformat(row[key]) if row[key] is not None else None
        validate(store.benchmark_from_row(row), row["name"])
        if not isinstance(row["id"], int) or isinstance(row["id"], bool) or row["id"] < 1:
            raise DefinitionError("Invalid original benchmark identifier")
        if not row["finalized_at"] or not _COMMIT.fullmatch(row["data_commit"]):
            raise DefinitionError("A finalized benchmark and pinned source commit are required")
        if not row["history_end"] or not row["component_name"]:
            raise DefinitionError("Resolved history and source component are required")
        for key in ("prediction_length", "truth_points"):
            if not isinstance(row[key], int) or isinstance(row[key], bool) or row[key] < 1:
                raise DefinitionError(f"Invalid {key}")
        manifest = deepcopy(document["generated_files"])
        if not manifest.get("repo") or not manifest.get("branch"):
            raise DefinitionError("Generated repository and branch are required")
        manifest["commit"] = manifest.get("commit") or object_commit
        if not _COMMIT.fullmatch(manifest["commit"]) or not re.fullmatch(r"Q[0-9]+", manifest["qid"]):
            raise DefinitionError("Invalid generated object identity or version")
        for kind in ("input", "reference"):
            entry = manifest[kind]
            if entry["qid"] != manifest["qid"] or entry["component"] != f"{kind}.parquet":
                raise DefinitionError("Generated file identity does not match its object")
            if not _SHA256.fullmatch(entry["sha256"]) or not isinstance(entry["rows"], int) or entry["rows"] < 1:
                raise DefinitionError("Invalid generated file checksum or row count")
            if not {row["date_column"], row["target_column"]}.issubset(entry["columns"]):
                raise DefinitionError("Generated file is missing its date or target column")
        manifest["recipe"] = document["recipe"]
        if manifest["recipe"]["commit"] != row["data_commit"]:
            raise DefinitionError("Recipe and source data versions differ")
        manifest["definition"] = {**file_metadata(manifest["qid"], data), "commit": object_commit}
        row["materialized"] = json.dumps(manifest)
        row["filters"] = json.dumps(row["filters"]) if row["filters"] else None
        row["covariates"] = json.dumps(row["covariates"]) if row["covariates"] else None
        row.update(created_by=None, target_preview=None)
        return row
    except DefinitionError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DefinitionError(f"Invalid benchmark descriptor: {exc}") from exc
