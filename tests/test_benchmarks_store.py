"""Tests for the benchmark-definition store against a fake connection (no database)."""

from datetime import date

import pytest

from app.benchmarks import store
from app.benchmarks.definition import Benchmark
from app.benchmarks.errors import NotDraft
from tests.fakes import FakeConn

BENCH = Benchmark("Q1", "v", "datum", date(2026, 4, 1), date(2026, 9, 30), "typ", "a",
                  history_start=date(2024, 1, 3), history_end=date(2026, 3, 25), input_filename="input.parquet")


def _use(monkeypatch, *results):
    conn = FakeConn(results)
    monkeypatch.setattr(store, "_connect", lambda: conn)
    return conn


def test_update_draft_reports_unknown_ids_and_refuses_final_or_retired(monkeypatch):
    _use(monkeypatch, ([], 0, None))
    assert store.update_draft(99, "x", None, BENCH) is False
    for state in ({"finalized_at": "2026-10-16 10:00:00", "retired_at": None},
                  {"finalized_at": None, "retired_at": "2026-10-16 10:00:00"}):
        conn = _use(monkeypatch, ([state], 1, None))
        with pytest.raises(NotDraft):
            store.update_draft(3, "x", None, BENCH)
        assert len(conn.executed) == 1 and conn.rolled and conn.commits == 0 and conn.closed


def test_finalize_sets_the_pin_once(monkeypatch):
    conn = _use(monkeypatch, ([{"finalized_at": None, "retired_at": None}], 1, None), ([], 1, None))
    assert store.finalize(3, data_commit="ab" * 32, component_name="d.parquet", truth_points=27,
                          prediction_length=27, history_end=date(2026, 3, 25)) is True
    update_sql, params = conn.executed[1]
    assert "finalized_at = NOW()" in update_sql and "data_commit" in update_sql
    assert params == ("ab" * 32, "d.parquet", 27, 27, date(2026, 3, 25), 3)


def test_finalize_refuses_a_final_benchmark_and_reports_unknown_ids(monkeypatch):
    conn = _use(monkeypatch, ([{"finalized_at": "2026-10-16 10:00:00", "retired_at": None}], 1, None))
    with pytest.raises(NotDraft):
        store.finalize(3, data_commit="c", component_name="d", truth_points=1, prediction_length=1,
                       history_end=date(2026, 3, 25))
    assert len(conn.executed) == 1
    _use(monkeypatch, ([], 0, None))
    assert store.finalize(9, data_commit="c", component_name="d", truth_points=1, prediction_length=1,
                          history_end=date(2026, 3, 25)) is False


def test_lists_and_get_use_state_filters_and_count_active_entries(monkeypatch):
    conn = _use(monkeypatch, ([{"id": 1}], 1, None), ([{"id": 2}], 1, None), ([], 0, None))
    assert store.list_final() == [{"id": 1}]
    assert store.list_drafts() == [{"id": 2}]
    assert store.get(9) is None
    final_sql, draft_sql, get_sql = (c[0] for c in conn.executed)
    assert "finalized_at IS NOT NULL" in final_sql and "retired_at IS NULL" in final_sql
    assert "finalized_at IS NULL" in draft_sql and "retired_at IS NULL" in draft_sql
    assert "removed_at IS NULL" in final_sql and "WHERE b.id = %s" in get_sql


def test_state_and_benchmark_from_row():
    row = {"dataset_qid": "Q1", "target_column": "v", "date_column": "datum", "filter_column": None,
           "filter_value": None, "test_start": date(2026, 4, 1), "test_end": date(2026, 9, 30),
           "history_start": None, "history_end": date(2026, 3, 25), "input_filename": "input.parquet",
           "prediction_length": 27, "data_commit": "c" * 40, "finalized_at": "t", "retired_at": None}
    b = store.benchmark_from_row(row)
    assert (b.history_end, b.prediction_length, b.data_commit) == (date(2026, 3, 25), 27, "c" * 40)
    assert store.state({"finalized_at": None, "retired_at": None}) == "draft"
    assert store.state({"finalized_at": "t", "retired_at": None}) == "final"
    assert store.state({"finalized_at": "t", "retired_at": "t"}) == "retired"
    assert store.state({"finalized_at": None, "retired_at": "t"}) == "retired"


def test_no_function_ever_issues_a_delete(monkeypatch):
    conn = _use(monkeypatch)
    store.create_draft("n", None, BENCH)
    store.list_final()
    store.list_drafts()
    store.get(1)
    store.retire(1)
    assert conn.executed and all("DELETE" not in sql.upper() for sql, _ in conn.executed)


def test_is_public_means_finalized_whatever_retired_at_says():
    assert store.is_public({"finalized_at": "t", "retired_at": None})
    assert store.is_public({"finalized_at": "t", "retired_at": "t"})
    assert not store.is_public({"finalized_at": None, "retired_at": None})
    assert not store.is_public({"finalized_at": None, "retired_at": "t"})
