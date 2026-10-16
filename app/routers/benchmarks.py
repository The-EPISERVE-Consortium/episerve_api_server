from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.auth import require_auth
from app.leaderboard import actions, store
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable
from app.schemas import (
    BenchmarkCreate, BenchmarkEntryOut, BenchmarkLeaderboardOut, BenchmarkOut, EntryCreate, EntryCreated,
)

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, actions.NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, actions.Conflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (LoadError, NotScorable)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=502, detail=f"Benchmarks error: {exc}")


def _benchmark_out(row: dict) -> BenchmarkOut:
    return BenchmarkOut(
        id=row["id"], name=row["name"], dataset_qid=row["dataset_qid"], target_column=row["target_column"],
        date_column=row["date_column"], filter_column=row.get("filter_column"),
        filter_value=row.get("filter_value"), test_start=row["test_start"].isoformat(),
        test_end=row["test_end"].isoformat(), truth_points=row["truth_points"],
        entry_count=row.get("entry_count"), created_at=str(row["created_at"]),
        retired_at=str(row["retired_at"]) if row.get("retired_at") else None,
    )


@router.get("", response_model=list[BenchmarkOut])
def list_benchmarks():
    """Active benchmarks with their entry counts (database only)."""
    try:
        return [_benchmark_out(row) for row in store.list_benchmarks()]
    except Exception as e:
        raise _http(e)


@router.get("/{benchmark_id}", response_model=BenchmarkOut)
def get_benchmark(benchmark_id: int):
    try:
        return _benchmark_out(actions.get_benchmark(benchmark_id))
    except Exception as e:
        raise _http(e)


@router.get("/{benchmark_id}/leaderboard", response_model=BenchmarkLeaderboardOut)
def get_leaderboard(
    benchmark_id: int,
    rank_by: Literal["mae", "rmse", "coverage", "interval_score"] = "interval_score",
    all_runs: bool = Query(False, description="Show every entry instead of the best entry per model"),
):
    """The stored, ranked leaderboard of a benchmark (database only)."""
    try:
        row = actions.get_benchmark(benchmark_id)
        board = actions.read_leaderboard(benchmark_id, rank_by, not all_runs)
    except Exception as e:
        raise _http(e)
    entries = [
        BenchmarkEntryOut(
            rank=i, entry_id=e.entry_id, run_qid=e.run_qid, model_name=e.model_name, docker_tag=e.docker_tag,
            params=e.params, history_start=e.history_start.isoformat() if e.history_start else None,
            history_end=e.history_end.isoformat(), run_timestamp=e.run_timestamp, n_scored=e.n_scored,
            n_expected=e.n_expected, metrics=e.metrics, points=e.points, truth_commit=e.truth_commit,
            truth_as_of=e.truth_as_of, scored_at=e.scored_at,
        )
        for i, e in enumerate(board.entries, start=1)
    ]
    return BenchmarkLeaderboardOut(
        benchmark=_benchmark_out(row), rank_by=rank_by,
        best=entries[0].model_name if entries else None, entries=entries,
    )


@router.post("", response_model=BenchmarkOut, status_code=201, dependencies=[Depends(require_auth)])
def create_benchmark(request: BenchmarkCreate):
    """Create a benchmark (prediction task); the truth window is checked against the current data."""
    try:
        benchmark_id = actions.create_benchmark(
            name=request.name, dataset_qid=request.dataset_qid, target_column=request.target_column,
            date_column=request.date_column, test_start=request.test_start, test_end=request.test_end,
            filter_column=request.filter_column, filter_value=request.filter_value,
        )
        return _benchmark_out(actions.get_benchmark(benchmark_id))
    except Exception as e:
        raise _http(e)


@router.post("/{benchmark_id}/entries", response_model=EntryCreated, status_code=201,
             dependencies=[Depends(require_auth)])
def add_entry(benchmark_id: int, request: EntryCreate):
    """Score a model run against the benchmark and store the result."""
    try:
        return EntryCreated(entry_id=actions.add_run(benchmark_id, request.run_qid))
    except Exception as e:
        raise _http(e)


@router.delete("/{benchmark_id}/entries/{entry_id}", status_code=204, dependencies=[Depends(require_auth)])
def remove_entry(benchmark_id: int, entry_id: int):
    """Remove an entry (soft); add the run again to recompute it."""
    try:
        actions.remove_entry(benchmark_id, entry_id)
    except Exception as e:
        raise _http(e)
    return Response(status_code=204)


@router.post("/{benchmark_id}/retire", status_code=204, dependencies=[Depends(require_auth)])
def retire_benchmark(benchmark_id: int):
    """Hide a benchmark from the list."""
    try:
        actions.retire_benchmark(benchmark_id)
    except Exception as e:
        raise _http(e)
    return Response(status_code=204)
