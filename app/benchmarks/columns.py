"""Which columns of a benchmark's input file a model run used.

A run may select columns (or rows) of the generated input file with a ``data_transformation_sql`` that the pull step
applies before the model starts. The file is the only data the SQL can see, so nothing outside the benchmark can get in.
The columns are worked out by running the SQL on an empty table with the file's columns.
"""

import duckdb


def used_columns(manifest: dict, sql_list) -> list[str] | None:
    """The columns the model got to see: all input columns without SQL, else the columns the SQL returns.

    Args:
        manifest: The benchmark's generated files (the input file's columns).
        sql_list: The run's ``data_transformation_sql`` (one entry per input; empty entries mean no SQL).

    Returns:
        The column names in order, or None if the SQL could not be evaluated.
    """
    columns = list(manifest["input"].get("columns") or [])
    sql = next((s.strip() for s in (sql_list or []) if s and s.strip()), "")
    if not sql or not columns:
        return columns
    date_column = columns[0]                                                   # the date is the first column
    definition = ", ".join(f'"{name.replace(chr(34), chr(34) * 2)}" {"DATE" if name == date_column else "DOUBLE"}'
                           for name in columns)
    try:
        with duckdb.connect(config={
            "enable_external_access": "false", "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false", "memory_limit": "128MB", "threads": "1",
        }) as connection:
            connection.execute(f"CREATE TABLE df ({definition})")
            result = connection.execute(f"SELECT * FROM ({sql.rstrip(';')}) AS q LIMIT 0")
            return [item[0] for item in result.description]
    except (duckdb.Error, ValueError):
        return None
