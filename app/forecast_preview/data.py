"""Build bounded chart series without model names or internal-model assumptions."""

from io import BytesIO
import math
import numbers
import re
from threading import Timer

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from app.sql_validation import validate_transformation_sql

MAX_ROWS = 250_000
MAX_COLUMNS = 100
MAX_DECODED_BYTES = 128 * 1024 * 1024
MAX_SERIES = 100
MAX_POINTS = 1500


def input_frame(data: bytes, sql: str | None) -> pd.DataFrame:
    """Replay saved workflow SQL on its supplied Parquet table in isolation.

    Args:
        data: Bounded bytes from the pinned source file.
        sql: Saved single SELECT, or None when the workflow applied no SQL.

    Returns:
        Complete transformed input within the preview row and column limits.

    Raises:
        ValueError: Source or result exceeds limits, or SQL is unsuitable.
        Exception: Parquet decoding or isolated query execution failed.
    """
    source = pq.ParquetFile(BytesIO(data))
    if (source.metadata.num_rows > MAX_ROWS or len(source.schema_arrow) > MAX_COLUMNS
            or sum(source.metadata.row_group(i).total_byte_size for i in range(source.metadata.num_row_groups)) > MAX_DECODED_BYTES):
        raise ValueError('Input exceeds the preview row or column limit.')
    table = source.read()
    if not sql:
        return table.to_pandas()
    validate_transformation_sql(sql)
    with duckdb.connect(config={
        'enable_external_access': 'false', 'allow_unsigned_extensions': 'false',
        'autoinstall_known_extensions': 'false', 'autoload_known_extensions': 'false',
        'memory_limit': '256MB', 'threads': '1', 'temp_directory': '',
    }) as connection:
        connection.register('df', table)
        connection.execute('SET lock_configuration = true')
        timer = Timer(15, connection.interrupt)
        timer.daemon = True
        timer.start()
        try:
            result = connection.execute(sql)
            columns = [item[0] for item in result.description]
            if len(columns) > MAX_COLUMNS or len(set(columns)) != len(columns):
                raise ValueError('Input has too many or duplicate result columns.')
            rows = result.fetchmany(MAX_ROWS + 1)
            if len(rows) > MAX_ROWS:
                raise ValueError('Transformed input exceeds the preview row limit.')
            return pd.DataFrame(rows, columns=columns)
        finally:
            timer.cancel()


def predictions_frame(data: bytes) -> pd.DataFrame:
    """Decode bounded TSV predictions without guessing numeric dates.

    Args:
        data: Recorded predictions.tsv bytes.

    Returns:
        Output table retaining original axis labels.

    Raises:
        ValueError: Predictions are empty or exceed the preview limits.
    """
    frame = pd.read_csv(BytesIO(data), sep='\t', nrows=MAX_ROWS + 1)
    if frame.empty or len(frame) > MAX_ROWS or len(frame.columns) > MAX_COLUMNS:
        raise ValueError('Predictions are empty or exceed the preview limits.')
    return frame


def _number(value) -> float | None:
    """Convert a chart value to a finite JSON number or a gap.

    Args:
        value: Numeric or missing cell.

    Returns:
        Finite number, or None when the value is missing or nonnumeric.
    """
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _axis(value) -> str:
    """Preserve dates, step positions, and categorical axis values as labels.

    Args:
        value: Original axis cell.

    Returns:
        Readable stable axis label, without converting integers to timestamps.
    """
    if pd.isna(value):
        raise ValueError('The time or position column contains missing values.')
    if isinstance(value, (pd.Timestamp,)):
        return value.date().isoformat() if value == value.normalize() else value.isoformat()
    if isinstance(value, numbers.Real) and float(value).is_integer():
        return str(int(value))
    text = str(value)
    return text[:10] if re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]00:00:00(?:\.0+)?', text) else text


def series(frame: pd.DataFrame, *, forecast: bool) -> list[dict]:
    """Identify generic numeric series, optional groups, and reported quantiles.

    Args:
        frame: Transformed supplied input or recorded prediction table.
        forecast: Whether matching _q10/_q90 columns represent output bounds.

    Returns:
        Series with original x labels, grouping values, and explicit sampling counts.

    Raises:
        ValueError: Columns or grouping cannot yield bounded unambiguous series.
    """
    if frame.empty or len(frame.columns) < 2 or not frame.columns.is_unique:
        raise ValueError('A preview needs an axis and at least one numeric value column.')
    columns = list(frame.columns)
    date_columns = []
    for column in columns:
        sample = frame[column].dropna().head(100)
        if (pd.api.types.is_datetime64_any_dtype(sample) or (len(sample) and sample.map(str).str.fullmatch(
                r'\d{4}-(?:W\d{2}|\d{2}-\d{2}(?:[ T].*)?)').all())):
            date_columns.append(column)
    if len(date_columns) > 1:
        raise ValueError('Multiple time columns were found; use the data table to inspect this output.')
    axis = date_columns[0] if date_columns else columns[0]
    remaining = [column for column in columns if column != axis]
    numeric = [c for c in remaining if pd.api.types.is_numeric_dtype(frame[c]) and c != 'x_auto_converted']
    if forecast:
        numeric = [c for c in numeric if not (str(c).endswith(('_q10', '_q90')) and str(c)[:-4] in columns)]
    group_columns = [c for c in remaining if c not in numeric and c != 'x_auto_converted'
                     and not pd.api.types.is_numeric_dtype(frame[c]) and not frame[c].isna().all()]
    if len(group_columns) > 6:
        raise ValueError('Too many categorical columns to identify preview series.')
    # Labels determined entirely by the axis (for example a season) do not
    # distinguish simultaneous series. Keep constant labels as provenance.
    constants = {str(c): str(frame[c].iloc[0]) for c in group_columns if frame[c].nunique(dropna=False) == 1}
    group_columns = [c for c in group_columns if c not in constants
                     and frame.groupby(axis, dropna=False)[c].nunique(dropna=False).max() > 1]
    grouped = frame.groupby(group_columns, sort=False, dropna=False) if group_columns else [((), frame)]
    out = []
    for key, group in grouped:
        values = key if isinstance(key, tuple) else (key,)
        labels = {**constants, **{str(c): str(v) for c, v in zip(group_columns, values)}}
        for column in numeric:
            if len(out) >= MAX_SERIES:
                raise ValueError('Too many series; use the source file to explore these data.')
            points = []
            positions = sorted({round(i * (len(group) - 1) / (min(len(group), MAX_POINTS) - 1))
                                for i in range(min(len(group), MAX_POINTS))}) if len(group) > 1 else [0]
            for _, row in group.iloc[positions].iterrows():
                point = {'x': _axis(row[axis]), 'value': _number(row[column])}
                if forecast:
                    point.update(q10=_number(row.get(str(column) + '_q10')), q90=_number(row.get(str(column) + '_q90')))
                points.append(point)
            out.append({'column': str(column), 'axis': str(axis), 'groups': labels,
                        'label': str(column) + (' · ' + ', '.join(f'{k}={v}' for k, v in labels.items()) if labels else ''),
                        'points': points, 'rows': len(group), 'sampled': len(group) > MAX_POINTS,
                        'duplicate_x': group[axis].duplicated().any().item()})
    if not out:
        raise ValueError('No numeric series could be identified.')
    return out


def table(frame: pd.DataFrame) -> dict:
    """Provide a small escaped-by-the-client table for ambiguous chart mappings.

    Args:
        frame: Supplied input or predictions with original columns.

    Returns:
        String cells for the first one hundred rows, plus the total row count.
    """
    return {'columns': [str(column) for column in frame.columns], 'rows': [
        ['' if pd.isna(value) else str(value) for value in row]
        for row in frame.head(100).itertuples(index=False, name=None)], 'total': len(frame)}
