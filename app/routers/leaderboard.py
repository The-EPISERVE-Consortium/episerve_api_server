from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from app.clients import ckan as ckan_client
from app.leaderboard.entries import Benchmark
from app.leaderboard.loaders import LoadError
from app.leaderboard.service import build_leaderboard, default_loaders
from app.schemas import LeaderboardEntry, LeaderboardResponse, SkippedRunOut

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


@router.get("", response_model=LeaderboardResponse)
def get_leaderboard(
    dataset_qid: str,
    target: str,
    date_column: str,
    test_start: date,
    test_end: date,
    filter_column: str | None = None,
    filter_value: str | None = None,
    rank_by: Literal["mae", "rmse", "coverage", "interval_score"] = "interval_score",
    all_runs: bool = Query(False, description="Show every run instead of the best run per model"),
):
    """Rank model runs by how well they predicted ``target`` over the test window."""
    if test_start > test_end:
        raise HTTPException(status_code=400, detail="test_start must not be after test_end")
    benchmark = Benchmark(
        dataset_qid=dataset_qid, target_column=target, date_column=date_column,
        test_start=test_start, test_end=test_end,
        filter_column=filter_column or None, filter_value=filter_value if filter_column else None,
    )
    try:
        dataset = next((d for d in ckan_client.list_processed_datasets() if d["qid"] == dataset_qid), None)
        runs = ckan_client.list_model_runs() if dataset else []
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CKAN error: {e}")
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_qid}' not found")
    try:
        board = build_leaderboard(
            benchmark, runs, dataset, rank_by=rank_by, best_per_model=not all_runs, **default_loaders(),
        )
    except LoadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Leaderboard error: {e}")
    entries = [
        LeaderboardEntry(
            rank=i, run_qid=e.run_qid, model_name=e.model_name, docker_tag=e.docker_tag, params=e.params,
            history_start=e.history_start.isoformat() if e.history_start else None,
            history_end=e.history_end.isoformat(), run_timestamp=e.run_timestamp,
            n_scored=e.n_scored, n_expected=e.n_expected, metrics=e.metrics, points=e.points,
        )
        for i, e in enumerate(board.entries, start=1)
    ]
    return LeaderboardResponse(
        dataset_qid=dataset_qid, target=target, date_column=date_column,
        test_start=test_start.isoformat(), test_end=test_end.isoformat(),
        filter_column=benchmark.filter_column, filter_value=benchmark.filter_value,
        rank_by=rank_by, truth_points=board.truth_points, truth_as_of=board.truth_as_of,
        best=entries[0].model_name if entries else None, entries=entries,
        not_scorable=[SkippedRunOut(run_qid=s.run_qid, model_name=s.model_name, reason=s.reason) for s in board.not_scorable],
    )
