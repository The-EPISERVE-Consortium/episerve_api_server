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


def test_load_predictions_missing_target_column_raises():
    with pytest.raises(loaders.LoadError, match="value"):
        loaders.load_predictions([{"datum": "2022-01-05", "other": "1"}], BENCH)


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame), buffer)
    return buffer.getvalue()


def test_fetch_run_params_parses_config(monkeypatch):
    monkeypatch.setattr(
        loaders, "download_preview_bytes", lambda url, base, limit: b'{"history_length": 91}'
    )
    assert loaders.fetch_run_params("Q1", "https://doip.example") == {"history_length": 91}


def test_parse_truth_duplicate_dates_raise():
    source = pd.DataFrame({"datum": ["2022-01-05", "2022-01-05"], "value": [1.0, 2.0]})
    with pytest.raises(loaders.LoadError, match="several observations"):
        loaders.parse_truth(_parquet_bytes(source), BENCH)


def test_parse_truth_drops_non_finite_values():
    source = pd.DataFrame({
        "datum": ["2022-01-05", "2022-01-12", "2022-01-19"],
        "value": [float("nan"), float("inf"), 2.5],
    })
    truth = loaders.parse_truth(_parquet_bytes(source), BENCH)
    assert truth["date"].tolist() == [pd.Timestamp("2022-01-19")]
    assert truth["truth"].tolist() == [2.5]


def test_load_predictions_non_finite_values_become_nan():
    records = [
        {"datum": "2022-01-05", "value": "inf", "value_q10": "-inf", "value_q90": "Infinity"},
        {"datum": "2022-01-12", "value": "1", "value_q10": "0", "value_q90": "2"},
    ]
    frame = loaders.load_predictions(records, BENCH)
    assert frame.loc[0, ["forecast", "q10", "q90"]].isna().all()
    assert frame.loc[1, "forecast"] == 1.0


def test_parse_truth_reads_window_and_series():
    source = pd.DataFrame({
        "datum": ["2021-12-29", "2022-01-05", "2022-01-12", "2022-01-12"],
        "value": [1.0, 10.0, 20.0, 99.0],
        "typ": ["a", "a", "a", "b"],
    })
    bench = Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": "a"})
    truth = loaders.parse_truth(_parquet_bytes(source), bench)
    assert truth["truth"].tolist() == [10.0, 20.0]


def test_parse_truth_rejects_garbage_bytes():
    with pytest.raises(loaders.LoadError):
        loaders.parse_truth(b"not a parquet file", BENCH)


def test_truth_from_pin_reads_the_named_component_at_the_pinned_commit(monkeypatch):
    source = pd.DataFrame({"datum": ["2022-01-05"], "value": [10.0]})
    calls = []

    def fake_read(commit, qid, filename, max_bytes):
        calls.append((commit, qid, filename, max_bytes))
        return _parquet_bytes(source)

    monkeypatch.setattr(loaders.lakefs_client, "read_processed_component", fake_read)
    truth = loaders.truth_from_pin("Q1", "d.parquet", "abc12345", BENCH)
    assert truth["truth"].tolist() == [10.0]
    assert calls == [("abc12345", "Q1", "d.parquet", loaders.MAX_DOWNLOAD_BYTES)]


def test_truth_from_pin_unsafe_commit_is_a_load_error(monkeypatch):
    monkeypatch.setattr(loaders.lakefs_client, "_client", lambda: None)
    with pytest.raises(loaders.LoadError, match="Invalid"):
        loaders.truth_from_pin("Q1", "d.parquet", "../evil", BENCH)


def test_last_observed_date_returns_the_latest_on_or_before():
    source = pd.DataFrame({"datum": ["2026-03-18", "2026-03-25", "2026-04-01"], "value": [1.0, 2.0, 3.0]})
    assert loaders.last_observed_date(_parquet_bytes(source), BENCH, date(2026, 3, 31)) == date(2026, 3, 25)


def test_last_observed_date_without_observations_raises():
    source = pd.DataFrame({"datum": ["2026-05-01"], "value": [1.0]})
    with pytest.raises(loaders.LoadError, match="no observations on or before"):
        loaders.last_observed_date(_parquet_bytes(source), BENCH, date(2026, 3, 31))


def test_parse_truth_with_two_filters_reads_exactly_one_series():
    source = pd.DataFrame({
        "datum": ["2022-01-05", "2022-01-05", "2022-01-05", "2022-01-12"],
        "typ": ["a", "a", "b", "a"],
        "age": ["young", "old", "young", "young"],
        "value": [1.0, 2.0, 3.0, 4.0],
    })
    bench = Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": "a",
                         "extra_filters": (("age", "young"),)})
    truth = loaders.parse_truth(_parquet_bytes(source), bench)
    assert truth["truth"].tolist() == [1.0, 4.0]                    # one row per date: no duplicates
    with pytest.raises(loaders.LoadError, match="several observations"):
        loaders.parse_truth(_parquet_bytes(source), Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": "a"}))
