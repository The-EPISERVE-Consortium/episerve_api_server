"""Assemble a ranked leaderboard from catalog runs, predictions and observations."""

import math
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Callable

import pandas as pd

from app.clients import ckan as ckan_client
from app.config import settings
from app.leaderboard import loaders
from app.leaderboard.entries import Benchmark, qualify_run
from app.leaderboard.loaders import LoadError
from app.leaderboard.metrics import METRICS, score_forecast

RANK_METRICS = METRICS
TARGET_COVERAGE = 0.8


@dataclass
class Entry:
    """One scored run."""

    run_qid: str
    model_name: str
    docker_tag: str
    params: dict
    history_start: date | None
    history_end: date
    run_timestamp: str
    n_scored: int
    n_expected: int
    metrics: dict[str, float | None]
    points: list[dict] = field(default_factory=list)


@dataclass
class SkippedRun:
    """A run on the benchmark's dataset that could not be scored."""

    run_qid: str
    model_name: str
    reason: str


@dataclass
class Leaderboard:
    """Ranked entries plus the runs that were left out and why."""

    benchmark: Benchmark
    rank_by: str
    truth_points: int
    truth_as_of: str
    entries: list[Entry]
    not_scorable: list[SkippedRun]


def sort_key(metric: str) -> Callable[[Entry], tuple]:
    """Sort key for ``metric``: lower is better, coverage nearest 0.8, None last."""

    def key(entry: Entry) -> tuple:
        value = entry.metrics.get(metric)
        if value is None:
            return (1, 0.0)
        if metric == "coverage":
            value = abs(value - TARGET_COVERAGE)
        return (0, value)

    return key


def _num(value) -> float | None:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else float(value)


def _points(forecast: pd.DataFrame, truth: pd.DataFrame) -> list[dict]:
    merged = truth.merge(forecast, on="date", how="left")
    return [
        {
            "date": row["date"].date().isoformat(),
            "forecast": _num(row.get("forecast")),
            "q10": _num(row.get("q10")),
            "q90": _num(row.get("q90")),
            "truth": float(row["truth"]),
        }
        for _, row in merged.iterrows()
    ]


def build_leaderboard(
    benchmark: Benchmark,
    runs: list[dict],
    dataset: dict,
    *,
    rank_by: str = "interval_score",
    best_per_model: bool = True,
    load_truth: Callable[[dict, Benchmark], pd.DataFrame],
    load_predictions: Callable[[dict, Benchmark], pd.DataFrame],
    load_params: Callable[[str], dict],
) -> Leaderboard:
    """Score every qualifying run and rank the results.

    Args:
        benchmark: The prediction task.
        runs: Catalog runs from ``ckan_client.list_model_runs()``.
        dataset: Catalog record of the benchmark's dataset.
        rank_by: One of ``METRICS``.
        best_per_model: Keep only each model's best run when True.
        load_truth: ``(dataset, benchmark) -> DataFrame[date, truth]``.
        load_predictions: ``(run, benchmark) -> DataFrame``; may raise ``LoadError``.
        load_params: ``(run_qid) -> dict`` of run parameters.

    Returns:
        The leaderboard.

    Raises:
        ValueError: If ``rank_by`` is not a known metric.
        LoadError: If the observations cannot be loaded.
    """
    if rank_by not in RANK_METRICS:
        raise ValueError(f"rank_by must be one of {RANK_METRICS}")
    truth = load_truth(dataset, benchmark)
    entries: list[Entry] = []
    skipped: list[SkippedRun] = []
    for run in runs:
        verdict = qualify_run(run, benchmark)
        if not verdict.relevant:
            continue
        name = run.get("model_name", "")
        if verdict.reason:
            skipped.append(SkippedRun(run["qid"], name, verdict.reason))
            continue
        try:
            forecast = load_predictions(run, benchmark)
        except LoadError as error:
            skipped.append(SkippedRun(run["qid"], name, str(error)))
            continue
        result = score_forecast(forecast, truth)
        if result.n_scored < result.n_expected:
            skipped.append(SkippedRun(
                run["qid"], name, f"forecast covers {result.n_scored} of {result.n_expected} dates in the test window",
            ))
            continue
        entries.append(Entry(
            run_qid=run["qid"], model_name=name, docker_tag=run.get("docker_tag", ""),
            params=load_params(run["qid"]), history_start=verdict.window.start,
            history_end=verdict.window.end, run_timestamp=run.get("run_timestamp", ""),
            n_scored=result.n_scored, n_expected=result.n_expected, metrics=result.metrics,
            points=_points(forecast, truth),
        ))
    entries.sort(key=sort_key(rank_by))
    if best_per_model:
        seen, best = set(), []
        for entry in entries:
            if entry.model_name not in seen:
                seen.add(entry.model_name)
                best.append(entry)
        entries = best
    return Leaderboard(
        benchmark=benchmark, rank_by=rank_by, truth_points=len(truth),
        truth_as_of=dataset.get("last_modified", ""), entries=entries, not_scorable=skipped,
    )


@lru_cache(maxsize=512)
def _cached_predictions(resource_id: str, benchmark: Benchmark) -> pd.DataFrame:
    return loaders.load_predictions(ckan_client.get_datastore_records(resource_id), benchmark)


@lru_cache(maxsize=256)
def _cached_params(qid: str, doip_base: str) -> dict:
    return loaders.fetch_run_params(qid, doip_base)


@lru_cache(maxsize=16)
def _cached_truth(dataset_qid: str, last_modified: str, benchmark: Benchmark, doip_base: str) -> pd.DataFrame:
    dataset = next(d for d in ckan_client.list_processed_datasets() if d["qid"] == dataset_qid)
    return loaders.load_truth(dataset, benchmark, doip_base)


def default_loaders(doip_base: str | None = None) -> dict:
    """Loader callables for ``build_leaderboard`` wired to CKAN and DOIP.

    Predictions and parameters are immutable per run, so they are cached by run;
    observations are cached by the dataset's ``last_modified`` timestamp, so a
    refresh of the dataset invalidates them.
    """
    base = doip_base or settings.doip_url

    def load_predictions(run: dict, benchmark: Benchmark) -> pd.DataFrame:
        if not run.get("predictions_resource_id"):
            raise LoadError("run has no predictions.tsv in CKAN")
        return _cached_predictions(run["predictions_resource_id"], benchmark).copy()

    return {
        "load_truth": lambda dataset, benchmark: _cached_truth(
            dataset["qid"], dataset.get("last_modified", ""), benchmark, base
        ).copy(),
        "load_predictions": load_predictions,
        "load_params": lambda qid: dict(_cached_params(qid, base)),
    }
