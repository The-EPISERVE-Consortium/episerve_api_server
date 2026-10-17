"""The input and reference files of a benchmark with covariates (no lakeFS, no network)."""

from datetime import date, timedelta
from io import BytesIO

import pandas as pd
import pyarrow.parquet as pq
import pytest

from app.benchmarks import materialize
from app.benchmarks.definition import Benchmark, Covariate
from app.leaderboard.loaders import LoadError


def _parquet(frame: pd.DataFrame) -> bytes:
    import pyarrow as pa
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pandas(frame), buffer)
    return buffer.getvalue()


WEDNESDAYS = [date(2026, 1, 7) + timedelta(days=7 * i) for i in range(12)]          # 2026-01-07 .. 2026-03-25
TARGET = _parquet(pd.DataFrame({
    "datum": [d.isoformat() for d in WEDNESDAYS] * 2,
    "region": ["DE"] * 12 + ["FR"] * 12,
    "v": [float(i) for i in range(12)] + [100.0 + i for i in range(12)],
}))
DAYS = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]                     # daily, to 2026-04-30
WEATHER = _parquet(pd.DataFrame({"day": [d.isoformat() for d in DAYS], "temp": [float(i) for i in range(120)],
                                 "rain": [1.0] * 120, "city": ["Berlin"] * 120}))


def _benchmark(**kw):
    base = dict(dataset_qid="Q1", target_column="v", date_column="datum", test_start=date(2026, 3, 18),
                test_end=date(2026, 4, 15), filters=(("region", "DE"),), history_end=date(2026, 3, 11))
    return Benchmark(**{**base, **kw})


def _covariate(**kw):
    base = dict(name="wetter", dataset_qid="Q2", date_column="day", columns=("temp", "rain"),
                filters=(("city", "Berlin"),), aggregation="mean")
    return Covariate(**{**base, **kw})


def test_the_input_has_the_date_the_target_and_the_covariates_aligned_to_the_target_dates():
    built = materialize.build(_benchmark(covariates=(_covariate(),)), date(2026, 3, 11), TARGET, {"wetter": WEATHER})
    assert built.columns == ["datum", "v", "wetter__temp", "wetter__rain"]
    assert built.input["datum"].tolist() == WEDNESDAYS[:10]                      # the history window only
    assert built.input["v"].tolist() == [float(i) for i in range(10)]           # the DE series, not FR
    # each Wednesday gets the mean of the seven days ending on it; day index 0 is 2026-01-01 (a Thursday)
    first = built.input.iloc[1]                                                  # 2026-01-14: days 2026-01-08..14
    assert first["wetter__temp"] == pytest.approx(sum(range(7, 14)) / 7)
    assert built.input.iloc[0]["wetter__temp"] == pytest.approx(sum(range(0, 7)) / 7)   # 2026-01-01..07: the first week reaches back one step


def test_sum_and_last_aggregate_differently_over_the_same_days():
    bench = _benchmark(covariates=(_covariate(name="s", aggregation="sum", columns=("rain",)),
                                   _covariate(name="l", aggregation="last", columns=("temp",))))
    built = materialize.build(bench, date(2026, 3, 11), TARGET, {"s": WEATHER, "l": WEATHER})
    assert built.input["s__rain"].tolist()[1:] == [7.0] * 9                      # seven daily values of 1.0
    assert built.input.iloc[1]["l__temp"] == 13.0                                # the value on 2026-01-14


def test_nothing_after_the_history_end_reaches_the_input():
    built = materialize.build(_benchmark(covariates=(_covariate(aggregation="last", columns=("temp",)),)),
                              date(2026, 3, 11), TARGET, {"wetter": WEATHER})
    last_day = (date(2026, 3, 11) - date(2026, 1, 1)).days                      # index of the history end in WEATHER
    assert built.input["wetter__temp"].max() == float(last_day)
    assert built.input["datum"].max() == date(2026, 3, 11)


def test_the_reference_holds_only_the_test_window_of_the_target():
    built = materialize.build(_benchmark(), date(2026, 3, 11), TARGET, {})
    assert built.columns == ["datum", "v"]                                       # no covariates, the plain series
    assert built.reference.columns.tolist() == ["datum", "v"]
    assert built.reference["datum"].tolist() == [date(2026, 3, 18), date(2026, 3, 25)]
    assert built.reference["v"].tolist() == [10.0, 11.0]


def test_gaps_stay_empty_and_are_reported():
    sparse = _parquet(pd.DataFrame({"day": ["2026-01-02"], "temp": [5.0], "rain": [0.0], "city": ["Berlin"]}))
    built = materialize.build(_benchmark(covariates=(_covariate(),)), date(2026, 3, 11), TARGET, {"wetter": sparse})
    assert built.input["wetter__temp"].isna().sum() == 9                         # only the first Wednesday has data
    assert built.missing["wetter__temp"] == pytest.approx(9 / 10, abs=1e-3)
    assert any("empty" in warning for warning in built.warnings)


def test_a_missing_column_or_an_empty_covariate_is_refused_with_the_reason():
    with pytest.raises(LoadError, match="no column 'nope'"):
        materialize.build(_benchmark(covariates=(_covariate(columns=("nope",)),)), date(2026, 3, 11), TARGET,
                          {"wetter": WEATHER})
    with pytest.raises(LoadError, match="no rows"):
        materialize.build(_benchmark(covariates=(_covariate(filters=(("city", "Paris"),)),)), date(2026, 3, 11),
                          TARGET, {"wetter": WEATHER})
    with pytest.raises(LoadError, match="several observations"):
        materialize.build(_benchmark(filters=()), date(2026, 3, 11), TARGET, {})  # DE and FR share every date


def test_the_parquet_is_reproducible_and_hashed():
    built = materialize.build(_benchmark(covariates=(_covariate(),)), date(2026, 3, 11), TARGET, {"wetter": WEATHER})
    data, digest = materialize.to_parquet(built.input)
    again, digest_again = materialize.to_parquet(built.input)
    assert digest == digest_again and len(digest) == 64 and data == again
    assert pq.read_table(BytesIO(data)).column_names == built.columns
