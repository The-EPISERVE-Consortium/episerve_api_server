"""Tests for reading a run's inputs from its DOIP provenance."""

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


def test_commit_is_lower_cased_and_unparsable_entries_are_skipped():
    upper = URL.replace(COMMIT, COMMIT.upper())
    parsed = parse_run_inputs(_meta([{"@id": upper}, {"@id": "https://example.org/other"}, {"nope": 1}, 5]))
    assert [(p.dataset_qid, p.commit) for p in parsed] == [("Q9493814122238", COMMIT)]
