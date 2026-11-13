"""Verify pinned-source observation recovery preserves values and population filters."""

import pandas as pd
import pytest

from app.forecast_preview import data, observations
from tests.test_forecast_preview_data import parquet


def predictions(axis='day', target='target', dates=('2026-01-03', '2026-01-04')):
    """Create recorded forecast series without model-specific assumptions.

    Args:
        axis: Name of the recorded time column.
        target: Name of the recorded target column.
        dates: Recorded forecast coordinates.

    Returns:
        Generic series extracted through the real preview code.
    """
    return data.series(pd.DataFrame({axis: list(dates), target: [1.] * len(dates)}), forecast=True)


def test_observations_preserve_computation_aliases_and_population_filters():
    """Expand the history window without introducing another region or raw values."""
    frame = pd.DataFrame({'date': ['2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04', '2026-01-05', '2026-01-03'],
                          'value': [2, 3, 4, 5, 6, 1000], 'factor': [10] * 6,
                          'region': ['Berlin'] * 5 + ['Hamburg']})
    sql = ("SELECT d.date AS day, d.value * d.factor AS target FROM df d "
           "WHERE d.date BETWEEN '2026-01-01' AND '2026-01-02' AND region = 'Berlin' "
           'ORDER BY d.date DESC LIMIT 1 OFFSET 1')
    raw = parquet(frame)
    supplied = data.input_frame(raw, sql)
    result = observations.reference(raw, sql, predictions())
    assert supplied.to_dict('records') == [{'day': '2026-01-01', 'target': 20}]
    assert result['series'][0]['points'] == [{'x': '2026-01-04', 'value': 50.}, {'x': '2026-01-03', 'value': 40.}]
    assert "Hamburg" not in str(result['table'])
    assert result['kind'] == 'source'
    assert observations.expanded_sql(sql, 'day') == result['sql']


@pytest.mark.parametrize('sql', [None, 'SELECT * FROM df',
    "SELECT day, round(value * 2, 1) AS target FROM df WHERE day < DATE '2026-01-03'",
    "SELECT day, value * 2 AS target FROM df WHERE NOT(day > '2026-01-02')",
    "SELECT CAST(day AS DATE) AS day, value * 2 AS target FROM df WHERE CAST(day AS DATE) <= DATE '2026-01-02'",
    "SELECT day, value * 2 AS target FROM df WHERE day < '2026-01-01' OR day > '2026-02-01'",
])
def test_date_bounds_star_casts_and_absent_sql(sql):
    """Support direct sources and simple deterministic calendar transformations.

    Args:
        sql: Row-wise transformation whose projection remains comparable.
    """
    raw = parquet(pd.DataFrame({'day': ['2026-01-02', '2026-01-03', '2026-01-04', '2026-01-05'],
                                'value': [1, 2, 3, 4], 'target': [2, 4, 6, 8]}))
    result = observations.reference(raw, sql, predictions())
    assert [point['value'] for point in result['series'][0]['points']] == [4, 6]
    assert result['table']['total'] == 2


@pytest.mark.parametrize('sql', [
    "SELECT day, value AS target FROM df WHERE day < '2026-01-03' OR region = 'Berlin'",
    'SELECT day, sum(value) AS target FROM df GROUP BY day',
    'SELECT day, avg(value) OVER (ORDER BY day) AS target FROM df',
    'WITH history AS (SELECT * FROM df) SELECT day, value AS target FROM history',
    'SELECT a.day, a.value AS target FROM df a JOIN df b ON a.day = b.day',
    'SELECT day, value AS target FROM (SELECT * FROM df)',
    'SELECT DISTINCT day, value AS target FROM df',
    'SELECT day, random() AS target FROM df',
    'SELECT day, value AS target FROM df WHERE day < other_day',
    'SELECT day, value AS target FROM df LIMIT 50%',
    "SELECT day, value AS target FROM read_parquet('/etc/passwd')",
    'SELECT day, value AS target FROM df; DELETE FROM df',
])
def test_ambiguous_queries_are_not_reinterpreted(sql):
    """Keep unsupported observation inference separate from supplied-input replay.

    Args:
        sql: Query whose semantics cannot safely be expanded by the preview.
    """
    with pytest.raises(ValueError):
        observations.expanded_sql(sql, 'day')


def test_missing_future_dates_targets_and_numeric_positions_do_not_invent_truth():
    """Leave genuine future forecasts without an observed overlay."""
    raw = parquet(pd.DataFrame({'day': ['2026-01-01'], 'target': [2]}))
    assert observations.reference(raw, None, predictions()) is None
    assert observations.reference(raw, None, predictions(target='other')) is None
    positional = data.series(pd.DataFrame({'day': [2, 3], 'x_auto_converted': [2, 3], 'target': [5, 6]}), forecast=True)
    assert observations.reference(raw, None, positional) is None


def test_weekly_coordinates_and_missing_values_are_preserved():
    """Align ISO weeks to recorded dates without filling missing target values."""
    raw = parquet(pd.DataFrame({'day': ['2026-W01', '2026-W02'], 'target': [None, 3]}))
    result = observations.reference(raw, None, predictions(dates=('2025-12-29', '2026-01-05')))
    assert result['series'][0]['points'] == [{'x': '2026-W01', 'value': None}, {'x': '2026-W02', 'value': 3.}]


def test_duplicate_observations_are_refused_and_grouped_observations_stay_separate():
    """Never silently combine different populations at the same coordinate."""
    frame = pd.DataFrame({'day': ['2026-01-03'] * 2, 'target': [10, 20]})
    with pytest.raises(ValueError, match='repeated coordinates'):
        observations.reference(parquet(frame), None, predictions())
    frame['region'] = ['Berlin', 'Hamburg']
    result = observations.reference(parquet(frame), None, predictions())
    assert len(result['series']) == 2
    assert {item['groups']['region'] for item in result['series']} == {'Berlin', 'Hamburg'}
