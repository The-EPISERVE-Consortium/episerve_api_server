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
    lakefs_path: str
    last_modified: str
    doip_url: str


class Model(BaseModel):
    name: str
    docker_image: str
    docker_tag: str
    description: str


class ModelRunRequest(BaseModel):
    model_image: str
    model_tag: str = "latest"
    input_path: str
    config: dict[str, Any]


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
