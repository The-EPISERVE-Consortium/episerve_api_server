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
    # Sections of the model card (spec model-card v1 of episerve_specs); None when the card has none.
    task: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = None   # JSON Schema of config.json
    inputs: list[dict[str, Any]] | None = None
    outputs: list[dict[str, Any]] | None = None
    resources: dict[str, Any] | None = None
    documentation: dict[str, Any] | None = None
    card_valid: bool | None = None             # None: not checked
    card_issues: list[dict[str, Any]] = []


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
    executed_by: str = ""  # pseudonym ``episerve_user_<id>`` of who executed the run (empty for older runs)
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
    input_sources: list[str] = []  # everything the run read, with versions (from its provenance)


class FilterIn(BaseModel):
    """One equality filter of a benchmark's series: ``column = value``."""

    column: str
    value: str


class CovariateIn(BaseModel):
    """A further data source of a benchmark; its columns are added to the model input."""

    name: str
    dataset_qid: str
    date_column: str
    columns: list[str]
    filters: list[FilterIn] = []
    aggregation: str = "mean"   # mean | sum | last


class BenchmarkOut(BaseModel):
    created_by: str | None = None  # the display name of the user who created it, if known
    id: int
    name: str
    state: str
    description: str | None = None
    dataset_qid: str
    target_column: str
    date_column: str
    filter_column: str | None  # the first filter (kept for older clients); all of them are in ``filters``
    filter_value: str | None
    filters: list[FilterIn] = []
    covariates: list[CovariateIn] = []
    generated_files: dict | None = None  # with covariates, once final: the generated input and reference files
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
    filters: list[FilterIn] | None = None  # any number; when given it is used instead of filter_column / filter_value
    covariates: list[CovariateIn] = []
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
    filters: list[FilterIn] | None = None  # replaces all filters (an empty list removes them)
    covariates: list[CovariateIn] | None = None  # replaces all covariates (an empty list removes them)
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
    columns: list[str] | None = None          # with covariates: the columns of the generated input file
    missing: dict[str, float] | None = None   # share of empty values per covariate column
    input_rows: int | None = None


class RunSpecOut(BaseModel):
    benchmark_id: int
    dataset_qid: str
    data_commit: str
    input_data_files: list[list[str]]
    data_transformation_sql: list[str]
    config: dict[str, Any]
    test_start: str
    test_end: str
    input_columns: list[str] | None = None    # with covariates: the columns of the generated input file
    date_column: str | None = None
    target_column: str | None = None
    covariate_columns: list[str] | None = None


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
    used_columns: list[str] | None = None   # the input columns the model got to see (null: not recorded)
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
