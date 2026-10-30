"""Upstream lineage as OpenLineage events, built from FDO records (shapes copied from the live platform)."""

import pytest
from fastapi.testclient import TestClient

from app import lineage
from app.main import app
from app.routers import lineage as lineage_router

DOIP = "https://doip.example"
COMMIT = "93ee770fc2315d4328496dec08b84e8fe6f0996d0ae6d9572aec448eab79f23a"
PINNED = "da311f1908de978c33133f7983fd49df59570a1335fcd1effc1b5ead76fc68e3"


def _dataset_fdo(qid, name, file, source):
    return {
        "@id": qid,
        "kernel": {"@id": qid, "modified": "2026-10-30T00:00:16Z",
                   "fdo:hasComponent": [{"@id": f"components/{file}", "componentId": file}]},
        "profile": {"@type": "Dataset", "name": name, "url": source, "additionalType": "incidence"},
        "provenance": {"prov:generatedAtTime": "2026-10-30T00:00:16Z",
                       "prov:wasAttributedTo": "EPISERVE Consortium dataset downloader",
                       "prov:wasDerivedFrom": {"@id": f"{qid}-raw", "prov:hadPrimarySource": source}},
    }


FDOS = {
    "Q3274128860531": _dataset_fdo(
        "Q3274128860531", "grippeweb", "GrippeWeb.parquet",
        "https://raw.githubusercontent.com/robert-koch-institut/GrippeWeb_Daten_des_Wochenberichts/"),
    "Q1679286399912": _dataset_fdo("Q1679286399912", "corona_incidence_states", "covid.parquet",
                                   "https://raw.githubusercontent.com/robert-koch-institut/COVID"),
    "Q6940002821235": _dataset_fdo("Q6940002821235", "weather_berlin_daily", "forecast.parquet",
                                   "https://api.open-meteo.com/v1/forecast?latitude=52.52"),
    # a model run that read a dataset at a version
    "Q1792420591934": {
        "kernel": {"modified": "2026-10-19T14:37:00Z", "fdo:hasComponent": [
            {"@id": f"components/{path}", "componentId": path.rsplit("/", 1)[-1]} for path in (
                "input/config.json", "input/config_prefect.json", "output/predictions.tsv", "output/run.log",
                "ro-crate-metadata.json")]},
        "profile": {"name": "run"},
        "provenance": {
            "@type": "prov:Activity",
            "prov:startedAtTime": "2026-10-19T14:36:31Z", "prov:endedAtTime": "2026-10-19T14:36:56Z",
            "prov:wasAssociatedWith": {"@id": "ghcr.io/x/model__baseline:latest", "schema:name": "model__baseline"},
            "prov:used": [{"@id": f"{DOIP}/doip/retrieve/Q3274128860531/GrippeWeb.parquet?version={COMMIT}"}],
            "schema:agent": {"@id": "episerve_user_8"},
        },
    },
    # a benchmark finalized before its FDO named the source files (bare QIDs + data_commit)
    "Q1792274309877": {
        "kernel": {"modified": "2026-10-17T21:58:29Z", "fdo:hasComponent": [
            {"componentId": "input.parquet"}, {"componentId": "reference.parquet"}]},
        "profile": {"additionalType": "benchmarks",
                    "episerveBenchmark": {"id": 3, "source_qids": ["Q1679286399912", "Q6940002821235"],
                                          "data_commit": PINNED}},
        "provenance": {"prov:generatedAtTime": "2026-10-17T21:58:29Z",
                       "prov:wasDerivedFrom": ["Q1679286399912", "Q6940002821235"]},
    },
    # a run on that benchmark's input
    "Q1792500000001": {
        "kernel": {"fdo:hasComponent": [{"componentId": "predictions.tsv"}]},
        "provenance": {"@type": "prov:Activity", "prov:endedAtTime": "2026-10-20T10:00:00Z",
                       "prov:wasAssociatedWith": {"@id": "ghcr.io/x/timesfm:latest", "schema:name": "timesfm"},
                       "prov:used": [{"@id": f"{DOIP}/doip/retrieve/Q1792274309877/input.parquet?version=b{'0' * 39}"}]},
    },
    "Q7611536028271": {"kernel": {"digitalObjectType": "https://schema.org/SoftwareApplication"},
                       "profile": {"@type": "SoftwareApplication"}},
}


def fetch(qid, version=None):
    return FDOS.get(qid)


@pytest.fixture(autouse=True)
def _doip(monkeypatch):
    monkeypatch.setattr(lineage.settings, "doip_url", DOIP + "/")


def _names(datasets):
    return [d["name"] for d in datasets]


def test_model_run_leads_to_the_dataset_version_it_read_and_its_source():
    result = lineage.upstream("Q1792420591934", fetch)
    assert result["kind"] == "run" and result["unresolved"] == [] and result["truncated"] == []
    run, download = result["events"]
    assert run["job"] == {"namespace": "episerve", "name": "model-runner/model__baseline", "facets": run["job"]["facets"]}
    assert run["job"]["facets"]["jobType"]["jobType"] == "MODEL_RUN"
    assert run["eventType"] == "COMPLETE" and run["eventTime"] == "2026-10-19T14:36:56Z"
    assert run["inputs"] == [{"namespace": DOIP, "name": "Q3274128860531/GrippeWeb.parquet", "facets": {
        "version": {"_producer": lineage.PRODUCER, "_schemaURL": lineage.VERSION_FACET, "datasetVersion": COMMIT}}}]
    # the DOIP path of the file (output/…); config, log and RO-Crate are not output
    assert _names(run["outputs"]) == ["Q1792420591934/output/predictions.tsv"]
    assert run["run"]["facets"]["nominalTime"]["nominalStartTime"] == "2026-10-19T14:36:31Z"
    assert run["run"]["facets"]["episerve"]["executed_by"] == "episerve_user_8"
    assert download["job"]["name"] == "dataset-downloader/grippeweb"
    assert download["inputs"][0]["namespace"] == "https://raw.githubusercontent.com"
    assert _names(download["outputs"]) == ["Q3274128860531/GrippeWeb.parquet"]   # = the run's input name


def test_run_on_a_benchmark_follows_to_the_pinned_sources():
    events = lineage.upstream("Q1792500000001", fetch)["events"]
    assert [e["job"]["name"] for e in events] == [
        "model-runner/timesfm", "benchmark/3",
        "dataset-downloader/corona_incidence_states", "dataset-downloader/weather_berlin_daily"]
    bench = events[1]
    assert _names(bench["outputs"]) == ["Q1792274309877/input.parquet", "Q1792274309877/reference.parquet"]
    # older benchmark: bare QIDs get the pinned commit and the source's only file, so names match the downloads
    assert _names(bench["inputs"]) == ["Q1679286399912/covid.parquet", "Q6940002821235/forecast.parquet"]
    assert {i["facets"]["version"]["datasetVersion"] for i in bench["inputs"]} == {PINNED}
    assert events[3]["inputs"][0]["name"] == "v1/forecast"


def test_benchmark_with_versioned_source_files():
    fdo = dict(FDOS["Q1792274309877"])
    fdo["provenance"] = {"prov:generatedAtTime": "2026-10-31T00:00:00Z", "prov:wasDerivedFrom": [
        {"@id": "Q1679286399912", "schema:name": "covid.parquet", "schema:version": "c" * 40,
         "schema:url": f"{DOIP}/doip/retrieve/Q1679286399912/covid.parquet?version={'c' * 40}"}]}
    event, parents = lineage.event_for("Q1792274309877", fdo)
    assert parents == [("Q1679286399912", "c" * 40)]
    assert event["inputs"][0]["name"] == "Q1679286399912/covid.parquet"
    assert event["inputs"][0]["facets"]["version"]["datasetVersion"] == "c" * 40


def test_model_has_no_upstream_event():
    assert lineage.upstream("Q7611536028271", fetch) == {
        "object": "Q7611536028271", "kind": "model", "events": [], "unresolved": [], "truncated": []}


def test_missing_parent_is_listed_and_reads_are_bounded():
    fdos = {k: v for k, v in FDOS.items() if k != "Q3274128860531"}
    result = lineage.upstream("Q1792420591934", lambda q, v=None: fdos.get(q))
    assert result["unresolved"] == ["Q3274128860531"] and len(result["events"]) == 1
    assert lineage.upstream("Q1792500000001", fetch, max_objects=2)["truncated"] == [
        "Q1679286399912", "Q6940002821235"]


def test_run_ids_are_stable():
    first = lineage.upstream("Q1792420591934", fetch)["events"][0]["run"]["runId"]
    assert first == lineage.upstream("Q1792420591934", fetch)["events"][0]["run"]["runId"]


@pytest.mark.parametrize("ref,name,namespace,qid", [
    ("Q1", None, None, None),     # too short for a QID: an external reference
    ("Q1792420591934", "Q1792420591934", DOIP, "Q1792420591934"),
    (f"lakefs://data-processed/{'a' * 64}/32/74/12/Q3274128860531/components/x.parquet",
     "Q3274128860531/x.parquet", DOIP, "Q3274128860531"),
    ("lakefs://data-raw/main/incidence/x.tsv", "incidence/x.tsv", "lakefs://data-raw", None),
])
def test_parse_input_forms(ref, name, namespace, qid):
    entry, found = lineage.parse_input(ref)
    assert found == qid
    if name:
        assert entry["name"] == name and entry["namespace"] == namespace


client = TestClient(app)


def test_endpoint(monkeypatch):
    monkeypatch.setattr(lineage_router, "fetch", fetch)
    monkeypatch.setattr(lineage_router, "latest", lambda q: None)
    r = client.get("/lineage/events", params={"object": "q1792420591934"})
    assert r.status_code == 200 and r.json()["object"] == "Q1792420591934" and len(r.json()["events"]) == 2
    r = client.get("/lineage/events", params={"object": "Q1792420591934", "format": "ndjson"})
    assert r.headers["content-type"].startswith("application/x-ndjson") and len(r.text.splitlines()) == 2
    assert client.get("/lineage/events", params={"object": "Q1000000000000"}).status_code == 404
    assert client.get("/lineage/events", params={"object": "abc"}).status_code == 422


def test_endpoint_doip_down(monkeypatch):
    def boom(qid):
        raise RuntimeError("down")
    monkeypatch.setattr(lineage_router, "fetch", lambda q, v: boom(q))
    monkeypatch.setattr(lineage_router, "latest", lambda q: None)
    assert client.get("/lineage/events", params={"object": "Q1792420591934"}).status_code == 502


def test_lakefs_uri_keeps_its_version_query():
    entry, qid = lineage.parse_input(
        f"lakefs://benchmarks/main/17/92/27/Q1792274309877/components/input.parquet?version={'e' * 64}")
    assert qid == "Q1792274309877"
    assert entry["name"] == "Q1792274309877/input.parquet" and entry["namespace"] == DOIP
    assert entry["facets"]["version"]["datasetVersion"] == "e" * 64
    entry, _ = lineage.parse_input(f"lakefs://data-raw/main/incidence/x.tsv?version={'f' * 64}")
    assert entry["facets"]["version"]["datasetVersion"] == "f" * 64


def test_datasets_are_read_at_the_version_used():
    old = _dataset_fdo("Q3274128860531", "grippeweb", "GrippeWeb.parquet",
                       "https://raw.githubusercontent.com/robert-koch-institut/GrippeWeb_Daten_des_Wochenberichts/")
    old["provenance"]["prov:generatedAtTime"] = "2026-10-19T00:00:10Z"
    calls = []

    def versioned(qid, version=None):
        calls.append((qid, version))
        if qid == "Q3274128860531" and version == COMMIT:
            return old
        return FDOS.get(qid) if version is None else None

    run, download = lineage.upstream("Q1792420591934", versioned)["events"]
    assert ("Q3274128860531", COMMIT) in calls
    # the download that produced the version the run read, not today's
    assert download["eventTime"] == "2026-10-19T00:00:10Z"
    assert download["outputs"][0]["facets"]["version"]["datasetVersion"] == COMMIT
    assert download["run"]["facets"]["episerve"]["version"] == COMMIT


def test_version_not_found_falls_back_to_the_current_fdo():
    run, download = lineage.upstream("Q1792420591934", lambda q, v=None: None if v else FDOS.get(q))["events"]
    assert download["eventTime"] == "2026-10-30T00:00:16Z" and "version" not in download["outputs"][0].get("facets", {})
    newest = {"Q3274128860531": "e" * 64}
    run, download = lineage.upstream("Q1792420591934", lambda q, v=None: None if v else FDOS.get(q),
                                     latest=newest.get)["events"]
    assert _version(download["outputs"][0]) == "e" * 64      # the current download, at its newest commit


def test_one_dataset_read_at_two_versions_gives_two_downloads():
    a, b = "a" * 64, "b" * 64
    run = {"provenance": {"@type": "prov:Activity", "prov:endedAtTime": "2026-10-20T00:00:00Z",
                          "prov:wasAssociatedWith": {"schema:name": "m"},
                          "prov:used": [{"@id": f"{DOIP}/doip/retrieve/Q3274128860531/GrippeWeb.parquet?version={a}"},
                                        {"@id": f"{DOIP}/doip/retrieve/Q3274128860531/GrippeWeb.parquet?version={b}"}]},
           "kernel": {}}

    def versioned(qid, version=None):
        if qid == "Q1792599999999":
            return run
        fdo = _dataset_fdo(qid, "grippeweb", "GrippeWeb.parquet", "https://raw.githubusercontent.com/x")
        fdo["provenance"]["prov:generatedAtTime"] = {a: "2026-10-01T00:00:00Z", b: "2026-10-02T00:00:00Z"}.get(version, "now")
        return fdo

    events = lineage.upstream("Q1792599999999", versioned)["events"]
    assert [e["eventTime"] for e in events[1:]] == ["2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"]


def _version(entry):
    return ((entry.get("facets") or {}).get("version") or {}).get("datasetVersion")


def test_written_files_get_the_version_read_downstream_else_the_newest():
    newest = {"Q1792500000001": "c" * 64, "Q1792274309877": "d" * 64}
    run, bench, *downloads = lineage.upstream("Q1792500000001", fetch, latest=newest.get)["events"]
    assert [_version(o) for o in run["outputs"]] == ["c" * 64]                 # the run itself: its newest commit
    assert {_version(o) for o in bench["outputs"]} == {"b" + "0" * 39}         # what the run read
    assert all(_version(o) == PINNED for d in downloads for o in d["outputs"])


def test_download_event_carries_the_source_change_date():
    fdo = _dataset_fdo("Q1", "x", "x.parquet", "https://example.org/x.csv")
    fdo["provenance"]["source_changed_at"] = "2026-10-16T08:00:00Z"
    event, _ = lineage.event_for("Q1", fdo)
    assert event["run"]["facets"]["episerve"]["source_changed_at"] == "2026-10-16T08:00:00Z"
