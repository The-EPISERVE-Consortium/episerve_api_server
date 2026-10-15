"""Format a leaderboard for display."""

from app.leaderboard.metrics import METRICS
from app.leaderboard.service import Leaderboard, sort_key

LABELS = {"mae": "MAE", "rmse": "RMSE", "coverage": "80% coverage", "interval_score": "interval score"}


def _fmt(metric: str, value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f}%" if metric == "coverage" else f"{value:.2f}"


def table_rows(board: Leaderboard) -> list[dict]:
    """One display row per entry, with the best value of each column marked."""
    best_runs = {}
    for metric in METRICS:
        scored = [e for e in board.entries if e.metrics.get(metric) is not None]
        best_runs[metric] = min(scored, key=sort_key(metric)).run_qid if scored else None
    rows = []
    for rank, entry in enumerate(board.entries, start=1):
        start = entry.history_start.isoformat() if entry.history_start else "…"
        rows.append({
            "rank": rank,
            "model": f"{entry.model_name}",
            "run_qid": entry.run_qid,
            "params": ", ".join(f"{k}={v}" for k, v in entry.params.items()) or "—",
            "history": f"{start} → {entry.history_end.isoformat()}",
            "scored": f"{entry.n_scored}/{entry.n_expected}",
            **{m: _fmt(m, entry.metrics.get(m)) for m in METRICS},
            "best": {m: best_runs[m] == entry.run_qid for m in METRICS},
        })
    return rows


def summary(board: Leaderboard) -> str:
    """The sentence stating the setup and the best entry."""
    b = board.benchmark
    setup = (
        f"Target {b.target_column}, test window {b.test_start} → {b.test_end}, "
        f"{board.truth_points} observations (data as of {board.truth_as_of or 'unknown'})."
    )
    if not board.entries:
        return f"{setup} No scorable runs for this task."
    best = board.entries[0]
    return f"{setup} Best entry: {best.model_name}, ranked by {LABELS[board.rank_by]}."
