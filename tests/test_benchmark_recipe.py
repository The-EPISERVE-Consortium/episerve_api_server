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


def test_layouts_describe_one_row_per_date_and_fall_back_to_date_and_target():
    input_layout, reference_layout = recipe.file_layouts(ROW, MANIFEST)
    assert input_layout["rows"] == 233 and input_layout["first_date"] == "2022-03-02"
    assert reference_layout["columns"] == [{"column": "datum", "role": "date"}, {"column": "v", "role": "target"}]
    assert "test window" in reference_layout["meaning"]


def test_the_steps_name_filters_windows_and_the_stored_commit():
    steps = recipe.steps(ROW, MANIFEST)
    assert len(steps) == 5                                              # pin, target, windows, write, store
    assert "typ = 'a' and age = '35-59'" in steps[1] and "d.parquet" in steps[1]
    assert "start of the data to 2026-08-12" in steps[2] and "2026-08-19 to 2026-10-07" in steps[2]
    assert "ee" * 32 in steps[-1] and "same SHA-256" in steps[-1]
