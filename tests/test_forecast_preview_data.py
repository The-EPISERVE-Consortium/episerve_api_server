"""Generic chart extraction and sandbox tests without model-specific rules."""

from io import BytesIO

import pandas as pd
import pytest

from app.forecast_preview import data


def parquet(frame):
    """Encode a test source.

    Args:
        frame: Input table fixture.

    Returns:
        Parquet bytes.
    """
    output = BytesIO()
    frame.to_parquet(output, index=False)
    return output.getvalue()


def test_workflow_sql_is_replayed_but_model_history_is_not():
    """Keep every supplied row after SQL regardless of model configuration."""
    raw = parquet(pd.DataFrame({'day': ['2026-01-01', '2026-01-02'], 'value': [1, 2], 'factor': [3, 4]}))
    frame = data.input_frame(raw, 'SELECT day, value * factor AS target FROM df WHERE value > 1')
    result = data.series(frame, forecast=False)
    assert result[0]['column'] == 'target'
    assert result[0]['points'] == [{'x': '2026-01-02', 'value': 8.0}]


@pytest.mark.parametrize('sql', ['DELETE FROM df', "SELECT * FROM read_csv('/etc/passwd')", 'SELECT * FROM df; SELECT 1'])
def test_replay_refuses_writes_external_access_and_multiple_statements(sql):
    """Prevent saved SQL from performing work outside the registered input.

    Args:
        sql: Unsafe or unsupported transformation.
    """
    with pytest.raises(Exception):
        data.input_frame(parquet(pd.DataFrame({'x': [1], 'y': [2]})), sql)


def test_grouped_weekly_input_discovers_time_column_without_model_names():
    """Keep disease groups distinct while a season remains axis metadata."""
    frame = pd.DataFrame({'count': [10, 20, 11, 21], 'season': ['2025/26'] * 4,
                          'disease': ['A', 'B', 'A', 'B'], 'week': ['2026-W01', '2026-W01', '2026-W02', '2026-W02'],
                          'region': ['National'] * 4, 'incidence': [1., 2., 3., 4.]})
    result = data.series(frame, forecast=False)
    assert len(result) == 4
    target = next(s for s in result if s['column'] == 'incidence' and s['groups']['disease'] == 'A')
    assert target['axis'] == 'week'
    assert target['points'] == [{'x': '2026-W01', 'value': 1.}, {'x': '2026-W02', 'value': 3.}]


def test_numeric_forecast_positions_are_not_interpreted_as_dates():
    """Retain multiple targets and attach quantiles to their own series."""
    frame = data.predictions_frame(b'day\tx_auto_converted\ta\ta_q10\ta_q90\tb\n109\t109\t5\t3\t8\t20\n')
    result = data.series(frame, forecast=True)
    assert [s['column'] for s in result] == ['a', 'b']
    assert result[0]['points'] == [{'x': '109', 'value': 5., 'q10': 3., 'q90': 8.}]


def test_sampling_preserves_endpoints_and_missing_values():
    """Bound display size without inventing values for missing observations."""
    frame = pd.DataFrame({'x': range(3000), 'y': [float('nan')] + list(range(1, 3000))})
    (result,) = data.series(frame, forecast=False)
    assert result['rows'] == 3000 and result['sampled']
    assert len(result['points']) == data.MAX_POINTS
    assert result['points'][0] == {'x': '0', 'value': None}
    assert result['points'][-1] == {'x': '2999', 'value': 2999.}


def test_duplicate_axes_and_ambiguous_time_columns_are_not_silently_joined():
    """Flag duplicate coordinates and reject ambiguous time-column choices."""
    (result,) = data.series(pd.DataFrame({'x': [1, 1], 'y': [2, 3]}), forecast=False)
    assert result['duplicate_x'] is True
    with pytest.raises(ValueError, match='Multiple time'):
        data.series(pd.DataFrame({'start': ['2026-01-01'], 'end': ['2026-01-02'], 'value': [2]}), forecast=False)


def test_source_and_transformation_result_limits(monkeypatch):
    """Reject oversized decoded sources and oversized query outputs.

    Args:
        monkeypatch: Fixture reducing limits for tiny test inputs.
    """
    raw = parquet(pd.DataFrame({'x': [1], 'y': [2]}))
    monkeypatch.setattr(data, 'MAX_ROWS', 2)
    with pytest.raises(ValueError, match='Transformed input'):
        data.input_frame(raw, 'SELECT * FROM df CROSS JOIN range(3)')
    with pytest.raises(ValueError, match='Input exceeds'):
        data.input_frame(parquet(pd.DataFrame({'x': [1, 2, 3]})), None)


def test_compressed_parquet_is_bounded_before_decoding(monkeypatch):
    """Reject a source whose declared uncompressed size exceeds the budget.

    Args:
        monkeypatch: Fixture reducing the decoded-byte budget.
    """
    monkeypatch.setattr(data, 'MAX_DECODED_BYTES', 1)
    with pytest.raises(ValueError, match='Input exceeds'):
        data.input_frame(parquet(pd.DataFrame({'x': [1], 'y': [2]})), None)
