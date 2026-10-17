"""Tests for the benchmark store against a fake connection (no database)."""

import json
from datetime import date

import pymysql.err
import pytest

from app.leaderboard import store
from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry
from tests.fakes import FakeConn


def _use(monkeypatch, *results):
    conn = FakeConn(results)
    monkeypatch.setattr(store, "_connect", lambda: conn)
    return conn


def _use_attempts(monkeypatch, *attempts):
    """One FakeConn per _connect() call, each with its own scripted results."""
    conns = [FakeConn(a) for a in attempts]
    pending = list(conns)
    monkeypatch.setattr(store, "_connect", lambda: pending.pop(0))
    return conns


def _entry(**overrides):
    base = dict(
        run_qid="Q10", model_name="timesfm", docker_tag="latest", params={"history_length": 91},
        history_start=date(2021, 1, 1), history_end=date(2021, 12, 31), run_timestamp="2026-01-01T00:00:00Z",
        n_scored=2, n_expected=2, metrics={"mae": 1.0, "rmse": 1.0, "coverage": 1.0, "interval_score": None},
        points=[{"date": "2022-01-05", "forecast": 11.0, "q10": 9.0, "q90": 13.0, "truth": 10.0}],
        truth_commit="0123abcd" * 8, truth_as_of="2026-10-14T00:00:00Z",
    )
    return Entry(**{**base, **overrides})


def test_add_entry_checks_for_active_duplicate_then_inserts(monkeypatch):
    conn = _use(monkeypatch, ([], 0, None), ([], 1, 42))
    assert store.add_entry(3, _entry()) == 42
    select, insert = conn.executed
    assert "FOR UPDATE" in select[0] and "removed_at IS NULL" in select[0] and select[1] == (3, "Q10")
    assert insert[0].startswith("INSERT INTO entries")
    params = insert[1]
    assert json.loads(params[5]) == {"history_length": 91}           # params column
    assert params[6] is None                                         # used_columns not recorded
    assert json.loads(params[15])[0]["truth"] == 10.0                # points column
    assert params[14] is None                                        # interval_score NULL, never NaN
    assert conn.commits == 1 and conn.closed


def test_add_entry_rejects_an_active_duplicate_without_inserting(monkeypatch):
    conn = _use(monkeypatch, ([{"id": 7}], 1, None))
    with pytest.raises(store.DuplicateEntry):
        store.add_entry(3, _entry())
    assert len(conn.executed) == 1 and conn.commits == 0 and conn.rolled and conn.closed


def _deadlock(code=1213):
    return pymysql.err.OperationalError(code, "Deadlock found when trying to get lock")


def test_add_entry_retries_once_after_a_deadlock(monkeypatch):
    first, second = _use_attempts(
        monkeypatch, (([], 0, None), _deadlock()), (([], 0, None), ([], 1, 43)))
    assert store.add_entry(3, _entry()) == 43
    assert first.closed and second.closed and first.rolled and second.commits == 1


def test_list_entries_maps_rows_back_to_entries(monkeypatch):
    row = {
        "id": 9, "run_qid": "Q10", "model_name": "timesfm", "docker_tag": "latest", "run_timestamp": "t",
        "params": '{"history_length": 91}', "history_start": None, "history_end": date(2021, 12, 31),
        "n_scored": 2, "n_expected": 2, "mae": 1.0, "rmse": 1.5, "coverage": None, "interval_score": 3.0,
        "points": '[{"date": "2022-01-05", "truth": 10.0}]', "truth_as_of": "2026-10-14",
        "truth_commit": "0123abcd" * 8, "scored_at": "2026-10-16 12:00:00",
    }
    conn = _use(monkeypatch, ([row], 1, None))
    (entry,) = store.list_entries(3)
    assert "removed_at IS NULL" in conn.executed[0][0]
    assert entry.entry_id == 9 and entry.params == {"history_length": 91}
    assert entry.metrics == {"mae": 1.0, "rmse": 1.5, "coverage": None, "interval_score": 3.0}
    assert entry.points == [{"date": "2022-01-05", "truth": 10.0}]
    assert entry.truth_commit == "0123abcd" * 8 and entry.scored_at == "2026-10-16 12:00:00"


def test_remove_entry_is_a_soft_delete(monkeypatch):
    conn = _use(monkeypatch, ([], 1, None), ([], 0, None))
    assert store.remove_entry(3, 9) is True
    assert store.remove_entry(3, 9) is False
    sql, params = conn.executed[0]
    assert sql.startswith("UPDATE entries SET removed_at") and params == (9, 3)


def test_no_function_ever_issues_a_delete(monkeypatch):
    conn = _use(monkeypatch)
    store.add_entry(1, _entry())
    store.list_entries(1)
    store.remove_entry(1, 1)
    assert conn.executed and all("DELETE" not in sql.upper() for sql, _ in conn.executed)


def test_used_columns_are_stored_as_json_and_read_back(monkeypatch):
    entry = _entry()
    entry.used_columns = ["Meldedatum", "Inzidenz_7-Tage"]
    conn = _use(monkeypatch, ([], 0, None), ([], 1, 43))
    store.add_entry(3, entry)
    assert json.loads(conn.executed[1][1][6]) == ["Meldedatum", "Inzidenz_7-Tage"]
    row = {"run_qid": "Q1", "model_name": "m", "docker_tag": "t", "params": "{}", "used_columns": '["a", "b"]',
           "history_start": None, "history_end": None, "run_timestamp": "", "n_scored": 1, "n_expected": 1,
           "mae": 1, "rmse": 1, "coverage": None, "interval_score": None, "points": "[]", "id": 1,
           "truth_commit": "", "truth_as_of": "", "scored_at": ""}
    assert store._entry_from_row(row).used_columns == ["a", "b"]
    assert store._entry_from_row({**row, "used_columns": None}).used_columns is None
