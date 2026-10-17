"""The words that describe how a benchmark's files were generated."""

import json
from datetime import date

from app.benchmarks import recipe

MANIFEST = {"repo": "benchmarks", "branch": "main", "commit": "ee" * 32, "qid": "Q1791000000001",
            "input": {"qid": "Q1791000000001", "component": "input.parquet", "sha256": "a" * 64, "rows": 233,
                      "columns": ["datum", "v"], "first_date": "2022-03-02", "last_date": "2026-08-12"},
            "reference": {"qid": "Q1791000000001", "component": "reference.parquet", "sha256": "b" * 64, "rows": 8}}
ROW = {"dataset_qid": "Q1", "component_name": "d.parquet", "target_column": "v", "date_column": "datum",
       "filters": json.dumps([["typ", "a"], ["age", "35-59"]]), "history_start": None, "history_end": date(2026, 8, 12),
       "test_start": date(2026, 8, 19), "test_end": date(2026, 10, 7), "data_commit": "c" * 64}


def test_an_older_manifest_without_sources_still_names_the_target_file():
    (target,) = recipe.source_rows(ROW, MANIFEST)
    assert (target["role"], target["qid"], target["file"]) == ("target", "Q1", "d.parquet")
    assert target["url"].endswith("/doip/retrieve/Q1/d.parquet?version=" + "c" * 64)
    assert target["catalog"] == "https://data.episerve.zib.de/dataset/q1"


def test_one_column_table_serves_both_files():
    layouts = recipe.file_layouts(ROW, MANIFEST)
    assert layouts[0]["rows"] == 233 and layouts[0]["first_date"] == "2022-03-02"
    assert "test window" in layouts[1]["meaning"]
    assert recipe.column_rows(layouts) == [
        {"column": "datum", "role": "date", "input": "yes", "reference": "yes"},
        {"column": "v", "role": "target", "input": "yes", "reference": "yes"}]


def test_the_steps_carry_short_sql_and_python_for_the_core_of_each_step():
    steps = recipe.steps(ROW, MANIFEST)
    assert [s["language"] for s in steps] == ["python", "sql", "sql", "python", ""]
    assert "pd.read_parquet('https://doip" in steps[0]["code"] and "version=" + "c" * 64 in steps[0]["code"]
    view = steps[1]["code"]
    assert "typ = 'a' AND age = '35-59'" in view and "isfinite(TRY_CAST(v AS DOUBLE))" in view
    windows = steps[2]["code"]
    assert "datum <= DATE '2026-08-12'" in windows and "BETWEEN DATE '2026-08-19' AND DATE '2026-10-07'" in windows
    assert all(len(s["code"].splitlines()) <= 3 for s in steps)
    assert "ee" * 6 in steps[-1]["text"] and "Q1791000000001" in steps[-1]["text"]


def test_a_covariate_step_aligns_with_the_grid_in_three_lines():
    cov = [{"name": "wetter", "dataset_qid": "Q2", "date_column": "day", "columns": ["temp"],
            "filters": [["city", "Berlin"]], "aggregation": "mean"}]
    row = {**ROW, "covariates": json.dumps(cov)}
    manifest = {**MANIFEST, "sources": [{"role": "target", "qid": "Q1", "component": "d.parquet"},
                                        {"role": "wetter", "qid": "Q2", "component": "w.parquet"}]}
    step = recipe.steps(row, manifest)[3]
    assert len(step["code"].splitlines()) == 3 and 'cov["city"] == \'Berlin\'' in step["code"]
    assert "np.searchsorted(grid" in step["code"] and ".mean()" in step["code"]
    assert "from w.parquet" in step["text"] and "wetter__temp" in step["text"]
