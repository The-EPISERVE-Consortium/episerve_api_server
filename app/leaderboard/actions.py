"""Create benchmarks, add and remove entries, read stored boards.

This is the only place that writes the leaderboard. Scores are computed here, once,
when a run is added; reading a board only queries the database.
"""

from datetime import date

import pandas as pd
import requests

from app.clients import ckan as ckan_client
from app.clients import lakefs as lakefs_client
from app.config import settings
from app.leaderboard import loaders, scoring, store
from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError
from app.leaderboard.service import Leaderboard, rank_entries


class NotFound(LookupError):
    """The benchmark, entry, run or dataset does not exist (or is retired)."""


class Conflict(Exception):
    """The run is already on the leaderboard."""


def _dataset(qid: str) -> dict:
    dataset = next((d for d in ckan_client.list_processed_datasets() if d["qid"] == qid), None)
    if dataset is None:
        raise NotFound(f"Dataset {qid!r} not found")
    return dataset


def get_benchmark(benchmark_id: int) -> dict:
    """One benchmark row (retired ones included).

    Raises:
        NotFound: If the id is unknown.
    """
    row = store.get_benchmark(benchmark_id)
    if row is None:
        raise NotFound(f"Benchmark {benchmark_id} not found")
    return row


def _active_benchmark(benchmark_id: int) -> dict:
    row = get_benchmark(benchmark_id)
    if row["retired_at"]:
        raise NotFound(f"Benchmark {benchmark_id} is retired")
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
    """Score a model run against a benchmark and store the entry.

    Returns:
        The new entry id.

    Raises:
        NotFound: Benchmark missing/retired, or run unknown.
        Conflict: The run is already on the leaderboard.
        scoring.NotScorable: The run cannot be scored (reason in the message).
        LoadError: The truth cannot be read.
    """
    row = _active_benchmark(benchmark_id)
    if run_qid in {e.run_qid for e in store.list_entries(benchmark_id)}:
        raise Conflict(f"Run {run_qid} is already on this leaderboard.")
    benchmark = store.benchmark_from_row(row)
    run = next((r for r in ckan_client.list_model_runs() if r["qid"] == run_qid), None)
    if run is None:
        raise NotFound(f"Model run {run_qid!r} not found")
    dataset = _dataset(benchmark.dataset_qid)
    commit = lakefs_client.processed_head_commit()
    truth = loaders.truth_at_commit(dataset, benchmark, commit)
    entry = scoring.score_run(
        benchmark, run, truth, truth_commit=commit, truth_as_of=dataset.get("last_modified", ""),
        load_predictions=_load_predictions,
        load_params=lambda qid: loaders.fetch_run_params(qid, settings.doip_url),
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
        NotFound: Unknown benchmark.
        ValueError: Unknown ``rank_by``.
    """
    row = get_benchmark(benchmark_id)
    entries = store.list_entries(benchmark_id)
    ranked = rank_entries(entries, rank_by, best_per_model)
    return Leaderboard(
        benchmark=store.benchmark_from_row(row), rank_by=rank_by, truth_points=row["truth_points"],
        truth_as_of=max((e.truth_as_of for e in entries), default=""), entries=ranked, not_scorable=[],
        name=row["name"], benchmark_id=row["id"],
    )


def candidates(benchmark_id: int) -> list[dict]:
    """Runs on the benchmark's dataset with the reason each cannot be added (None if it can)."""
    row = _active_benchmark(benchmark_id)
    active = {e.run_qid for e in store.list_entries(benchmark_id)}
    return scoring.candidate_runs(store.benchmark_from_row(row), ckan_client.list_model_runs(), active)
