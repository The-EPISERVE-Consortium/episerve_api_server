"""Helpers that start the run workflow from a final benchmark (no UI, no I/O)."""

from app.benchmarks.definition import filters_from_row
from app.leaderboard.entries import has_equality_filter, parse_history_window


def apply_benchmark(state: dict, spec: dict, dataset_row: dict, benchmark_row: dict) -> dict:
    """Prefill the workflow state from a benchmark's run spec.

    Args:
        state: The workflow state (mutated and returned).
        spec: Result of ``benchmarks.actions.run_spec``.
        dataset_row: The benchmark dataset as listed by ``ckan_client.list_processed_datasets``.
        benchmark_row: The benchmark row (name, date and filter columns).
    """
    url, filename = spec["input_data_files"][0]
    qid = spec["dataset_qid"]
    state["datasets"] = [{**dataset_row, "data_path": url}]
    state["sql"] = {qid: spec["data_transformation_sql"][0]}
    state["filenames"] = {qid: filename}
    state["config"] = None
    state["benchmark"] = {
        "id": spec["benchmark_id"], "name": benchmark_row["name"], "dataset_qid": qid,
        "data_commit": spec["data_commit"], "prediction_length": spec["config"]["prediction_length"],
        "test_start": spec["test_start"], "test_end": spec["test_end"],
        "date_column": benchmark_row["date_column"],
        "filters": [list(pair) for pair in filters_from_row(benchmark_row)],
    }
    return state


def clear_benchmark(state: dict) -> None:
    """Forget the benchmark (the user chose datasets by hand)."""
    state["benchmark"] = None


def overlay_config(config: dict, benchmark: dict | None, declared: set[str]) -> dict:
    """Set the horizon keys the model declares; leave everything else as it is."""
    if not benchmark:
        return config
    merged = dict(config)
    if "prediction_length" in declared:
        merged["prediction_length"] = benchmark["prediction_length"]
    if "prediction_offset" in declared:
        merged["prediction_offset"] = 0
    return merged


def workflow_problems(
    benchmark: dict | None, data_path: str, sql: str, date_column: str,
    filters: list,
) -> list[str]:
    """Why a run built from the current workflow input would not qualify for the benchmark."""
    if not benchmark:
        return []
    problems = []
    if f"?version={benchmark['data_commit']}" not in (data_path or ""):
        problems.append(f"the input is not read at the benchmark's data commit {benchmark['data_commit'][:12]}")
    window = parse_history_window(sql or "", date_column)
    if window is None:
        problems.append("the history window cannot be read from the SQL")
    elif window.end.isoformat() >= benchmark["test_start"]:
        problems.append(f"the history ends {window.end}, on or after the test start {benchmark['test_start']}")
    for column, value in filters or []:
        if not has_equality_filter(sql or "", column, value or ""):
            problems.append(f"the filter {column} = {value!r} is missing from the SQL")
    return problems
