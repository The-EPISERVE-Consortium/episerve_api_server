"""Pure helpers for starting the run workflow from a benchmark."""

from app.benchmarks.workflow import apply_benchmark, overlay_config, workflow_problems

COMMIT = "ab" * 32
URL = "https://doip.example/doip/retrieve/Q1791000000001/input.parquet?version=" + "ee" * 32
SPEC = {"benchmark_id": 3, "dataset_qid": "Q1", "data_commit": COMMIT, "input_data_files": [[URL, "input.parquet"]],
        "data_transformation_sql": [""], "input_columns": ["datum", "v"], "config": {"prediction_length": 27, "prediction_offset": 0},
        "test_start": "2026-04-01", "test_end": "2026-09-30"}
ROW = {"id": 3, "name": "Flu", "date_column": "datum", "filters": '[["typ", "a"]]'}
DATASET = {"qid": "Q1", "name": "Wastewater", "data_path": "https://doip.example/doip/retrieve/Q1/d.parquet"}


def _state():
    return {"datasets": [], "model": {"name": "m"}, "sql": {"Q9": "old"}, "config": "{}", "filenames": {"Q9": "x"}}


def test_apply_benchmark_prefills_dataset_url_filename_and_resets_config():
    state = apply_benchmark(_state(), SPEC, DATASET, ROW)
    assert state["datasets"] == [{**DATASET, "data_path": URL}]
    assert state["sql"] == {"Q1": ""} and state["filenames"] == {"Q1": "input.parquet"}
    assert state["config"] is None and state["model"] == {"name": "m"}
    assert state["benchmark"]["id"] == 3 and state["benchmark"]["prediction_length"] == 27
    assert state["benchmark"]["filters"] == [["typ", "a"]] and state["benchmark"]["name"] == "Flu"


def test_overlay_config_only_touches_declared_parameters():
    benchmark = {"prediction_length": 27}
    assert overlay_config({"history_length": 91, "prediction_length": 4}, benchmark,
                          {"history_length", "prediction_length", "prediction_offset"}) == {
        "history_length": 91, "prediction_length": 27, "prediction_offset": 0}
    assert overlay_config({"history_length": 91}, benchmark, {"history_length"}) == {"history_length": 91}
    assert overlay_config({"a": 1}, None, {"prediction_length"}) == {"a": 1}


def test_the_generated_file_must_be_the_input_but_an_sql_selection_on_it_is_fine():
    uri = "https://doip.example/doip/retrieve/Q1791000000001/input.parquet?version=" + "ee" * 32
    spec = {**SPEC, "input_data_files": [[uri, "input.parquet"]]}
    state = apply_benchmark(_state(), spec, DATASET, ROW)
    assert state["datasets"][0]["data_path"] == uri and state["sql"] == {"Q1": ""}
    b = state["benchmark"]
    assert b["input_uri"] == uri
    assert workflow_problems(b, uri, "") == []
    assert workflow_problems(b, uri, "-- SELECT * FROM df WHERE a = 'b'") == []   # a comment is no SQL
    assert workflow_problems(b, DATASET["data_path"], "") == [
        "the input is not the benchmark's generated input file"]
    assert workflow_problems(b, uri, 'SELECT "datum", "v" FROM df') == []          # a column selection is allowed
    assert workflow_problems(b, uri, "SELECT * FROM df WHERE datum >= DATE '2022-01-01'") == []

