"""Specs of platform formats (from episerve_specs): their JSON Schemas, and validating a document.

The schemas are served at the URLs documents name in ``conformsTo``
(``https://api.episerve.zib.de/schemas/<spec>/v<N>.json``). Validation gives the same
result as the ``episerve-validate`` CLI and the sync flow; an invalid document is a normal
answer (``valid: false``), not an HTTP error.
"""

import json

from episerve_specs import available_specs, load_schema, schema_url, validate
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.schemas import SpecInfo, ValidationResult

router = APIRouter(tags=["specs"])

MAX_DOCUMENT_BYTES = 1024 * 1024


async def read_json_body(request: Request):
    """The parsed JSON body; 413 when it is larger than MAX_DOCUMENT_BYTES, 400 when it is not JSON."""
    max_bytes = MAX_DOCUMENT_BYTES
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Body larger than {max_bytes} bytes")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Body is not JSON: {e}")


@router.get("/schemas", response_model=dict[str, SpecInfo])
def list_schemas():
    """Every spec, its versions and the URL of each version's schema."""
    return {
        spec: SpecInfo(versions=versions, schemas={v: schema_url(spec, v) for v in versions})
        for spec, versions in available_specs().items()
    }


@router.get("/schemas/{spec}/v{version}.json", responses={200: {"content": {"application/schema+json": {}}}})
def get_schema(spec: str, version: str):
    """The JSON Schema of one spec version."""
    try:
        schema = load_schema(spec, version)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return JSONResponse(schema, media_type="application/schema+json")


@router.post(
    "/validate/{spec}",
    response_model=ValidationResult,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object", "description": "The document, e.g. a model's fdo.json"}}}}},
)
async def validate_document(spec: str, request: Request, version: str | None = None):
    """Validate a JSON document (the request body) against a spec, e.g. a model's fdo.json against ``model-card``.

    The version is ``version`` if given, else the one the document's ``conformsTo`` names, else the newest.
    """
    if spec not in available_specs():
        raise HTTPException(status_code=404, detail=f"unknown spec '{spec}' (known: {', '.join(available_specs())})")
    document = await read_json_body(request)
    try:
        result = validate(document, spec=spec, version=version)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {**result.to_dict(), "schema": schema_url(result.spec, result.version)}
