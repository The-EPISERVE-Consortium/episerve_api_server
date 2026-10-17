"""Helpers that start the run workflow from a final benchmark (no UI, no I/O)."""

from app.benchmarks.definition import filters_from_row


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
    state["datasets"] = [{**dataset_row, "data_path": url}]        # the benchmark's generated input file
    state["sql"] = {qid: spec["data_transformation_sql"][0]}
    state["filenames"] = {qid: filename}
    state["config"] = None
    state["benchmark"] = {
        "id": spec["benchmark_id"], "name": benchmark_row["name"], "dataset_qid": qid,
        "data_commit": spec["data_commit"], "prediction_length": spec["config"]["prediction_length"],
        "test_start": spec["test_start"], "test_end": spec["test_end"],
        "date_column": benchmark_row["date_column"],
        "filters": [list(pair) for pair in filters_from_row(benchmark_row)],
        "input_uri": url,        # the generated input file, which a run must use as it is
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


def workflow_problems(benchmark: dict | None, data_path: str, sql: str) -> list[str]:
    """Why a run built from the current workflow input would not qualify for the benchmark.

    ``sql`` is not checked: it can only select from the generated input file, which holds nothing but the history;
    the leaderboard shows which columns the run used."""
    if not benchmark:
        return []
    problems = []
    if (data_path or "") != benchmark["input_uri"]:
        problems.append("the input is not the benchmark's generated input file")
    return problems             # an SQL on that file (a selection of columns or rows) is fine: the file is all it sees
