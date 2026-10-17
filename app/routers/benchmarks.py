import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.auth import optional_user, require_right
from app.benchmarks import actions as bactions, store as bstore
from app.benchmarks.definition import DefinitionError
from app.leaderboard import actions as lactions
from app.leaderboard.loaders import LoadError
from app.leaderboard.scoring import NotScorable
from app.schemas import (
    BenchmarkCreate, BenchmarkEntryOut, BenchmarkLeaderboardOut, BenchmarkOut, BenchmarkPatch, CheckReportOut, DescriptionIn,
    EntryCreate, EntryCreated, RunSpecOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, (bactions.NotFound, lactions.NotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (bactions.Conflict, lactions.Conflict)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (LoadError, NotScorable, DefinitionError)):
        return HTTPException(status_code=422, detail=str(exc))
    logger.exception("benchmarks request failed")
    return HTTPException(status_code=502, detail="The benchmarks database or data storage is unavailable.")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _benchmark_out(row: dict) -> BenchmarkOut:
    return BenchmarkOut(
        id=row["id"], name=row["name"], state=bstore.state(row), description=row.get("description"),
        dataset_qid=row["dataset_qid"], target_column=row["target_column"],
        date_column=row["date_column"], filter_column=row.get("filter_column"),
        filter_value=row.get("filter_value"), test_start=row["test_start"].isoformat(),
        test_end=row["test_end"].isoformat(), history_start=_iso(row.get("history_start")),
        history_end=_iso(row.get("history_end")), input_filename=row.get("input_filename") or "input.parquet",
        prediction_length=row.get("prediction_length"), truth_points=row.get("truth_points"),
        data_commit=row.get("data_commit"), entry_count=row.get("entry_count"), created_at=str(row["created_at"]),
        finalized_at=str(row["finalized_at"]) if row.get("finalized_at") else None,
        retired_at=str(row["retired_at"]) if row.get("retired_at") else None,
    )


@router.get("", response_model=list[BenchmarkOut])
def list_benchmarks():
    """Final benchmarks with their entry counts (database only)."""
    try:
        return [_benchmark_out(row) for row in bactions.list_final()]
    except Exception as e:
        raise _http(e)


@router.get("/drafts", response_model=list[BenchmarkOut], dependencies=[Depends(require_right("benchmarks"))])
def list_drafts():
    """Draft benchmarks (logged-in users only)."""
    try:
        return [_benchmark_out(row) for row in bactions.list_drafts()]
    except Exception as e:
        raise _http(e)


@router.get("/{benchmark_id}", response_model=BenchmarkOut)
def get_benchmark(benchmark_id: int, user=Depends(optional_user)):
    """One benchmark; a draft is visible only to users with the benchmarks right."""
    try:
        return _benchmark_out(bactions.get(benchmark_id, include_draft=bool(user and user.can("benchmarks"))))
    except Exception as e:
        raise _http(e)


@router.get("/{benchmark_id}/run-spec", response_model=RunSpecOut)
def get_run_spec(benchmark_id: int):
    """Everything needed to run a model on a final benchmark."""
    try:
        return bactions.run_spec(benchmark_id)
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
        row = bactions.get(benchmark_id, include_draft=False)
        board = lactions.read_leaderboard(benchmark_id, rank_by, not all_runs)
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


@router.post("", response_model=BenchmarkOut, status_code=201, dependencies=[Depends(require_right("benchmarks"))])
def create_benchmark(request: BenchmarkCreate):
    """Create a draft benchmark."""
    try:
        benchmark_id = bactions.create_draft(
            name=request.name, description=request.description, dataset_qid=request.dataset_qid,
            target_column=request.target_column, date_column=request.date_column,
            test_start=request.test_start, test_end=request.test_end, filter_column=request.filter_column,
            filter_value=request.filter_value, history_start=request.history_start,
            history_end=request.history_end, input_filename=request.input_filename,
        )
        return _benchmark_out(bactions.get(benchmark_id, include_draft=True))
    except Exception as e:
        raise _http(e)


@router.patch("/{benchmark_id}", response_model=BenchmarkOut, dependencies=[Depends(require_right("benchmarks"))])
def update_benchmark(benchmark_id: int, request: BenchmarkPatch):
    """Change fields of a draft (only the fields sent; null clears an optional field)."""
    try:
        bactions.update_draft(benchmark_id, **request.model_dump(exclude_unset=True))
        return _benchmark_out(bactions.get(benchmark_id, include_draft=True))
    except Exception as e:
        raise _http(e)


@router.put("/{benchmark_id}/description", response_model=BenchmarkOut, dependencies=[Depends(require_right("admin"))])
def set_description(benchmark_id: int, request: DescriptionIn):
    """Change the description of a benchmark in any state (admins only; empty clears it)."""
    try:
        bactions.set_description(benchmark_id, request.description)
        return _benchmark_out(bactions.get(benchmark_id, include_draft=True))
    except Exception as e:
        raise _http(e)


@router.post("/{benchmark_id}/check", response_model=CheckReportOut, dependencies=[Depends(require_right("benchmarks"))])
def check_benchmark(benchmark_id: int):
    """Check a draft against the current data without changing it."""
    try:
        report = bactions.check(benchmark_id)
    except Exception as e:
        raise _http(e)
    return CheckReportOut(
        ok=report.ok, problems=report.problems, warnings=report.warnings, data_commit=report.data_commit,
        component_name=report.component_name, truth_points=report.truth_points,
        history_end=_iso(report.history_end), last_history_date=_iso(report.last_history_date),
        prediction_length=report.prediction_length, frequency_days=report.frequency_days,
        input_sql=report.input_sql,
    )


@router.post("/{benchmark_id}/finalize", response_model=BenchmarkOut, dependencies=[Depends(require_right("benchmarks"))])
def finalize_benchmark(benchmark_id: int):
    """Pin a draft to the current data and make it final."""
    try:
        bactions.finalize(benchmark_id)
        return _benchmark_out(bactions.get(benchmark_id, include_draft=True))
    except Exception as e:
        raise _http(e)


@router.post("/{benchmark_id}/copy", response_model=BenchmarkOut, status_code=201,
             dependencies=[Depends(require_right("benchmarks"))])
def copy_benchmark(benchmark_id: int):
    """Copy a benchmark as a new draft."""
    try:
        return _benchmark_out(bactions.get(bactions.copy_as_draft(benchmark_id), include_draft=True))
    except Exception as e:
        raise _http(e)


@router.post("/{benchmark_id}/entries", response_model=EntryCreated, status_code=201,
             dependencies=[Depends(require_right("benchmarks"))])
def add_entry(benchmark_id: int, request: EntryCreate):
    """Score a model run against the benchmark and store the result."""
    try:
        return EntryCreated(entry_id=lactions.add_run(benchmark_id, request.run_qid))
    except Exception as e:
        raise _http(e)


@router.delete("/{benchmark_id}/entries/{entry_id}", status_code=204, dependencies=[Depends(require_right("benchmarks"))])
def remove_entry(benchmark_id: int, entry_id: int):
    """Remove an entry (soft); add the run again to recompute it."""
    try:
        lactions.remove_entry(benchmark_id, entry_id)
    except Exception as e:
        raise _http(e)
    return Response(status_code=204)


@router.post("/{benchmark_id}/retire", status_code=204, dependencies=[Depends(require_right("benchmarks"))])
def retire_benchmark(benchmark_id: int):
    """Hide a benchmark from the list."""
    try:
        bactions.retire(benchmark_id)
    except Exception as e:
        raise _http(e)
    return Response(status_code=204)
