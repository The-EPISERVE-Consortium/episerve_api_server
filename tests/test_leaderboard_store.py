"""Tests for the benchmark store against a fake connection (no database)."""

import json
from datetime import date

import pymysql.err
import pytest

from app.config import settings
from app.leaderboard import store
from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry, Leaderboard
from tests.fakes import FakeConn

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 12, 31), "typ", "a")


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


def test_dataclass_extensions_have_defaults():
    entry = Entry("Q1", "m", "t", {}, None, date(2021, 12, 31), "", 1, 1, {}, [])
    assert (entry.entry_id, entry.truth_commit, entry.truth_as_of, entry.scored_at) == (None, "", "", "")
    board = Leaderboard(BENCH, "mae", 1, "", [])
    assert (board.name, board.benchmark_id) == ("", 0)


def test_connect_uses_benchmark_settings(monkeypatch):
    seen = {}
    monkeypatch.setattr(store.pymysql, "connect", lambda **kw: seen.update(kw) or object())
    monkeypatch.setattr(settings, "benchmarks_db", "benchmarks")
    monkeypatch.setattr(settings, "benchmarks_user", "bb")
    monkeypatch.setattr(settings, "benchmarks_password", "pw")
    store._connect()
    assert (seen["database"], seen["user"], seen["password"]) == ("benchmarks", "bb", "pw")
    assert seen["charset"] == "utf8mb4"


def test_add_entry_checks_for_active_duplicate_then_inserts(monkeypatch):
    conn = _use(monkeypatch, ([], 0, None), ([], 1, 42))
    assert store.add_entry(3, _entry()) == 42
    select, insert = conn.executed
    assert "FOR UPDATE" in select[0] and "removed_at IS NULL" in select[0] and select[1] == (3, "Q10")
    assert insert[0].startswith("INSERT INTO entries")
    params = insert[1]
    assert json.loads(params[5]) == {"history_length": 91}           # params column
    assert json.loads(params[14])[0]["truth"] == 10.0                # points column
    assert params[13] is None                                        # interval_score NULL, never NaN
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


def test_add_entry_retries_after_a_lock_wait_timeout(monkeypatch):
    conns = _use_attempts(monkeypatch, (([], 0, None), _deadlock(1205)), (([], 0, None), ([], 1, 44)))
    assert store.add_entry(3, _entry()) == 44
    assert all(c.closed for c in conns)


def test_add_entry_deadlock_then_committed_duplicate_is_a_duplicate(monkeypatch):
    first, second = _use_attempts(
        monkeypatch, (([], 0, None), _deadlock()), (([{"id": 7}], 1, None),))
    with pytest.raises(store.DuplicateEntry):
        store.add_entry(3, _entry())
    assert first.closed and second.closed


def test_add_entry_second_deadlock_propagates(monkeypatch):
    conns = _use_attempts(monkeypatch, (([], 0, None), _deadlock()), (([], 0, None), _deadlock()))
    with pytest.raises(pymysql.err.OperationalError):
        store.add_entry(3, _entry())
    assert all(c.closed for c in conns)


def test_add_entry_does_not_retry_other_operational_errors(monkeypatch):
    err = pymysql.err.OperationalError(2003, "Can't connect")
    conns = _use_attempts(monkeypatch, (([], 0, None), err), (([], 0, None), ([], 1, 45)))
    with pytest.raises(pymysql.err.OperationalError):
        store.add_entry(3, _entry())
    assert conns[0].closed and len(conns[1].executed) == 0


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
