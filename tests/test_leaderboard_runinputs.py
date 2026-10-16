"""Tests for reading a run's inputs from its DOIP provenance."""

from app.leaderboard import run_inputs
from app.leaderboard.run_inputs import RunInput, parse_run_inputs

COMMIT = "04de04c840dc5f1651978d74e969be38f7e83697fa42c1ef03fea57b4694226a"
URL = f"https://doip.episerve.zib.de/doip/retrieve/Q9493814122238/amelag_aggregierte_kurve.parquet?version={COMMIT}"
SQL = "SELECT 1"


def _meta(used):
    return {"provenance": {"prov:used": used}}


def test_parses_url_commit_and_sql():
    assert parse_run_inputs(_meta([{"@id": URL, "@type": "prov:Entity", "schema:query": SQL}])) == [
        RunInput("Q9493814122238", "amelag_aggregierte_kurve.parquet", COMMIT, SQL)
    ]


def test_url_without_version_has_no_commit_and_strings_are_accepted():
    plain = "https://doip.episerve.zib.de/doip/retrieve/Q1/x.parquet"
    assert parse_run_inputs(_meta([plain])) == [RunInput("Q1", "x.parquet", None, None)]
    assert parse_run_inputs(_meta({"@id": plain})) == [RunInput("Q1", "x.parquet", None, None)]


def test_commit_is_lower_cased_and_unparsable_entries_are_skipped():
    upper = URL.replace(COMMIT, COMMIT.upper())
    parsed = parse_run_inputs(_meta([{"@id": upper}, {"@id": "https://example.org/other"}, {"nope": 1}, 5]))
    assert [(p.dataset_qid, p.commit) for p in parsed] == [("Q9493814122238", COMMIT)]


def test_missing_or_malformed_provenance_gives_an_empty_list():
    for meta in ({}, {"provenance": None}, {"provenance": {"prov:used": "text"}}, "not a dict"):
        assert parse_run_inputs(meta) == []


def test_fetch_uses_the_bounded_metadata_download(monkeypatch):
    seen = {}
    monkeypatch.setattr(run_inputs, "fetch_run_metadata",
                        lambda qid, base: seen.update(qid=qid, base=base) or _meta([URL]))
    (item,) = run_inputs.fetch_run_inputs("Q1788513584728", "https://doip.example")
    assert seen == {"qid": "Q1788513584728", "base": "https://doip.example"} and item.commit == COMMIT
