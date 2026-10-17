"""Score one model run against a benchmark."""

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import pandas as pd

from app.leaderboard.entries import Benchmark, qualify_run
from app.leaderboard.loaders import LoadError
from app.leaderboard.metrics import score_forecast
from app.leaderboard.run_inputs import RunInput
from app.leaderboard.service import Entry, finite_or_none, points_for


class NotScorable(Exception):
    """The run cannot be scored for this benchmark; the message says why."""


def score_run(
    benchmark: Benchmark,
    run: dict,
    truth: pd.DataFrame,
    *,
    truth_commit: str,
    truth_as_of: str,
    load_predictions: Callable[[dict, Benchmark], pd.DataFrame],
    load_params: Callable[[str], dict],
    inputs: list[RunInput] | None = None,
    manifest: dict | None = None,
) -> Entry:
    """Qualify and score a run, producing the entry to store.

    Args:
        benchmark: The prediction task.
        run: Catalog record from ``ckan_client.list_model_runs()``.
        truth: Observed series for the test window (columns ``date``, ``truth``).
        truth_commit: lakeFS commit id the truth was read at.
        truth_as_of: When the data was pinned (the benchmark's finalization time), for display.
        load_predictions: ``(run, benchmark) -> DataFrame``; may raise ``LoadError``.
        load_params: ``(run_qid) -> dict`` of run parameters.
        inputs: The run's provenance inputs; required for a benchmark pinned to a data commit.
        manifest: The generated files of a benchmark with covariates (then ``inputs`` is not used).

    Returns:
        The scored entry (not yet stored).

    Raises:
        NotScorable: With the reason, if the run does not qualify, its predictions
            cannot be read, or it does not forecast every observed date.
    """
    verdict = qualify_run(run, benchmark, inputs=inputs, manifest=manifest)
    if not verdict.relevant:
        raise NotScorable("the run did not read this benchmark's input file" if manifest
                          else "the run was not made from this benchmark's dataset")
    if verdict.reason:
        raise NotScorable(verdict.reason)
    try:
        forecast = load_predictions(run, benchmark)
    except LoadError as error:
        raise NotScorable(str(error)) from error
    result = score_forecast(forecast, truth)
    if result.n_scored < result.n_expected:
        raise NotScorable(
            f"forecast covers {result.n_scored} of {result.n_expected} dates in the test window"
        )
    return Entry(
        run_qid=run["qid"], model_name=run.get("model_name", ""), docker_tag=run.get("docker_tag", ""),
        params=load_params(run["qid"]), history_start=verdict.window.start, history_end=verdict.window.end,
        run_timestamp=run.get("run_timestamp", ""), n_scored=result.n_scored, n_expected=result.n_expected,
        metrics={name: finite_or_none(value) for name, value in result.metrics.items()},
        points=points_for(forecast, truth), truth_commit=truth_commit, truth_as_of=truth_as_of,
    )


def candidate_runs(
    benchmark: Benchmark, runs: list[dict], active_run_qids: set[str],
    load_inputs: Callable[[str], list[RunInput] | None] | None = None,
    load_params: Callable[[str], dict] | None = None,
    manifest: dict | None = None,
) -> list[dict]:
    """Runs on the benchmark's data, with the reason each cannot be added (None if it can).

    Each candidate also carries what is cheap to know before scoring: the image tag, the history window (when
    the provenance proves it) and, for runs that can be added and with ``load_params``, the run's parameters.

    Only the cheap checks run here (status, dataset, history window, filter); a run that
    passes them can still be rejected when added if its predictions are unusable. For a
    benchmark pinned to a data commit, ``load_inputs(run_qid)`` supplies the run's provenance
    inputs (for runs that are successful and not yet added; fetched in parallel); a failing loader counts as unreadable.
    """
    relevant = [(run, qualify_run(run, benchmark, manifest=manifest)) for run in runs]
    relevant = [(run, verdict) for run, verdict in relevant if verdict.relevant]
    pinned = benchmark.data_commit is not None and not manifest       # a generated input file needs no provenance fetch
    to_load = [
        run["qid"] for run, _ in relevant
        if pinned and load_inputs and run.get("status") == "success" and run["qid"] not in active_run_qids
    ]

    def safe_load(qid: str) -> list[RunInput] | None:
        try:
            return load_inputs(qid)
        except Exception:
            return None

    if to_load:
        with ThreadPoolExecutor(max_workers=8) as pool:
            loaded = dict(zip(to_load, pool.map(safe_load, to_load)))
    else:
        loaded = {}
    candidates = []
    for run, verdict in relevant:
        if run["qid"] in loaded:
            verdict = qualify_run(run, benchmark, inputs=loaded[run["qid"]], manifest=manifest)
        reason = "already on this leaderboard" if run["qid"] in active_run_qids else verdict.reason
        candidates.append({
            "qid": run["qid"], "model_name": run.get("model_name", ""),
            "run_timestamp": run.get("run_timestamp", ""), "reason": reason,
            "docker_tag": run.get("docker_tag", ""),
            "history_start": verdict.window.start if verdict.window else None,
            "history_end": verdict.window.end if verdict.window else None,
            "params": None,
        })
    addable = [c["qid"] for c in candidates if c["reason"] is None]
    if load_params and addable:
        def safe_params(qid: str) -> dict:
            try:
                return load_params(qid)
            except Exception:
                return {}

        with ThreadPoolExecutor(max_workers=8) as pool:
            params = dict(zip(addable, pool.map(safe_params, addable)))
        for c in candidates:
            if c["qid"] in params:
                c["params"] = params[c["qid"]]
    return sorted(candidates, key=lambda c: c["run_timestamp"], reverse=True)
