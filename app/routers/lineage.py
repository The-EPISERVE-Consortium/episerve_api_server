"""Upstream lineage of an object as OpenLineage events, built from the FDO records (see app/lineage.py)."""

import json
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app import lineage
from app.clients import doip as doip_client
from app.clients import lakefs as lakefs_client

router = APIRouter(prefix="/lineage", tags=["lineage"])

_QID = re.compile(r"[Qq]\d{6,}")


def fetch(qid: str, version: str | None) -> dict | None:
    """An FDO: at a commit of data-processed when a version is given (None if it is not there), else the current one."""
    if version:
        try:
            return lakefs_client.read_processed_fdo(version, qid)
        except Exception:
            return None
    return doip_client.fetch_fdo(qid)


@router.get("/events")
def get_events(
    object: str = Query(..., description="QID of a dataset, benchmark data object or model run"),
    format: str = Query("json", pattern="^(json|ndjson)$",
                        description="json: object with events and unresolved links; ndjson: one event per line"),
):
    """The OpenLineage RunEvents of an object and of everything it was made from (upstream).

    Built from the FDOs when asked, nothing is stored. A model run leads to the files it read (for a benchmark run:
    the benchmark's generated files, then their source datasets at the pinned version), a dataset to its external
    source. Datasets on the platform are named ``<QID>/<file>`` in the namespace of the DOIP base URL; the lakeFS
    commit is their version.
    """
    if not _QID.fullmatch(object):
        raise HTTPException(status_code=422, detail="object must be a QID, e.g. Q1792420591934")
    try:
        result = lineage.upstream(object, fetch)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"DOIP error: {e}")
    if result["kind"] is None:
        raise HTTPException(status_code=404, detail=f"no FDO for {object.upper()}")
    if format == "ndjson":
        body = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in result["events"])
        return Response(body, media_type="application/x-ndjson")
    return result
