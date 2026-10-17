"""Entries, leaderboards and ranking of scored runs."""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from app.leaderboard.entries import Benchmark
from app.leaderboard.metrics import METRICS

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
    used_columns: list[str] | None = None    # the input columns the model got to see (None: not recorded)
    entry_id: int | None = None
    truth_commit: str = ""
    truth_as_of: str = ""
    scored_at: str = ""


@dataclass
class Leaderboard:
    """A benchmark with its ranked entries."""

    benchmark: Benchmark
    rank_by: str
    truth_points: int
    truth_as_of: str
    entries: list[Entry]
    name: str = ""
    benchmark_id: int = 0
    input_columns: list[str] = field(default_factory=list)      # all columns of the benchmark's input file


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


def rank_entries(entries: list[Entry], rank_by: str, best_per_model: bool = True) -> list[Entry]:
    """Sort entries by ``rank_by`` (best first) and optionally keep each model's best.

    Args:
        entries: Scored entries.
        rank_by: One of ``RANK_METRICS``.
        best_per_model: Keep only the best entry per model name when True.

    Returns:
        The ranked entries.

    Raises:
        ValueError: If ``rank_by`` is not a known metric.
    """
    if rank_by not in RANK_METRICS:
        raise ValueError(f"rank_by must be one of {RANK_METRICS}")
    ordered = sorted(entries, key=sort_key(rank_by))
    if not best_per_model:
        return ordered
    seen: set[str] = set()
    best = []
    for entry in ordered:
        if entry.model_name not in seen:
            seen.add(entry.model_name)
            best.append(entry)
    return best


def finite_or_none(value) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def points_for(forecast: pd.DataFrame, truth: pd.DataFrame) -> list[dict]:
    merged = truth.merge(forecast, on="date", how="left")
    return [
        {
            "date": row["date"].date().isoformat(),
            "forecast": finite_or_none(row.get("forecast")),
            "q10": finite_or_none(row.get("q10")),
            "q90": finite_or_none(row.get("q90")),
            "truth": float(row["truth"]),
        }
        for _, row in merged.iterrows()
    ]
