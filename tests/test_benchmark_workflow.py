"""Pure helpers for starting the run workflow from a benchmark."""

from app.benchmarks.workflow import apply_benchmark, clear_benchmark, overlay_config, workflow_problems

COMMIT = "ab" * 32
URL = f"https://doip.example/doip/retrieve/Q1/d.parquet?version={COMMIT}"
SQL = ("SELECT CAST(\"datum\" AS DATE) AS \"datum\", \"v\" FROM df WHERE \"typ\" = 'a' "
       "AND CAST(\"datum\" AS DATE) <= DATE '2026-03-25' ORDER BY \"datum\"")
SPEC = {"benchmark_id": 3, "dataset_qid": "Q1", "data_commit": COMMIT, "input_data_files": [[URL, "input.parquet"]],
        "data_transformation_sql": [SQL], "config": {"prediction_length": 27, "prediction_offset": 0},
        "test_start": "2026-04-01", "test_end": "2026-09-30"}
ROW = {"id": 3, "name": "Flu", "date_column": "datum", "filter_column": "typ", "filter_value": "a"}
DATASET = {"qid": "Q1", "name": "Wastewater", "data_path": "https://doip.example/doip/retrieve/Q1/d.parquet"}


def _state():
    return {"datasets": [], "model": {"name": "m"}, "sql": {"Q9": "old"}, "config": "{}", "filenames": {"Q9": "x"}}


def test_apply_benchmark_prefills_dataset_url_sql_filename_and_resets_config():
    state = apply_benchmark(_state(), SPEC, DATASET, ROW)
    assert state["datasets"] == [{**DATASET, "data_path": URL}]
    assert state["sql"] == {"Q1": SQL} and state["filenames"] == {"Q1": "input.parquet"}
    assert state["config"] is None and state["model"] == {"name": "m"}
    assert state["benchmark"]["id"] == 3 and state["benchmark"]["prediction_length"] == 27
    assert state["benchmark"]["filter_value"] == "a" and state["benchmark"]["name"] == "Flu"


def test_clear_benchmark():
    state = apply_benchmark(_state(), SPEC, DATASET, ROW)
    clear_benchmark(state)
    assert state["benchmark"] is None


def test_overlay_config_only_touches_declared_parameters():
    benchmark = {"prediction_length": 27}
    assert overlay_config({"history_length": 91, "prediction_length": 4}, benchmark,
                          {"history_length", "prediction_length", "prediction_offset"}) == {
        "history_length": 91, "prediction_length": 27, "prediction_offset": 0}
    assert overlay_config({"history_length": 91}, benchmark, {"history_length"}) == {"history_length": 91}
    assert overlay_config({"a": 1}, None, {"prediction_length"}) == {"a": 1}


def test_no_problems_for_the_prefilled_input():
    state = apply_benchmark(_state(), SPEC, DATASET, ROW)
    assert workflow_problems(state["benchmark"], URL, SQL, "datum", "typ", "a") == []


def test_problems_are_explained():
    b = apply_benchmark(_state(), SPEC, DATASET, ROW)["benchmark"]
    assert any("commit" in p for p in workflow_problems(b, DATASET["data_path"], SQL, "datum", "typ", "a"))
    leaky = SQL.replace("2026-03-25", "2026-04-10")
    assert any("test" in p for p in workflow_problems(b, URL, leaky, "datum", "typ", "a"))
    assert any("filter" in p for p in workflow_problems(b, URL, SQL.replace("\"typ\" = 'a' AND ", ""), "datum", "typ", "a"))
    assert any("history" in p for p in workflow_problems(b, URL, "SELECT 1", "datum", "typ", "a"))
    assert workflow_problems(None, URL, "SELECT 1", "datum", None, None) == []
