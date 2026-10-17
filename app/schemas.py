from datetime import date
from pydantic import BaseModel, Field
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
    started_by: str = ""  # pseudonym ``episerve_user_<id>`` of who started the run (empty for older runs)
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


class BenchmarkOut(BaseModel):
    created_by: str | None = None  # the display name of the user who created it, if known
    id: int
    name: str
    state: str
    description: str | None = None
    dataset_qid: str
    target_column: str
    date_column: str
    filter_column: str | None
    filter_value: str | None
    test_start: str
    test_end: str
    history_start: str | None = None
    history_end: str | None = None
    input_filename: str = "input.parquet"
    prediction_length: int | None = None
    truth_points: int | None = None
    data_commit: str | None = None
    entry_count: int | None = None
    created_at: str
    finalized_at: str | None = None
    retired_at: str | None


class BenchmarkCreate(BaseModel):
    name: str
    description: str | None = None
    dataset_qid: str
    target_column: str
    date_column: str
    test_start: date
    test_end: date
    filter_column: str | None = None
    filter_value: str | None = None
    history_start: date | None = None
    history_end: date | None = None
    input_filename: str = "input.parquet"


class DescriptionIn(BaseModel):
    description: str | None = Field(default=None, max_length=4000)


class BenchmarkPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    dataset_qid: str | None = None
    target_column: str | None = None
    date_column: str | None = None
    test_start: date | None = None
    test_end: date | None = None
    filter_column: str | None = None
    filter_value: str | None = None
    history_start: date | None = None
    history_end: date | None = None
    input_filename: str | None = None


class CheckReportOut(BaseModel):
    ok: bool
    problems: list[str]
    warnings: list[str]
    data_commit: str | None = None
    component_name: str | None = None
    truth_points: int | None = None
    history_end: str | None = None
    last_history_date: str | None = None
    prediction_length: int | None = None
    frequency_days: int | None = None
    input_sql: str | None = None


class RunSpecOut(BaseModel):
    benchmark_id: int
    dataset_qid: str
    data_commit: str
    input_data_files: list[list[str]]
    data_transformation_sql: list[str]
    config: dict[str, Any]
    test_start: str
    test_end: str


class EntryCreate(BaseModel):
    run_qid: str


class EntryCreated(BaseModel):
    entry_id: int


class BenchmarkEntryOut(BaseModel):
    rank: int
    entry_id: int | None
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
    truth_commit: str
    truth_as_of: str
    scored_at: str


class BenchmarkLeaderboardOut(BaseModel):
    benchmark: BenchmarkOut
    rank_by: str
    best: str | None
    entries: list[BenchmarkEntryOut]
