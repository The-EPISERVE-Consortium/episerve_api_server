from pydantic import BaseModel
from typing import Any


class RawDataset(BaseModel):
    path: str
    last_modified: str
    size_bytes: int


class ProcessedDataset(BaseModel):
    qid: str
    name: str
    description: str
    source_url: str
    data_path: str
    last_modified: str
    metadata_created: str
    doip_url: str


class Model(BaseModel):
    qid: str = ""
    name: str
    docker_image: str
    docker_tag: str
    description: str
    doip_url: str = ""


class ModelRunRequest(BaseModel):
    model_image: str
    model_tag: str = "latest"
    input_data_files: list[list[str]]
    config: dict[str, Any]
    data_transformation_sql: list[str] | None = None


class ModelRunResponse(BaseModel):
    run_id: str
    prefect_flow_run_id: str
    status: str


class ModelRunStatus(BaseModel):
    run_id: str
    qid: str
    model_name: str
    docker_tag: str
    status: str
    run_timestamp: str
    computation_time: str
    input_files: list[str]
    output_files: list[str]
    doip_url: str
