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
    manifest = {"commit": "ee" * 32, "reference": {"qid": "Q1791000000002", "component": "reference.parquet", "sha256": hashlib.sha256(data).hexdigest()}}
    calls = []
    monkeypatch.setattr(loaders.lakefs_client, "read_benchmark_component",
                        lambda commit, qid, name, limit: calls.append((commit, qid, name)) or data)
    truth = loaders.truth_from_reference(manifest, BENCH)
    assert calls == [("ee" * 32, "Q1791000000002", "reference.parquet")]
    assert truth["date"].tolist() == [pd.Timestamp("2022-01-05")] and truth["truth"].tolist() == [10.0]   # NaN dropped
    with pytest.raises(loaders.LoadError, match="hash"):
        loaders.truth_from_reference({**manifest, "reference": {**manifest["reference"], "sha256": "0" * 64}}, BENCH)



def test_input_history_checks_pin_and_keeps_missing_target_gaps(monkeypatch):
    """Read only frozen input dates in the run window without filtering it again.

    Args:
        monkeypatch: Fixture replacing lakeFS with a frozen Parquet file.
    """
    import hashlib
    data = _parquet_bytes(pd.DataFrame({
        "datum": ["2021-11-30", "2021-12-01", "2021-12-15", "2021-12-31", "2022-01-01"],
        "value": [5.0, 10.0, float("nan"), 20.0, 99.0],
    }))
    info = {"qid": "Q123", "component": "input.parquet", "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {"commit": "ab" * 32, "input": info}
    calls = []
    monkeypatch.setattr(loaders.lakefs_client, "read_benchmark_component",
                        lambda *args: calls.append(args) or data)
    result = loaders.input_history(manifest, BENCH, date(2021, 12, 1), date(2021, 12, 31))
    assert calls[0][:3] == ("ab" * 32, "Q123", "input.parquet")
    assert result == [{"date": "2021-12-01", "value": 10.0}, {"date": "2021-12-15", "value": None},
                      {"date": "2021-12-31", "value": 20.0}]
    with pytest.raises(loaders.LoadError, match="hash"):
        loaders.input_history({**manifest, "input": {**info, "sha256": "0" * 64}}, BENCH, None, date(2021, 12, 31))


@pytest.mark.parametrize("dates", [["invalid"], ["2021-12-01", "2021-12-01"], ["2022-01-05"]])
def test_input_history_rejects_invalid_dates_duplicates_or_no_history(monkeypatch, dates):
    """Refuse ambiguous dates and reference-only data rather than plotting history.

    Args:
        monkeypatch: Fixture replacing lakeFS reads.
        dates: Invalid, duplicate, or out-of-window date values.
    """
    import hashlib
    data = _parquet_bytes(pd.DataFrame({"datum": dates, "value": [1.0] * len(dates)}))
    monkeypatch.setattr(loaders.lakefs_client, "read_benchmark_component", lambda *args: data)
    manifest = {"commit": "ab" * 32, "input": {"qid": "Q123", "component": "input.parquet", "sha256": hashlib.sha256(data).hexdigest()}}
    with pytest.raises(loaders.LoadError):
        loaders.input_history(manifest, BENCH, None, date(2021, 12, 31))


def test_legacy_input_history_replays_the_saved_transformation_at_the_source_pin(monkeypatch):
    """Recover the original transformed input rather than a newer benchmark file.

    Args:
        monkeypatch: Fixture supplying a saved configuration and pinned source.
    """
    import json
    from app.leaderboard.service import Entry
    commit = "ab" * 32
    source = _parquet_bytes(pd.DataFrame({
        "datum": ["2021-12-01", "2021-12-15", "2021-12-15", "2022-01-05"],
        "value": [10.0, 20.0, 99.0, 30.0], "typ": ["a", "a", "b", "a"],
    }))
    config = {"input_data_files": [[f"https://doip.example/doip/retrieve/Q1/data.parquet?version={commit}", "input.parquet"]],
              "data_transformation_sql": ["SELECT datum, value * 2 AS value FROM df WHERE typ = 'a'"]}
    monkeypatch.setattr(loaders, "download_preview_bytes", lambda *args: json.dumps(config).encode())
    calls = []
    monkeypatch.setattr(loaders.lakefs_client, "read_processed_component", lambda *args: calls.append(args) or source)
    entry = Entry("Q10", "model", "latest", {}, None, date(2021, 12, 31), "", 1, 1, {}, truth_commit=commit)
    assert loaders.legacy_input_history(entry, BENCH, "data.parquet", "https://doip.example") == [
        {"date": "2021-12-01", "value": 20.0}, {"date": "2021-12-15", "value": 40.0},
    ]
    assert calls[0][:3] == (commit, "Q1", "data.parquet")


@pytest.mark.parametrize("change", ["version", "origin", "dataset", "missing-config", "write-sql", "external-sql"])
def test_legacy_input_history_rejects_unverifiable_or_unsafe_inputs(monkeypatch, change):
    """Refuse another source pin and block SQL writes or external reads.

    Args:
        monkeypatch: Fixture replacing saved configuration and lakeFS reads.
        change: Configuration defect or unsupported query to exercise.
    """
    import json
    from app.leaderboard.service import Entry
    commit = "ab" * 32
    url = f"https://doip.example/doip/retrieve/Q1/data.parquet?version={commit}"
    config = {"input_data_files": [[url, "input.parquet"]], "data_transformation_sql": ["SELECT * FROM df"]}
    if change == "version":
        config["input_data_files"][0][0] = url.replace(commit, "cd" * 32)
    elif change == "origin":
        config["input_data_files"][0][0] = url.replace("doip.example", "elsewhere.example")
    elif change == "dataset":
        config["input_data_files"][0][0] = url.replace("/Q1/", "/Q2/")
    elif change == "missing-config":
        config = {}
    elif change == "write-sql":
        config["data_transformation_sql"] = ["DELETE FROM df"]
    else:
        config["data_transformation_sql"] = ["SELECT * FROM read_csv('/etc/passwd')"]
    monkeypatch.setattr(loaders, "download_preview_bytes", lambda *args: json.dumps(config).encode())
    data = _parquet_bytes(pd.DataFrame({"datum": ["2021-12-01"], "value": [10.0]}))
    monkeypatch.setattr(loaders.lakefs_client, "read_processed_component", lambda *args: data)
    entry = Entry("Q10", "model", "latest", {}, None, date(2021, 12, 31), "", 1, 1, {}, truth_commit=commit)
    with pytest.raises(loaders.LoadError):
        loaders.legacy_input_history(entry, BENCH, "data.parquet", "https://doip.example")
