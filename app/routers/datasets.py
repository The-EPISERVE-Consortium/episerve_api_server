from fastapi import APIRouter, HTTPException
from app.clients import ckan as ckan_client
from app.schemas import RawDataset, ProcessedDataset

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("/raw", response_model=list[RawDataset])
def get_raw_datasets():
    try:
        return ckan_client.list_raw_datasets()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")


@router.get("/processed", response_model=list[ProcessedDataset])
def get_processed_datasets():
    try:
        return ckan_client.list_processed_datasets()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")
