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


def _truth(data: bytes, bench: Benchmark) -> pd.DataFrame:
    return loaders.read_series(data, bench, bench.test_start, bench.test_end, require_unique=True, empty_message="empty")


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame), buffer)
    return buffer.getvalue()


def test_fetch_run_params_parses_config(monkeypatch):
    monkeypatch.setattr(
        loaders, "download_preview_bytes", lambda url, base, limit: b'{"history_length": 91}'
    )
    assert loaders.fetch_run_params("Q1", "https://doip.example") == {"history_length": 91}


def test_read_series_duplicate_dates_raise():
    source = pd.DataFrame({"datum": ["2022-01-05", "2022-01-05"], "value": [1.0, 2.0]})
    with pytest.raises(loaders.LoadError, match="several observations"):
        _truth(_parquet_bytes(source), BENCH)


def test_read_series_drops_non_finite_values():
    source = pd.DataFrame({
        "datum": ["2022-01-05", "2022-01-12", "2022-01-19"],
        "value": [float("nan"), float("inf"), 2.5],
    })
    truth = _truth(_parquet_bytes(source), BENCH)
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


def test_read_series_reads_window_and_series():
    source = pd.DataFrame({
        "datum": ["2021-12-29", "2022-01-05", "2022-01-12", "2022-01-12"],
        "value": [1.0, 10.0, 20.0, 99.0],
        "typ": ["a", "a", "a", "b"],
    })
    bench = Benchmark(**{**BENCH.__dict__, "filters": (("typ", "a"),)})
    truth = _truth(_parquet_bytes(source), bench)
    assert truth["truth"].tolist() == [10.0, 20.0]


def test_read_series_rejects_garbage_bytes():
    with pytest.raises(loaders.LoadError):
        _truth(b"not a parquet file", BENCH)


def test_read_series_with_two_filters_reads_exactly_one_series():
    source = pd.DataFrame({
        "datum": ["2022-01-05", "2022-01-05", "2022-01-05", "2022-01-12"],
        "typ": ["a", "a", "b", "a"],
        "age": ["young", "old", "young", "young"],
        "value": [1.0, 2.0, 3.0, 4.0],
    })
    bench = Benchmark(**{**BENCH.__dict__, "filters": (("typ", "a"), ("age", "young"))})
    truth = _truth(_parquet_bytes(source), bench)
    assert truth["truth"].tolist() == [1.0, 4.0]                    # one row per date: no duplicates
    with pytest.raises(loaders.LoadError, match="several observations"):
        _truth(_parquet_bytes(source), Benchmark(**{**BENCH.__dict__, "filters": (("typ", "a"),)}))


def test_truth_from_reference_reads_the_file_at_the_commit_and_checks_its_hash(monkeypatch):
    import hashlib
    source = pd.DataFrame({"datum": [date(2022, 1, 5), date(2022, 1, 12)], "value": [10.0, float("nan")]})
    data = _parquet_bytes(source)
    manifest = {"commit": "ee" * 32, "reference": {"path": "3/reference.parquet", "sha256": hashlib.sha256(data).hexdigest()}}
    calls = []
    monkeypatch.setattr(loaders.lakefs_client, "read_benchmark_file",
                        lambda commit, path, limit: calls.append((commit, path)) or data)
    truth = loaders.truth_from_reference(manifest, BENCH)
    assert calls == [("ee" * 32, "3/reference.parquet")]
    assert truth["date"].tolist() == [pd.Timestamp("2022-01-05")] and truth["truth"].tolist() == [10.0]   # NaN dropped
    with pytest.raises(loaders.LoadError, match="hash"):
        loaders.truth_from_reference({**manifest, "reference": {**manifest["reference"], "sha256": "0" * 64}}, BENCH)

