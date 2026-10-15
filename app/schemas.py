from pydantic import BaseModel
from typing import Any


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
    git_repo: str = ""
    docker_image_created: str = ""
    additional_properties: list[dict[str, Any]] = []


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


class LeaderboardEntry(BaseModel):
    rank: int
    run_qid: str
    model_name: str
    docker_tag: str
    params: dict[str, Any]
    history_start: str | None
    history_end: str
    run_timestamp: str
    n_scored: int
    n_expected: int
    metrics: dict[str, float | None]
    points: list[dict[str, Any]]


class SkippedRunOut(BaseModel):
    run_qid: str
    model_name: str
    reason: str


class LeaderboardResponse(BaseModel):
    dataset_qid: str
    target: str
    date_column: str
    test_start: str
    test_end: str
    filter_column: str | None
    filter_value: str | None
    rank_by: str
    truth_points: int
    truth_as_of: str
    best: str | None
    entries: list[LeaderboardEntry]
    not_scorable: list[SkippedRunOut]
