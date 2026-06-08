from fastapi import APIRouter, Depends, HTTPException
from app.auth import require_auth
from app.clients import ckan as ckan_client
from app.schemas import Model

router = APIRouter(prefix="/models", tags=["models"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[Model])
def get_models():
    try:
        return ckan_client.list_models()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")
