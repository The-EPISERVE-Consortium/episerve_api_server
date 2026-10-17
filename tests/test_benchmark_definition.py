"""Tests for the benchmark definition: validation, horizon."""

from datetime import date, timedelta

import pytest

from app.benchmarks.definition import (
    Benchmark, DefinitionError, derive_horizon, filters_from_row, normalize_filters, validate,
)


def _b(**overrides):
    base = dict(
        dataset_qid="Q9493814122238", target_column="viruslast_normalisiert", date_column="datum",
        test_start=date(2026, 4, 1), test_end=date(2026, 9, 30),
        filters=(("typ", "Influenza A+B"),), history_end=date(2026, 3, 25),
    )
    return Benchmark(**{**base, **overrides})


@pytest.mark.parametrize("name,overrides,message", [
    ("  ", {}, "name"),
    ("ok", {"dataset_qid": "bad"}, "dataset"),
    ("ok", {"filters": (("", "x"),)}, "filter column must be"),
    ("ok", {"filters": (("typ", ""),)}, "filter value"),
    ("ok", {"input_filename": "../x.parquet"}, "file name"),
    ("ok", {"test_start": date(2026, 10, 1)}, "test start"),
    ("ok", {"history_end": date(2026, 4, 1)}, "history must end before"),
])
def test_validate_rejects(name, overrides, message):
    with pytest.raises(DefinitionError, match=message):
        validate(_b(**overrides), name)


def _weekly(start, n):
    return [start + timedelta(days=7 * i) for i in range(n)]


def test_regular_weekly_horizon():
    h = derive_horizon(_weekly(date(2026, 4, 1), 27), last_history=date(2026, 3, 25))
    assert (h.prediction_length, h.frequency_days, h.regular) == (27, 7, True)


def test_gap_between_history_and_window_is_covered():
    h = derive_horizon(_weekly(date(2026, 4, 15), 25), last_history=date(2026, 3, 25))
    assert h.prediction_length == 27


def test_irregular_spacing_is_flagged():
    h = derive_horizon([date(2026, 4, 1), date(2026, 4, 8), date(2026, 4, 22)], date(2026, 3, 25))
    assert (h.prediction_length, h.frequency_days, h.regular) == (4, 7, False)


@pytest.mark.parametrize("observed,last", [([], date(2026, 3, 25)), ([date(2026, 4, 1)], date(2026, 4, 1)), ([date(2026, 4, 1), date(2026, 4, 8)], date(2026, 4, 5))])
def test_horizon_rejects_empty_or_overlapping(observed, last):
    with pytest.raises(DefinitionError):
        derive_horizon(observed, last)


def _two(**overrides):
    return _b(filters=(("typ", "Influenza A+B"), ("Altersgruppe", "35-59")), **overrides)


def test_several_filters_are_validated_and_there_is_no_limit_on_their_number():
    validate(_two(), "Name")
    many = tuple((f"col{i}", str(i)) for i in range(12))
    validate(_b(filters=many), "Name")                                           # the API takes any number
    with pytest.raises(DefinitionError, match="only once"):
        validate(_b(filters=(("typ", "x"), ("TYP", "y"))), "Name")
    with pytest.raises(DefinitionError, match="value"):
        validate(_b(filters=(("typ", "x"), ("a", ""))), "Name")
    with pytest.raises(DefinitionError, match="filter column must be"):
        validate(_b(filters=(("typ", "x"), (" ", "1"))), "Name")


def test_filters_convert_between_the_row_the_api_and_the_definition():
    assert filters_from_row({"filters": '[["typ", "a"], ["age", "35-59"]]'}) == [("typ", "a"), ("age", "35-59")]
    assert filters_from_row({"filters": [["typ", "a"]]}) == [("typ", "a")]
    assert filters_from_row({"filters": None}) == [] and filters_from_row({}) == [] and filters_from_row({"filters": ""}) == []
    assert normalize_filters([{"column": " typ ", "value": "a"}, ("age", "1")]) == (("typ", "a"), ("age", "1"))
    assert normalize_filters(None) == () and normalize_filters([]) == ()
