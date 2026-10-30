"""The FDO objects of a benchmark's generated files."""

import json
import re
from datetime import date

from app.benchmarks import fdo
from app.benchmarks.definition import Benchmark


def test_a_qid_is_sharded_like_every_other_object():
    assert re.fullmatch(r"Q\d{13}", fdo.mint_qid())
    assert fdo.shard("Q1791234567890") == "17/91/23/Q1791234567890"
    assert fdo.component_path("Q1791234567890", "input.parquet") == "17/91/23/Q1791234567890/components/input.parquet"
    assert fdo.fdo_path("Q1791234567890") == "17/91/23/Q1791234567890/Q1791234567890.fdo.json"


def test_the_description_is_one_open_immutable_object_with_a_component_per_file(monkeypatch):
    monkeypatch.setattr(fdo.settings, "doip_url", "https://doip.episerve.zib.de")
    bench = Benchmark("Q1", "v", "datum", date(2026, 4, 1), date(2026, 9, 30), data_commit="c" * 40)
    files = {"input": {"component": "input.parquet", "sha256": "a" * 64, "rows": 10, "columns": ["datum", "v"]},
             "reference": {"component": "reference.parquet", "sha256": "b" * 64, "rows": 2, "columns": ["datum", "v"]}}
    document = json.loads(fdo.build_fdo(
        qid="Q1791234567890", benchmark_id=3, benchmark_name="Flu", files=files, benchmark=bench,
        source_qids=["Q1"], description="d"))
    assert document["@id"] == "Q1791234567890" and document["kernel"]["immutable"] is True
    assert [c["componentId"] for c in document["kernel"]["fdo:hasComponent"]] == ["input.parquet", "reference.parquet"]
    assert document["profile"]["accessRights"] == "open" and document["profile"]["display_name"] == "Benchmark 3: Flu"
    assert document["profile"]["episerveBenchmark"] == {
        "id": 3, "files": files, "source_qids": ["Q1"], "data_commit": "c" * 40}
    assert document["provenance"]["prov:wasDerivedFrom"] == [
        {"@id": "Q1", "schema:version": "c" * 40, "schema:url": f"https://doip.episerve.zib.de/doip/retrieve/Q1?version={'c' * 40}"}]


def test_the_provenance_names_each_source_file_at_the_pinned_commit(monkeypatch):
    monkeypatch.setattr(fdo.settings, "doip_url", "https://doip.example/")
    commit = "d" * 40
    bench = Benchmark("Q1", "v", "datum", date(2026, 4, 1), date(2026, 9, 30), data_commit=commit)
    sources = [{"role": "target", "qid": "Q1", "component": "cases.parquet"},
               {"role": "wetter", "qid": "Q2", "component": "weather.parquet"},
               {"role": "wetter2", "qid": "Q2", "component": "weather.parquet"}]
    document = json.loads(fdo.build_fdo(
        qid="Q1791234567890", benchmark_id=3, benchmark_name="Flu", files={}, benchmark=bench,
        source_qids=["Q1", "Q2"], description="d", sources=sources))
    assert document["provenance"]["prov:wasDerivedFrom"] == [
        {"@id": "Q1", "schema:name": "cases.parquet", "schema:roleName": "target", "schema:version": commit,
         "schema:url": f"https://doip.example/doip/retrieve/Q1/cases.parquet?version={commit}"},
        {"@id": "Q2", "schema:name": "weather.parquet", "schema:roleName": "wetter", "schema:version": commit,
         "schema:url": f"https://doip.example/doip/retrieve/Q2/weather.parquet?version={commit}"},
    ]
    assert document["profile"]["episerveBenchmark"]["source_qids"] == ["Q1", "Q2"]
    assert document["profile"]["episerveBenchmark"]["data_commit"] == commit
