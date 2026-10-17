"""The FDO objects of a benchmark's generated files."""

import json
import re
from datetime import date

from app.benchmarks import fdo
from app.benchmarks.definition import Benchmark


def test_qids_are_distinct_and_sharded_like_every_other_object():
    first, second = fdo.mint_qids(2)
    assert first != second and all(re.fullmatch(r"Q\d{13}", q) for q in (first, second))
    assert fdo.shard("Q1791234567890") == "17/91/23/Q1791234567890"
    assert fdo.component_path("Q1791234567890", "input.parquet") == "17/91/23/Q1791234567890/components/input.parquet"
    assert fdo.fdo_path("Q1791234567890") == "17/91/23/Q1791234567890/Q1791234567890.fdo.json"


def test_the_description_is_open_immutable_and_names_the_sources():
    bench = Benchmark("Q1", "v", "datum", date(2026, 4, 1), date(2026, 9, 30), data_commit="c" * 40)
    document = json.loads(fdo.build_fdo(
        qid="Q1791234567890", benchmark_id=3, benchmark_name="Flu", role="input", component="input.parquet",
        sha256="a" * 64, rows=10, columns=["datum", "v"], benchmark=bench, source_qids=["Q1"], description="d"))
    assert document["@id"] == "Q1791234567890" and document["kernel"]["immutable"] is True
    assert document["kernel"]["fdo:hasComponent"][0]["componentId"] == "input.parquet"
    assert document["profile"]["accessRights"] == "open"
    assert document["profile"]["episerveBenchmark"] == {
        "id": 3, "role": "input", "rows": 10, "columns": ["datum", "v"], "sha256": "a" * 64,
        "source_qids": ["Q1"], "data_commit": "c" * 40}
    assert document["provenance"]["prov:wasDerivedFrom"] == ["Q1"]
