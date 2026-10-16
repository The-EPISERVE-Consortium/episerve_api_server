"""Assemble a ranked leaderboard from catalog runs, predictions and observations."""

import copy
import math
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd
import requests

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
    entry_id: int | None = None
    truth_commit: str = ""
    truth_as_of: str = ""
    scored_at: str = ""


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
    name: str = ""
    benchmark_id: int = 0


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
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


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
            n_scored=result.n_scored, n_expected=result.n_expected, metrics={name: _num(value) for name, value in result.metrics.items()},
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


_CACHE_LIMIT = 16
_lock = threading.Lock()
_predictions_cache: dict = {}
_params_cache: dict = {}
_truth_cache: dict = {}


def clear_caches() -> None:
    """Empty every loader cache."""
    with _lock:
        _predictions_cache.clear()
        _params_cache.clear()
        _truth_cache.clear()


def _remember(cache: dict, key, value, limit: int) -> None:
    with _lock:
        cache[key] = value
        while len(cache) > limit:
            cache.pop(next(iter(cache)))


def _cached_predictions(resource_id: str, benchmark: Benchmark) -> pd.DataFrame:
    key = (resource_id, benchmark)
    with _lock:
        hit = _predictions_cache.get(key)
    if hit is None:
        hit = loaders.load_predictions(ckan_client.get_datastore_records(resource_id), benchmark)
        _remember(_predictions_cache, key, hit, 512)
    return hit.copy()


def _cached_params(qid: str, doip_base: str) -> dict:
    key = (qid, doip_base)
    with _lock:
        hit = _params_cache.get(key)
    if hit is None:
        hit = loaders.fetch_run_params(qid, doip_base)
        if hit:  # an empty result means the fetch failed; retry next time
            _remember(_params_cache, key, hit, 256)
    return copy.deepcopy(hit)


def _cached_truth(dataset: dict, benchmark: Benchmark, doip_base: str) -> pd.DataFrame:
    key = (dataset["qid"], dataset.get("last_modified", ""), benchmark, doip_base)
    with _lock:
        hit = _truth_cache.get(key)
    if hit is None:
        hit = loaders.load_truth(dataset, benchmark, doip_base)
        _remember(_truth_cache, key, hit, _CACHE_LIMIT)
    return hit.copy()


def default_loaders(doip_base: str | None = None) -> dict:
    """Loader callables for ``build_leaderboard`` wired to CKAN and DOIP.

    Predictions and parameters are immutable per run, so they are cached by run
    (empty parameter results are not cached); observations are cached by the
    dataset's ``last_modified`` timestamp, so a refresh of the dataset
    invalidates them.
    """
    base = doip_base or settings.doip_url

    def load_predictions(run: dict, benchmark: Benchmark) -> pd.DataFrame:
        if not run.get("predictions_resource_id"):
            raise LoadError("run has no predictions.tsv in CKAN")
        try:
            return _cached_predictions(run["predictions_resource_id"], benchmark)
        except (requests.RequestException, RuntimeError) as exc:
            raise LoadError("predictions could not be read from the CKAN DataStore") from exc

    return {
        "load_truth": lambda dataset, benchmark: _cached_truth(dataset, benchmark, base),
        "load_predictions": load_predictions,
        "load_params": lambda qid: _cached_params(qid, base),
    }
