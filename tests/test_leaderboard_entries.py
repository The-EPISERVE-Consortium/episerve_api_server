"""Tests for run qualification against a benchmark's generated input file."""

from datetime import date

from app.leaderboard.entries import HistoryWindow, qualify_run

MANIFEST = {"repo": "benchmarks", "branch": "main", "commit": "ee" * 32,
            "input": {"path": "3/input.parquet", "first_date": "2021-01-06", "last_date": "2021-12-29"}}
URI = "lakefs://benchmarks/main/3/input.parquet"


def _run(**overrides):
    return {"qid": "Q10", "status": "success", "input_sources": [f"{URI}?version={'ee' * 32}"], **overrides}


def test_a_run_that_read_the_generated_input_file_qualifies_with_its_history_window():
    result = qualify_run(_run(), MANIFEST)
    assert result.relevant and result.reason is None
    assert result.window == HistoryWindow(date(2021, 1, 6), date(2021, 12, 29))


def test_another_version_of_the_file_is_refused_with_both_ids():
    result = qualify_run(_run(input_sources=[f"{URI}?version={'dd' * 32}"]), MANIFEST)
    assert result.relevant and "dddddddddddd" in result.reason and "eeeeeeeeeeee" in result.reason


def test_other_inputs_besides_the_file_are_refused():
    result = qualify_run(_run(input_sources=[*_run()["input_sources"], "https://doip/doip/retrieve/Q1/x"]), MANIFEST)
    assert result.relevant and "exactly that file" in result.reason


def test_a_failed_run_is_listed_with_the_reason():
    assert qualify_run(_run(status="failed"), MANIFEST).reason.startswith("run status is failed")


def test_runs_that_did_not_read_the_file_are_not_relevant():
    other = _run(input_sources=["lakefs://benchmarks/main/4/input.parquet"])
    assert qualify_run(other, MANIFEST).relevant is False
    assert qualify_run({"qid": "Q12", "status": "success"}, MANIFEST).relevant is False
