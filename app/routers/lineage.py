"""Upstream lineage of an object as OpenLineage events, built from the FDO records (see app/lineage.py)."""

import json
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app import lineage
from app.clients import doip as doip_client

router = APIRouter(prefix="/lineage", tags=["lineage"])

_QID = re.compile(r"[Qq]\d{6,}")
_COMMIT = re.compile(r"[0-9a-f]{64}")


def fetch(qid: str, version: str | None) -> dict | None:
    """An FDO from the DOIP server: at a commit when a version is given (None if it is not there), else the current one."""
    if version:
        try:
            return doip_client.fetch_fdo(qid, version=version)
        except Exception:
            return None
    return doip_client.fetch_fdo(qid)


def latest(qid: str) -> str | None:
    """The newest commit of an object, for the files it wrote when no reader names a version."""
    return doip_client.latest_version(qid)


@router.get("/events")
def get_events(
    object: str = Query(..., description="QID of a dataset, benchmark data object or model run"),
    format: str = Query("json", pattern="^(json|ndjson)$",
                        description="json: object with events and unresolved links; ndjson: one event per line"),
    version: str | None = Query(None, description="lakeFS commit of the object to start from (default: its current version)"),
):
    """The OpenLineage RunEvents of an object and of everything it was made from (upstream).

    Built from the FDOs when asked, nothing is stored. A model run leads to the files it read (for a benchmark run:
    the benchmark's generated files, then their source datasets at the pinned version), a dataset to its external
    source. Every platform file has a version. Datasets on the platform are named ``<QID>/<file>`` in the namespace of the DOIP base URL; the lakeFS
    commit is their version.
    """
    if not _QID.fullmatch(object):
        raise HTTPException(status_code=422, detail="object must be a QID, e.g. Q1792420591934")
    if version is not None and not _COMMIT.fullmatch(version):
        raise HTTPException(status_code=422, detail="version must be a full lakeFS commit ID (64 hex characters)")
    try:
        result = lineage.upstream(object, fetch, latest=latest, version=version)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"DOIP error: {e}")
    if result["kind"] is None:
        detail = f"no FDO for {object.upper()}" + (f" at version {version}" if version else "")
        raise HTTPException(status_code=404, detail=detail)
    if format == "ndjson":
        body = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in result["events"])
        return Response(body, media_type="application/x-ndjson")
    return result
