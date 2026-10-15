"""Tests for the leaderboard data loaders (no network)."""

from datetime import date
from io import BytesIO

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.leaderboard import loaders
from app.leaderboard.entries import Benchmark

BENCH = Benchmark(
    dataset_qid="Q1", target_column="value", date_column="datum",
    test_start=date(2022, 1, 1), test_end=date(2022, 1, 31),
)


def test_load_predictions_casts_text_and_clips_to_window():
    records = [
        {"datum": "2021-12-29", "value": "1", "value_q10": "0", "value_q90": "2"},
        {"datum": "2022-01-05", "value": "10.5", "value_q10": "9", "value_q90": "12"},
        {"datum": "2022-02-02", "value": "3", "value_q10": "1", "value_q90": "5"},
    ]
    frame = loaders.load_predictions(records, BENCH)
    assert list(frame.columns) == ["date", "forecast", "q10", "q90"]
    assert frame["date"].tolist() == [pd.Timestamp("2022-01-05")]
    assert frame["forecast"].tolist() == [10.5]


def test_load_predictions_without_quantiles_omits_them():
    frame = loaders.load_predictions([{"datum": "2022-01-05", "value": "1"}], BENCH)
    assert list(frame.columns) == ["date", "forecast"]


def test_load_predictions_non_numeric_text_becomes_nan():
    frame = loaders.load_predictions([{"datum": "2022-01-05", "value": ""}], BENCH)
    assert frame["forecast"].isna().all()


def test_load_predictions_missing_target_column_raises():
    with pytest.raises(loaders.LoadError, match="value"):
        loaders.load_predictions([{"datum": "2022-01-05", "other": "1"}], BENCH)


def test_load_predictions_unparseable_date_raises():
    with pytest.raises(loaders.LoadError, match="date"):
        loaders.load_predictions([{"datum": "not-a-date", "value": "1"}], BENCH)


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame), buffer)
    return buffer.getvalue()


def _dataset():
    return {"qid": "Q1", "components": [{"name": "d.parquet", "url": "https://doip.example/doip/retrieve/Q1/d.parquet"}]}


def test_load_truth_filters_window_and_series(monkeypatch):
    source = pd.DataFrame({
        "datum": ["2021-12-29", "2022-01-05", "2022-01-12", "2022-01-12"],
        "value": [1.0, 10.0, 20.0, 99.0],
        "typ": ["a", "a", "a", "b"],
    })
    monkeypatch.setattr(loaders, "download_preview_bytes", lambda url, base, limit: _parquet_bytes(source))
    bench = Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": "a"})
    truth = loaders.load_truth(_dataset(), bench, "https://doip.example")
    assert truth["date"].tolist() == [pd.Timestamp("2022-01-05"), pd.Timestamp("2022-01-12")]
    assert truth["truth"].tolist() == [10.0, 20.0]


def test_load_truth_unknown_column_raises(monkeypatch):
    source = pd.DataFrame({"datum": ["2022-01-05"], "other": [1.0]})
    monkeypatch.setattr(loaders, "download_preview_bytes", lambda url, base, limit: _parquet_bytes(source))
    with pytest.raises(loaders.LoadError, match="value"):
        loaders.load_truth(_dataset(), BENCH, "https://doip.example")


def test_load_truth_empty_window_raises(monkeypatch):
    source = pd.DataFrame({"datum": ["2020-01-01"], "value": [1.0]})
    monkeypatch.setattr(loaders, "download_preview_bytes", lambda url, base, limit: _parquet_bytes(source))
    with pytest.raises(loaders.LoadError, match="no observations"):
        loaders.load_truth(_dataset(), BENCH, "https://doip.example")


def test_fetch_run_params_returns_empty_dict_on_failure(monkeypatch):
    def boom(url, base, limit):
        raise ValueError("nope")

    monkeypatch.setattr(loaders, "download_preview_bytes", boom)
    assert loaders.fetch_run_params("Q1", "https://doip.example") == {}


def test_fetch_run_params_parses_config(monkeypatch):
    monkeypatch.setattr(
        loaders, "download_preview_bytes", lambda url, base, limit: b'{"history_length": 91}'
    )
    assert loaders.fetch_run_params("Q1", "https://doip.example") == {"history_length": 91}


def test_load_predictions_duplicate_dates_keep_last():
    records = [
        {"datum": "2022-01-05", "value": "1"},
        {"datum": "2022-01-05", "value": "2"},
    ]
    frame = loaders.load_predictions(records, BENCH)
    assert len(frame) == 1
    assert frame["forecast"].tolist() == [2.0]


def _patch(monkeypatch, data):
    monkeypatch.setattr(loaders, "download_preview_bytes", lambda url, base, limit: data)


def test_load_truth_skips_non_numeric_and_bad_dates(monkeypatch):
    source = pd.DataFrame({
        "datum": ["2022-01-05", "2022-01-12", "2022-01-19", "garbage"],
        "value": ["1.5", "abc", "", "7"],
    })
    _patch(monkeypatch, _parquet_bytes(source))
    truth = loaders.load_truth(_dataset(), BENCH, "https://doip.example")
    assert truth["date"].tolist() == [pd.Timestamp("2022-01-05")]
    assert truth["truth"].tolist() == [1.5]


def test_load_truth_corrupt_parquet_raises_load_error(monkeypatch):
    _patch(monkeypatch, b"not a parquet file")
    with pytest.raises(loaders.LoadError):
        loaders.load_truth(_dataset(), BENCH, "https://doip.example")


def test_load_truth_duplicate_dates_raise(monkeypatch):
    source = pd.DataFrame({"datum": ["2022-01-05", "2022-01-05"], "value": [1.0, 2.0]})
    _patch(monkeypatch, _parquet_bytes(source))
    with pytest.raises(loaders.LoadError, match="several observations"):
        loaders.load_truth(_dataset(), BENCH, "https://doip.example")


def test_load_truth_filter_column_without_value_raises(monkeypatch):
    source = pd.DataFrame({"datum": ["2022-01-05"], "value": [1.0], "typ": ["a"]})
    _patch(monkeypatch, _parquet_bytes(source))
    bench = Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": None})
    with pytest.raises(loaders.LoadError, match="filter value"):
        loaders.load_truth(_dataset(), bench, "https://doip.example")
