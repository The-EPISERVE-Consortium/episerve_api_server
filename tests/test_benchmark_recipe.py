"""The description of how a benchmark's files were generated."""

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
    assert layouts[0]["catalog"] == "https://data.episerve.zib.de/dataset/q1791000000001"
    assert "test window" in layouts[1]["meaning"]
    assert recipe.column_rows(layouts) == [
        {"column": "datum", "role": "date", "input": "yes", "reference": "yes"},
        {"column": "v", "role": "target", "input": "yes", "reference": "yes"}]


def test_the_description_lists_sources_filters_windows_and_sql():
    info = recipe.describe(ROW, MANIFEST)
    assert info["commit"] == "c" * 64 and info["merge"] == []
    (target,) = info["sources"]
    assert target["filters"] == ["typ = 'a'", "age = '35-59'"] and target["columns"] == ["v"]
    assert target["url"].endswith("/doip/retrieve/Q1/d.parquet?version=" + "c" * 64)
    assert [(w["file"], w["from"], w["to"], w["rows"], w["content"]) for w in info["windows"]] == [
        ("input.parquet", "2022-03-02", "2026-08-12", 233, "target"),
        ("reference.parquet", "2026-08-19", "2026-10-07", 8, "target only")]
    view, input_sql, reference_sql = info["sql"]
    assert 'CAST("typ" AS VARCHAR) = \'a\'' in view and 'CAST("age" AS VARCHAR) = \'35-59\'' in view
    assert 'isfinite(TRY_CAST("v" AS DOUBLE))' in view
    assert '"datum" <= DATE \'2026-08-12\'' in input_sql
    assert "BETWEEN DATE '2026-08-19' AND DATE '2026-10-07'" in reference_sql


def test_a_history_start_becomes_a_lower_bound_of_the_input_window():
    info = recipe.describe({**ROW, "history_start": date(2025, 2, 5)}, MANIFEST)
    assert "BETWEEN DATE '2025-02-05' AND DATE '2026-08-12'" in info["sql"][1]
    assert info["windows"][0]["from"] == "2025-02-05"


def test_a_covariate_gets_a_source_row_and_a_merge_row():
    cov = [{"name": "wetter", "dataset_qid": "Q2", "date_column": "day", "columns": ["temp"],
            "filters": [["city", "Berlin"]], "aggregation": "mean"}]
    row = {**ROW, "covariates": json.dumps(cov)}
    manifest = {**MANIFEST, "sources": [{"role": "target", "qid": "Q1", "component": "d.parquet"},
                                        {"role": "wetter", "qid": "Q2", "component": "w.parquet"}]}
    info = recipe.describe(row, manifest)
    wetter = info["sources"][1]
    assert (wetter["role"], wetter["file"], wetter["filters"], wetter["date_column"], wetter["columns"]) == (
        "wetter", "w.parquet", ["city = 'Berlin'"], "day", ["temp"])
    assert wetter["url"].endswith("/Q2/w.parquet?version=" + "c" * 64)
    (merge,) = info["merge"]
    assert merge["columns"] == ["wetter__temp"] and merge["joined_to"] == "the target's 233 dates"
    assert "combined by mean" in merge["rule"] and "after 2026-08-12 are never used" in merge["rule"]
    assert info["windows"][0]["content"] == "target and covariates"


# --- the description must do what the code does ---------------------------------------------------------------


def _frames():
    import numpy as np
    import pandas as pd
    days = pd.date_range("2025-01-01", periods=80, freq="7D")
    target = pd.DataFrame({"datum": days.strftime("%Y-%m-%d"), "Inz-7": np.arange(80, dtype=float), "typ": "a",
                           "age": "35-59", "bl": 11})                       # a hyphenated name and a numeric filter
    other = target.assign(typ="b", **{"Inz-7": -1.0, "bl": 12})              # other series that the filters drop
    noisy = pd.DataFrame({"datum": ["2025-01-01"], "Inz-7": [float("nan")], "typ": "a", "age": "35-59", "bl": 11})
    target = pd.concat([target, other, noisy], ignore_index=True)
    daily = pd.date_range("2024-12-01", periods=600, freq="D")
    cov = pd.DataFrame({"day": daily.strftime("%Y-%m-%d"), "temp": np.arange(600, dtype=float) % 17,
                        "city": ["Berlin", "Hamburg"] * 300})
    cov.loc[10:20, "temp"] = float("nan")
    return target, cov


def _parquet(frame):
    from io import BytesIO
    import pyarrow as pa
    import pyarrow.parquet as pq
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), buffer)
    return buffer.getvalue()


def test_the_sql_of_the_description_gives_what_the_builder_gives():
    import duckdb
    import pandas as pd
    from app.benchmarks import materialize
    from app.benchmarks.store import benchmark_from_row

    target, cov = _frames()
    for aggregation in ("mean", "sum", "last"):
        row = {**ROW, "target_column": "Inz-7", "filters": json.dumps([["typ", "a"], ["age", "35-59"], ["bl", "11"]]),
               "history_end": date(2026, 4, 1),
               "test_start": date(2026, 4, 8), "test_end": date(2026, 6, 24), "history_start": date(2025, 2, 5),
               "dataset_qid": "Q1", "covariates": json.dumps([
                   {"name": "wetter", "dataset_qid": "Q2", "date_column": "day", "columns": ["temp"],
                    "filters": [["city", "Berlin"]], "aggregation": aggregation}])}
        manifest = {**MANIFEST, "sources": [{"role": "target", "qid": "Q1", "component": "d.parquet"},
                                            {"role": "wetter", "qid": "Q2", "component": "w.parquet"}]}
        built = materialize.build(benchmark_from_row(row), date(2026, 4, 1), _parquet(target), {"wetter": _parquet(cov)})
        sql = recipe.describe(row, manifest)["sql"]

        connection = duckdb.connect()
        connection.register("df", target)
        connection.execute(sql[0])
        input_table = connection.execute(sql[1].rstrip(";")).df()
        reference_table = connection.execute(sql[2].rstrip(";")).df()
        assert input_table["Inz-7"].tolist() == built.input["Inz-7"].tolist()
        assert pd.to_datetime(input_table["datum"]).dt.date.tolist() == built.input["datum"].tolist()
        assert reference_table["Inz-7"].tolist() == built.reference["Inz-7"].tolist()


def test_the_description_is_stored_with_the_manifest_and_shown_as_stored():
    stored = {"commit": "frozen", "sources": [], "windows": [], "sql": [], "merge": []}
    assert recipe.display(ROW, {**MANIFEST, "recipe": stored}) == stored
    assert recipe.display(ROW, MANIFEST)["sources"][0]["role"] == "target"            # generated when none is stored
    old = [{"text": "old", "code": "", "language": ""}]                                # the earlier format: regenerated
    assert recipe.display(ROW, {**MANIFEST, "recipe": old})["sql"]
