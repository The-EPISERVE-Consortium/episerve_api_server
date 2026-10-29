from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from app.clients import ckan as ckan_client
from app.routers.specs import read_json_body
from app.model_config import config_problems, model_config_defaults
from app.schemas import ConfigCheckRequest, ConfigCheckResult, Model

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[Model])
def get_models():
    try:
        return ckan_client.list_models()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")


@router.post(
    "/{model}/check-config",
    response_model=ConfigCheckResult,
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": ConfigCheckRequest.model_json_schema()}}}},
)
async def check_config(model: str, http_request: Request):
    """Check a run config (body: ``{"config": {...}}``) against the parameters in the model's card, without starting anything.

    ``model`` is the model's name or QID. The problems are the ones POST /model-runs would refuse.
    """
    try:
        request = ConfigCheckRequest.model_validate(await read_json_body(http_request))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_url=False))
    try:
        models = await run_in_threadpool(ckan_client.list_models)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")
    match = next((m for m in models if model in (m["name"], m["qid"]) or model.upper() == m["qid"].upper()), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"unknown model '{model}'")
    parameters = match.get("parameters")
    if not parameters:
        return ConfigCheckResult(model=match["name"], qid=match["qid"], valid=None, config=request.config)
    problems = config_problems(request.config, parameters)
    return ConfigCheckResult(
        model=match["name"],
        qid=match["qid"],
        valid=not problems,
        problems=problems,
        config={**model_config_defaults(parameters), **request.config},
    )
