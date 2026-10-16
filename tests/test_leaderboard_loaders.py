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


def test_parse_truth_unknown_column_raises():
    source = pd.DataFrame({"datum": ["2022-01-05"], "other": [1.0]})
    with pytest.raises(loaders.LoadError, match="value"):
        loaders.parse_truth(_parquet_bytes(source), BENCH)


def test_parse_truth_empty_window_raises():
    source = pd.DataFrame({"datum": ["2020-01-01"], "value": [1.0]})
    with pytest.raises(loaders.LoadError, match="no observations"):
        loaders.parse_truth(_parquet_bytes(source), BENCH)


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


def test_parse_truth_skips_non_numeric_and_bad_dates():
    source = pd.DataFrame({
        "datum": ["2022-01-05", "2022-01-12", "2022-01-19", "garbage"],
        "value": ["1.5", "abc", "", "7"],
    })
    truth = loaders.parse_truth(_parquet_bytes(source), BENCH)
    assert truth["date"].tolist() == [pd.Timestamp("2022-01-05")]
    assert truth["truth"].tolist() == [1.5]


def test_parse_truth_duplicate_dates_raise():
    source = pd.DataFrame({"datum": ["2022-01-05", "2022-01-05"], "value": [1.0, 2.0]})
    with pytest.raises(loaders.LoadError, match="several observations"):
        loaders.parse_truth(_parquet_bytes(source), BENCH)


def test_parse_truth_filter_column_without_value_raises():
    source = pd.DataFrame({"datum": ["2022-01-05"], "value": [1.0], "typ": ["a"]})
    bench = Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": None})
    with pytest.raises(loaders.LoadError, match="filter value"):
        loaders.parse_truth(_parquet_bytes(source), bench)


def test_fetch_run_params_non_finite_constants_become_null(monkeypatch):
    monkeypatch.setattr(
        loaders, "download_preview_bytes",
        lambda url, base, limit: b'{"a": NaN, "b": Infinity, "c": -Infinity, "d": 1}',
    )
    assert loaders.fetch_run_params("Q1", "https://doip.example") == {
        "a": None, "b": None, "c": None, "d": 1,
    }


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


def test_truth_at_commit_reads_the_first_component_at_the_pinned_commit(monkeypatch):
    source = pd.DataFrame({"datum": ["2022-01-05"], "value": [10.0]})
    calls = []

    def fake_read(commit, qid, filename, max_bytes):
        calls.append((commit, qid, filename, max_bytes))
        return _parquet_bytes(source)

    monkeypatch.setattr(loaders.lakefs_client, "read_processed_component", fake_read)
    dataset = {"qid": "Q1", "components": [{"name": "d.parquet", "url": "https://doip.example/doip/retrieve/Q1/d.parquet"}]}
    truth = loaders.truth_at_commit(dataset, BENCH, "abc12345")
    assert truth["truth"].tolist() == [10.0]
    assert calls == [("abc12345", "Q1", "d.parquet", loaders.MAX_DOWNLOAD_BYTES)]


def test_truth_at_commit_wraps_value_errors_as_load_errors(monkeypatch):
    def too_big(commit, qid, filename, max_bytes):
        raise ValueError("The dataset file exceeds the size limit.")

    monkeypatch.setattr(loaders.lakefs_client, "read_processed_component", too_big)
    dataset = {"qid": "Q1", "components": [{"name": "d.parquet", "url": "u"}]}
    with pytest.raises(loaders.LoadError, match="size limit"):
        loaders.truth_at_commit(dataset, BENCH, "abc12345")
    with pytest.raises(loaders.LoadError, match="components"):
        loaders.truth_at_commit({"qid": "Q1", "components": []}, BENCH, "abc12345")


def test_truth_at_commit_unsafe_commit_is_a_load_error(monkeypatch):
    monkeypatch.setattr(loaders.lakefs_client, "_client", lambda: None)
    dataset = {"qid": "Q1", "components": [{"name": "d.parquet", "url": "u"}]}
    with pytest.raises(loaders.LoadError, match="Invalid"):
        loaders.truth_at_commit(dataset, BENCH, "../evil")


def test_truth_at_commit_keeps_load_errors_unchanged(monkeypatch):
    monkeypatch.setattr(loaders.lakefs_client, "read_processed_component", lambda *a: b"not a parquet file")
    dataset = {"qid": "Q1", "components": [{"name": "d.parquet", "url": "u"}]}
    with pytest.raises(loaders.LoadError, match="could not be read"):
        loaders.truth_at_commit(dataset, BENCH, "abc12345")


def test_last_observed_date_returns_the_latest_on_or_before():
    source = pd.DataFrame({"datum": ["2026-03-18", "2026-03-25", "2026-04-01"], "value": [1.0, 2.0, 3.0]})
    assert loaders.last_observed_date(_parquet_bytes(source), BENCH, date(2026, 3, 31)) == date(2026, 3, 25)


def test_last_observed_date_tolerates_duplicate_dates_and_applies_the_filter():
    source = pd.DataFrame({
        "datum": ["2026-03-25", "2026-03-25", "2026-03-18"], "value": [1.0, 2.0, 3.0], "typ": ["a", "b", "b"],
    })
    bench = Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": "b"})
    assert loaders.last_observed_date(_parquet_bytes(source), bench, date(2026, 3, 31)) == date(2026, 3, 25)
    bench_a = Benchmark(**{**BENCH.__dict__, "filter_column": "typ", "filter_value": "a"})
    with pytest.raises(loaders.LoadError):                      # filter 'a' only has the 2026-03-25 row
        loaders.last_observed_date(_parquet_bytes(source), bench_a, date(2026, 3, 20))


def test_last_observed_date_without_observations_raises():
    source = pd.DataFrame({"datum": ["2026-05-01"], "value": [1.0]})
    with pytest.raises(loaders.LoadError, match="no observations on or before"):
        loaders.last_observed_date(_parquet_bytes(source), BENCH, date(2026, 3, 31))


def test_last_observed_date_ignores_non_numeric_values():
    source = pd.DataFrame({"datum": ["2026-03-18", "2026-03-25"], "value": ["1.5", "abc"]})
    assert loaders.last_observed_date(_parquet_bytes(source), BENCH, date(2026, 3, 31)) == date(2026, 3, 18)
