"""Recover comparable observations from a run's pinned source without guessing SQL."""

from datetime import datetime, timezone
import json
import re

import duckdb
from app.forecast_preview import data
from app.sql_validation import validate_transformation_sql

_SCALARS = {'+', '-', '*', '/', '//', '%', '**', 'abs', 'round', 'ceil', 'floor',
            'coalesce', 'nullif', 'greatest', 'least', 'lower', 'upper', 'trim',
            'date_trunc', 'strftime', 'strptime', 'try_strptime', 'year', 'month', 'day'}


def _nodes(value):
    """Walk expression dictionaries in a parsed SQL subtree.

    Args:
        value: DuckDB's JSON representation of an expression or query.

    Yields:
        Each nested dictionary, including the root when it is a dictionary.
    """
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def _columns(expression) -> set[str]:
    """Identify column dependencies without confusing aliases with SQL text.

    Args:
        expression: Parsed expression subtree.

    Returns:
        Case-normalized unqualified column names.
    """
    return {node['column_names'][-1].casefold() for node in _nodes(expression)
            if node.get('class') == 'COLUMN_REF'}


def _calendar(value) -> str | None:
    """Normalize explicit dates and ISO weeks without interpreting numeric steps.

    Args:
        value: Original forecast or source axis label.

    Returns:
        UTC ISO timestamp, or None when no explicit calendar coordinate exists.
    """
    text = str(value)
    try:
        if re.fullmatch(r'\d{4}-W\d{2}', text):
            text = datetime.fromisocalendar(int(text[:4]), int(text[6:]), 1).isoformat()
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T].*)?', text):
            return None
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _without_time_bounds(expression, column: str):
    """Remove calendar bounds while preserving population and other row filters.

    Args:
        expression: Parsed WHERE clause or nested predicate.
        column: Unambiguous source column underlying the forecast calendar axis.

    Returns:
        Retained predicate, or None when only calendar bounds were present.

    Raises:
        ValueError: Time and other criteria cannot be separated reliably.
    """
    if not expression or column not in _columns(expression):
        return expression
    if expression.get('type') == 'CONJUNCTION_AND':
        children = [_without_time_bounds(child, column) for child in expression['children']]
        children = [child for child in children if child is not None]
        return {**expression, 'children': children} if len(children) > 1 else (children[0] if children else None)
    if expression.get('type') == 'CONJUNCTION_OR':
        children = [_without_time_bounds(child, column) for child in expression['children']]
        if all(child is None for child in children):
            return None
        raise ValueError('Time bounds are mixed with other filters in an OR expression.')
    constants = [node['value'] for node in _nodes(expression) if node.get('class') == 'CONSTANT']
    if (_columns(expression) == {column}
            and expression.get('type') in {'COMPARE_LESSTHAN', 'COMPARE_LESSTHANOREQUALTO',
                                          'COMPARE_GREATERTHAN', 'COMPARE_GREATERTHANOREQUALTO',
                                          'COMPARE_EQUAL', 'COMPARE_BETWEEN'}
            and constants and all(_calendar(item.get('value')) is not None for item in constants)):
        return None
    raise ValueError('The time filter cannot be expanded reliably.')


def expanded_sql(sql: str | None, axis: str) -> str | None:
    """Expand a simple row-wise query beyond its history bounds without changing values.

    Args:
        sql: Recorded input SQL, or None for an untransformed source.
        axis: Calendar-axis column name in the recorded predictions.

    Returns:
        SQL preserving projections, aliases, population filters and ordering,
        with calendar bounds and LIMIT/OFFSET removed; None for no SQL.

    Raises:
        ValueError: Aggregation, joins, subqueries or other ambiguous SQL is used.
    """
    if not sql:
        return None
    validate_transformation_sql(sql)
    with duckdb.connect(config={'enable_external_access': 'false',
                               'autoinstall_known_extensions': 'false',
                               'autoload_known_extensions': 'false'}) as connection:
        parsed = json.loads(connection.execute('SELECT json_serialize_sql(?)', [sql]).fetchone()[0])
        if parsed.get('error') or len(parsed.get('statements', [])) != 1:
            raise ValueError('The recorded SQL could not be parsed for observations.')
        query = parsed['statements'][0]['node']
        source = query.get('from_table') or {}
        if (query.get('type') != 'SELECT_NODE' or source.get('type') != 'BASE_TABLE'
                or source.get('table_name', '').casefold() != 'df' or source.get('schema_name')
                or source.get('catalog_name') or source.get('sample') or source.get('at_clause')
                or query.get('cte_map', {}).get('map') or query.get('group_expressions')
                or query.get('group_sets') or query.get('having') or query.get('qualify') or query.get('sample')
                or query.get('aggregate_handling') != 'STANDARD_HANDLING'):
            raise ValueError('Observations require a single row-wise SELECT from df.')
        for node in _nodes(query):
            kind = node.get('class')
            if kind and kind not in {'COLUMN_REF', 'CONSTANT', 'FUNCTION', 'CAST', 'CASE',
                                     'COMPARISON', 'CONJUNCTION', 'OPERATOR', 'BETWEEN', 'STAR'}:
                raise ValueError('The query contains an unsupported observation transformation.')
            if kind == 'FUNCTION' and (node.get('function_name', '').casefold() not in _SCALARS
                                       or node.get('schema') or node.get('catalog')):
                raise ValueError('The query is not a supported deterministic row-wise transformation.')
        projections = query['select_list']
        matches = [item for item in projections if (item.get('alias') or (
            item.get('column_names', [''])[-1] if item.get('class') == 'COLUMN_REF' else '')).casefold() == axis.casefold()]
        if len(matches) == 1 and len(_columns(matches[0])) == 1:
            column = next(iter(_columns(matches[0])))
        elif not matches and any(item.get('class') == 'STAR' and not item.get('replace_list')
                                 and not item.get('rename_list') for item in projections):
            column = axis.casefold()
        else:
            raise ValueError('The forecast time column cannot be traced to one source column.')
        query['where_clause'] = _without_time_bounds(query.get('where_clause'), column)
        modifiers = query.get('modifiers', [])
        if any(item['type'] not in {'ORDER_MODIFIER', 'LIMIT_MODIFIER'} for item in modifiers):
            raise ValueError('The query changes row identity through an unsupported modifier.')
        query['modifiers'] = [item for item in modifiers if item['type'] == 'ORDER_MODIFIER']
        return connection.execute('SELECT json_deserialize_sql(?)', [json.dumps(parsed)]).fetchone()[0]


def reference(content: bytes, sql: str | None, predictions: list[dict]) -> dict | None:
    """Extract observed targets only at actual forecast calendar coordinates.

    Args:
        content: Original bytes from the authorized pinned source component.
        sql: Recorded transformation applied to the model input.
        predictions: Parsed output series with calendar axes and target names.

    Returns:
        Series and a table for matching observations, or None if none exist.

    Raises:
        ValueError: Coordinates or transformations are ambiguous or unsupported.
    """
    eligible = [item for item in predictions if not item['position_axis'] and item['points']
                and all(_calendar(point['x']) is not None for point in item['points'])]
    axes = {item['axis'] for item in eligible}
    if not eligible:
        return None
    if len(axes) != 1:
        raise ValueError('Predictions have multiple observation time axes.')
    axis = next(iter(axes))
    transformed = expanded_sql(sql, axis)
    frame = data.input_frame(content, transformed)
    targets = {item['column'] for item in eligible}
    if axis not in frame or not targets.intersection(frame.columns):
        return None
    coordinates = {_calendar(point['x']) for item in eligible for point in item['points']}
    frame = frame.loc[frame[axis].map(_calendar).isin(coordinates)]
    if frame.empty:
        return None
    series = [item for item in data.series(frame, forecast=False) if item['column'] in targets]
    if not series:
        return None
    if any(item['duplicate_x'] for item in series):
        raise ValueError('Observed targets have repeated coordinates without an unambiguous group.')
    return {'series': series, 'table': data.table(frame), 'sql': transformed, 'kind': 'source'}
