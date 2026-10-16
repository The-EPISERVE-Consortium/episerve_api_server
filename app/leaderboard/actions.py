"""Create benchmarks, add and remove entries, read stored boards.

This is the only place that writes the leaderboard. Scores are computed here, once,
when a run is added; reading a board only queries the database.
"""

import logging
from datetime import date

import pandas as pd
import requests

from app.benchmarks import store as bstore
from app.benchmarks.errors import Conflict, NotFound
from app.clients import ckan as ckan_client
from app.clients import lakefs as lakefs_client
from app.config import settings
from app.leaderboard import loaders, run_inputs, scoring, store
from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError
from app.leaderboard.run_inputs import RunInput
from app.leaderboard.service import Leaderboard, rank_entries

logger = logging.getLogger(__name__)


def _dataset(qid: str) -> dict:
    dataset = next((d for d in ckan_client.list_processed_datasets() if d["qid"] == qid), None)
    if dataset is None:
        raise NotFound(f"Dataset {qid!r} not found")
    return dataset


def _final_benchmark(benchmark_id: int) -> dict:
    row = bstore.get(benchmark_id)
    if row is None or bstore.state(row) != "final":
        raise NotFound(f"Benchmark {benchmark_id} not found or not final")
    return row


def _load_inputs(run_qid: str) -> list[RunInput] | None:
    """The run's inputs from its DOIP provenance, or None when they cannot be read."""
    try:
        return run_inputs.fetch_run_inputs(run_qid, settings.doip_url)
    except Exception:
        logger.exception("could not read the provenance of run %s", run_qid)
        return None


def get_benchmark(benchmark_id: int) -> dict:
    """One benchmark row (retired ones included).

    Raises:
        NotFound: If the id is unknown.
    """
    row = store.get_benchmark(benchmark_id)
    if row is None:
        raise NotFound(f"Benchmark {benchmark_id} not found")
    return row


def create_benchmark(
    name: str,
    dataset_qid: str,
    target_column: str,
    date_column: str,
    test_start: date,
    test_end: date,
    filter_column: str | None = None,
    filter_value: str | None = None,
) -> int:
    """Validate a prediction task against the current data and store it.

    Raises:
        LoadError: Invalid input, or the truth window cannot be read / has no observations.
        NotFound: Unknown dataset.
    """
    if not (name or "").strip():
        raise LoadError("A name is required.")
    if not (target_column or "").strip() or not (date_column or "").strip():
        raise LoadError("Target column and date column are required.")
    for label, value, limit in (
        ("Name", name, 200), ("Target column", target_column, 200), ("Date column", date_column, 200),
        ("Filter column", filter_column, 200), ("Filter value", filter_value, 500),
    ):
        if value and len(value.strip() if label != "Filter value" else value) > limit:
            raise LoadError(f"{label} must be at most {limit} characters.")
    if test_start > test_end:
        raise LoadError("The test start must not be after the test end.")
    if filter_column and not filter_value:
        raise LoadError("A filter value is required with a filter column.")
    benchmark = Benchmark(
        dataset_qid=dataset_qid, target_column=target_column.strip(), date_column=date_column.strip(),
        test_start=test_start, test_end=test_end,
        filter_column=filter_column or None, filter_value=filter_value if filter_column else None,
    )
    dataset = _dataset(dataset_qid)
    commit = lakefs_client.processed_head_commit()
    truth = loaders.truth_at_commit(dataset, benchmark, commit)
    return store.create_benchmark(name.strip(), benchmark, truth_points=len(truth))


def _load_predictions(run: dict, benchmark: Benchmark) -> pd.DataFrame:
    resource_id = run.get("predictions_resource_id")
    if not resource_id:
        raise LoadError("run has no predictions.tsv in CKAN")
    try:
        records = ckan_client.get_datastore_records(resource_id)
    except (requests.RequestException, RuntimeError) as exc:
        raise LoadError("predictions could not be read from the CKAN DataStore") from exc
    return loaders.load_predictions(records, benchmark)


def add_run(benchmark_id: int, run_qid: str) -> int:
    """Score a model run against a final benchmark and store the entry.

    The truth is read at the benchmark's pinned ``data_commit`` (the lakeFS head is not
    consulted), and the run is accepted only if its provenance shows its input at exactly
    that commit.

    Returns:
        The new entry id.

    Raises:
        NotFound: Benchmark missing, draft or retired, or run unknown.
        Conflict: The run is already on the leaderboard.
        scoring.NotScorable: The run cannot be scored (reason in the message).
        LoadError: The truth cannot be read.
    """
    row = _final_benchmark(benchmark_id)
    if run_qid in {e.run_qid for e in store.list_entries(benchmark_id)}:
        raise Conflict(f"Run {run_qid} is already on this leaderboard.")
    benchmark = bstore.benchmark_from_row(row)
    run = next((r for r in ckan_client.list_model_runs() if r["qid"] == run_qid), None)
    if run is None:
        raise NotFound(f"Model run {run_qid!r} not found")
    dataset = _dataset(benchmark.dataset_qid)
    commit = row["data_commit"]
    truth = loaders.truth_at_commit(dataset, benchmark, commit)
    inputs = _load_inputs(run_qid)
    entry = scoring.score_run(
        benchmark, run, truth, truth_commit=commit, truth_as_of=row["finalized_at"],
        load_predictions=_load_predictions,
        load_params=lambda qid: loaders.fetch_run_params(qid, settings.doip_url),
        inputs=inputs,
    )
    try:
        return store.add_entry(benchmark_id, entry)
    except store.DuplicateEntry as exc:
        raise Conflict(str(exc)) from exc


def remove_entry(benchmark_id: int, entry_id: int) -> None:
    """Soft-remove an entry (recompute by adding the run again).

    Raises:
        NotFound: No active entry with that id on the benchmark.
    """
    if not store.remove_entry(benchmark_id, entry_id):
        raise NotFound(f"Entry {entry_id} not found on benchmark {benchmark_id}")


def retire_benchmark(benchmark_id: int) -> None:
    """Hide a benchmark from the list.

    Raises:
        NotFound: Unknown or already retired.
    """
    get_benchmark(benchmark_id)
    if not store.retire_benchmark(benchmark_id):
        raise NotFound(f"Benchmark {benchmark_id} not found or already retired")


def read_leaderboard(benchmark_id: int, rank_by: str = "interval_score", best_per_model: bool = True) -> Leaderboard:
    """The stored, ranked board; database only.

    Raises:
        NotFound: Unknown or draft benchmark.
        ValueError: Unknown ``rank_by``.
    """
    row = bstore.get(benchmark_id)
    if row is None or bstore.state(row) == "draft":
        raise NotFound(f"Benchmark {benchmark_id} not found")
    entries = store.list_entries(benchmark_id)
    ranked = rank_entries(entries, rank_by, best_per_model)
    return Leaderboard(
        benchmark=bstore.benchmark_from_row(row), rank_by=rank_by, truth_points=row["truth_points"] or 0,
        truth_as_of=max((e.truth_as_of for e in entries), default=""), entries=ranked,
        name=row["name"], benchmark_id=row["id"],
    )


def candidates(benchmark_id: int) -> list[dict]:
    """Runs on the benchmark's dataset with the reason each cannot be added (None if it can)."""
    row = _final_benchmark(benchmark_id)
    active = {e.run_qid for e in store.list_entries(benchmark_id)}
    return scoring.candidate_runs(
        bstore.benchmark_from_row(row), ckan_client.list_model_runs(), active, load_inputs=_load_inputs,
    )
