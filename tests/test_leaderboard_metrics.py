"""Unit tests for forecast scoring metrics."""

import math

import pandas as pd
import pytest

from app.leaderboard.metrics import score_forecast


def _truth(values, start="2022-01-05"):
    dates = pd.date_range(start, periods=len(values), freq="7D")
    return pd.DataFrame({"date": dates, "truth": values})


def _forecast(values, q10=None, q90=None, start="2022-01-05"):
    dates = pd.date_range(start, periods=len(values), freq="7D")
    frame = pd.DataFrame({"date": dates, "forecast": values})
    if q10 is not None:
        frame["q10"] = q10
        frame["q90"] = q90
    return frame


def test_point_metrics():
    result = score_forecast(_forecast([12.0, 18.0]), _truth([10.0, 20.0]))
    assert result.metrics["mae"] == pytest.approx(2.0)
    assert result.metrics["rmse"] == pytest.approx(2.0)
    assert (result.n_expected, result.n_scored) == (2, 2)


def test_interval_metrics():
    # y=10 is inside [8, 14] (width 6); y=20 is 1 above 19 (width 4 + 10 * 1).
    result = score_forecast(
        _forecast([12.0, 18.0], q10=[8.0, 15.0], q90=[14.0, 19.0]), _truth([10.0, 20.0])
    )
    assert result.metrics["coverage"] == pytest.approx(0.5)
    assert result.metrics["interval_score"] == pytest.approx(10.0)


def test_below_interval_is_penalised():
    result = score_forecast(
        _forecast([5.0], q10=[6.0], q90=[9.0]), _truth([4.0])
    )
    # width 3 + (2/0.2) * (6 - 4) = 23
    assert result.metrics["interval_score"] == pytest.approx(23.0)
    assert result.metrics["coverage"] == 0.0


def test_missing_quantile_columns_leave_interval_metrics_none():
    result = score_forecast(_forecast([12.0]), _truth([10.0]))
    assert result.metrics["coverage"] is None
    assert result.metrics["interval_score"] is None
    assert result.metrics["mae"] == pytest.approx(2.0)


def test_forecast_missing_a_truth_date_counts_as_unscored():
    forecast = _forecast([12.0])  # only the first of two truth dates
    result = score_forecast(forecast, _truth([10.0, 20.0]))
    assert (result.n_expected, result.n_scored) == (2, 1)


def test_nan_forecast_values_are_not_scored():
    forecast = _forecast([float("nan"), 18.0])
    result = score_forecast(forecast, _truth([10.0, 20.0]))
    assert result.n_scored == 1
    assert result.metrics["mae"] == pytest.approx(2.0)


def test_nothing_scored_gives_none_metrics_not_nan():
    result = score_forecast(_forecast([1.0], start="2030-01-02"), _truth([10.0]))
    assert result.n_scored == 0
    assert all(value is None for value in result.metrics.values())
    assert not any(isinstance(v, float) and math.isnan(v) for v in result.metrics.values())
