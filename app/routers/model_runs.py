import json
from fastapi import APIRouter, HTTPException
from app.clients import lakefs as lakefs_client
from app.clients import prefect as prefect_client
from app.schemas import ModelRunRequest, ModelRunResponse, ModelRunStatus

router = APIRouter(prefix="/model-runs", tags=["model-runs"])


@router.post("", response_model=ModelRunResponse, status_code=202)
def start_model_run(request: ModelRunRequest):
    try:
        result = prefect_client.trigger_model_run(
            input_path=request.input_path,
            model_image=request.model_image,
            model_tag=request.model_tag,
            config_json=json.dumps(request.config),
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
        return lakefs_client.list_model_runs()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"lakeFS error: {e}")


@router.get("/{run_id}", response_model=ModelRunStatus)
def get_model_run(run_id: str):
    try:
        run = lakefs_client.get_model_run(run_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"lakeFS error: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Model run '{run_id}' not found")
    return run
