"""Score a forecast against observed values."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

ALPHA = 0.2
METRICS = ("mae", "rmse", "coverage", "interval_score")


@dataclass(frozen=True)
class ScoreResult:
    """Scores for one forecast over one truth window.

    Attributes:
        n_expected: Number of truth dates in the window.
        n_scored: Truth dates that had a usable forecast.
        metrics: Metric name to value; ``None`` when it cannot be computed.
    """

    n_expected: int
    n_scored: int
    metrics: dict[str, float | None]


def score_forecast(forecast: pd.DataFrame, truth: pd.DataFrame) -> ScoreResult:
    """Compare forecast values with observations on matching dates.

    Args:
        forecast: Columns ``date``, ``forecast`` and optionally ``q10``/``q90``.
        truth: Columns ``date`` and ``truth``.

    Returns:
        The counts and the metrics. Interval metrics are ``None`` unless every
        scored row has both ``q10`` and ``q90``, or nothing could be scored.
    """
    merged = truth.merge(forecast, on="date", how="left")
    scored = merged.dropna(subset=["forecast", "truth"])
    metrics: dict[str, float | None] = {name: None for name in METRICS}
    if not scored.empty:
        error = scored["forecast"] - scored["truth"]
        metrics["mae"] = float(error.abs().mean())
        metrics["rmse"] = float(np.sqrt((error ** 2).mean()))
        if {"q10", "q90"} <= set(scored.columns):
            if not scored[["q10", "q90"]].isna().any().any():
                low, high, y = scored["q10"], scored["q90"], scored["truth"]
                metrics["coverage"] = float(((y >= low) & (y <= high)).mean())
                penalty = 2 / ALPHA
                interval = (high - low) + penalty * (low - y).clip(lower=0) + penalty * (y - high).clip(lower=0)
                metrics["interval_score"] = float(interval.mean())
    return ScoreResult(n_expected=len(truth), n_scored=len(scored), metrics=metrics)
