import json
from fastapi import APIRouter, Depends, HTTPException, Response
from app import users
from app.auth import require_right
from app.users import User
from app.clients import ckan as ckan_client
from app.clients import prefect as prefect_client
from app.clients import doip as doip_client
from app.model_snapshot import run_model_snapshot
from app.model_config import config_problems, model_params
from app.schemas import ModelRunRequest, ModelRunResponse, ModelRunStatus

router = APIRouter(prefix="/model-runs", tags=["model-runs"])


@router.post("", response_model=ModelRunResponse, status_code=202)
def start_model_run(request: ModelRunRequest, user: User = Depends(require_right("runs"))):
    """Schedule a model run, preserving any requested immutable image identity.

    Args:
        request: Validated run parameters.
        user: Authorized user starting the run.

    Returns:
        Scheduled Prefect flow identifier and state.

    Raises:
        HTTPException: Configuration is invalid or Prefect is unavailable.
    """
    params = None if request.model_digest else model_params(request.model_image)
    problems = config_problems(request.config, params) if params is not None else []
    if problems:
        raise HTTPException(status_code=422, detail="Invalid model config: " + "; ".join(problems))
    try:
        result = prefect_client.trigger_model_run(
            input_data_files=request.input_data_files,
            model_image=request.model_image,
            model_tag=request.model_tag,
            model_digest=request.model_digest,
            config_json=json.dumps(request.config),
            data_transformation_sql=request.data_transformation_sql,
            executed_by=users.pseudonym(user),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Prefect error: {e}")

    return ModelRunResponse(
        run_id=result["prefect_flow_run_id"],
        prefect_flow_run_id=result["prefect_flow_run_id"],
        status=result["status"],
    )


@router.get("", response_model=list[ModelRunStatus])
def list_model_runs():
    try:
        return ckan_client.list_model_runs()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")


@router.get("/{run_id}", response_model=ModelRunStatus)
def get_model_run(run_id: str):
    """Read catalog run details and enrich their model identity from the run FDO.

    Args:
        run_id: Catalog model-run QID.

    Returns:
        Run status with the recorded image digest and source revision.

    Raises:
        HTTPException: The run is unknown or an upstream metadata service fails.
    """
    try:
        runs = ckan_client.list_model_runs()
        run = next((r for r in runs if r["qid"].upper() == run_id.upper()), None)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Model run '{run_id}' not found")
    try:
        fdo = doip_client.fetch_fdo(run["qid"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Run provenance is unavailable") from exc
    return {**run, **run_model_snapshot(fdo)} if fdo else run


@router.get("/{run_id}/forecast-preview")
def forecast_preview(run_id: str, response: Response, version: str | None = None):
    """Return a lazy, versioned preview of supplied input and recorded forecasts.

    Args:
        run_id: Public model-run QID.
        response: Response carrying cache-control headers.
        version: Optional exact artifact commit.

    Returns:
        Generic chart data from the database cache or freshly reconstructed artifacts.

    Raises:
        HTTPException: Input, access, availability, or generation failure.
    """
    from app.forecast_preview import cache, service
    import logging

    response.headers['Cache-Control'] = 'no-store'
    try:
        return service.preview(run_id, version)
    except service.PreviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={'Cache-Control': 'no-store'}) from exc
    except cache.PreviewBusy as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={'Retry-After': '3', 'Cache-Control': 'no-store'}) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception('Forecast preview failed for %s', run_id)
        raise HTTPException(status_code=503, detail='The preview service is unavailable. Please retry shortly.',
                            headers={'Cache-Control': 'no-store'}) from exc
