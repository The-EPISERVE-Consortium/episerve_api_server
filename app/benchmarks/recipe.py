"""How a final benchmark's generated files came about: layouts, sources with their filters, windows, SQL and merge rules.

Pure functions over the stored benchmark row and manifest (no I/O), so the Data tab can show them without a
call to any data store.
"""

from app.benchmarks.definition import covariates_from_row, filters_from_row, object_url
from app.config import settings


CATALOG_URL = "https://data.episerve.zib.de"      # the public address (the pod's CKAN_URL is the cluster-internal one)


def _filters_text(filters) -> str:
    return " and ".join(f"{column} = {value!r}" for column, value in filters)


def file_layouts(row: dict, manifest: dict) -> list[dict]:
    """One entry per generated file: what a row is, its columns with roles, size, date range and download URL."""
    owner = {name: covariate.name for covariate in covariates_from_row(row) for name in covariate.output_columns()}
    layouts = []
    for kind, title, meaning in (
        ("input", "input.parquet", "an observation date of the target series in the input window"),
        ("reference", "reference.parquet", "an observation date of the target series in the test window"),
    ):
        entry = manifest[kind]
        columns = [{"column": name,
                    "role": "date" if name == row["date_column"] else "target" if name == row["target_column"]
                    else f"covariate ({owner.get(name, '?')})"} for name in entry.get("columns")
                   or [row["date_column"], row["target_column"]]]
        layouts.append({
            "kind": kind, "title": title, "meaning": meaning, "columns": columns, "rows": entry["rows"],
            "first_date": entry.get("first_date"), "last_date": entry.get("last_date"),
            "sha256": entry["sha256"],
            # files in the older layout are only in lakeFS (inside the cluster): no download or catalog link
            "url": object_url(manifest, kind) if "qid" in entry else None,
            "catalog": f"{CATALOG_URL}/dataset/{entry['qid'].lower()}" if "qid" in entry else None,
        })
    return layouts


def source_rows(row: dict, manifest: dict) -> list[dict]:
    """The datasets the files were generated from, with links to the catalog entry and to the file at the pinned
    data commit. Older manifests do not list their sources; the target is then taken from the benchmark row."""
    sources = manifest.get("sources") or [
        {"role": "target", "qid": row["dataset_qid"], "component": row.get("component_name")}]
    commit = row.get("data_commit")
    base = settings.doip_url.rstrip("/")
    rows = []
    for source in sources:
        component = source.get("component")
        rows.append({
            "role": source["role"], "qid": source["qid"],
            "catalog": f"{CATALOG_URL}/dataset/{source['qid'].lower()}",
            "file": component,
            "url": f"{base}/doip/retrieve/{source['qid']}/{component}" + (f"?version={commit}" if commit else "")
            if component else None,
        })
    return rows


def column_rows(layouts: list[dict]) -> list[dict]:
    """One table for both files: every column once, with its role and the files it is in."""
    in_files = {layout["kind"]: {c["column"] for c in layout["columns"]} for layout in layouts}
    first = layouts[0]["columns"]
    return [{"column": c["column"], "role": c["role"],
             "input": "yes" if c["column"] in in_files["input"] else "-",
             "reference": "yes" if c["column"] in in_files.get("reference", set()) else "-"} for c in first]


def _ident(name: str) -> str:
    """A column name as a quoted SQL identifier (names can contain hyphens, spaces and brackets)."""
    return '"' + name.replace('"', '""') + '"'


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


RULES = [
    "Dates and the target are cast with TRY_CAST (to date and to double); rows that do not convert, and target "
    "values that are not finite (NaN, +/-inf), are dropped.",
    "Filters compare the column as text (CAST(column AS VARCHAR) = 'value'); all filters of a source are combined "
    "with AND.",
    "Both ends of each window are inclusive.",
    "At most one row per date is allowed in the target series; otherwise finalizing fails and a filter has to be "
    "added.",
]


def _code(row: dict, manifest: dict, sources: list[dict]) -> str:
    """A Python script that re-creates both files from the sources (equivalent to ``materialize.build``)."""
    filters = filters_from_row(row)
    covariates = covariates_from_row(row)
    date_column, target_column = row["date_column"], row["target_column"]
    history_end = str(row.get("history_end") or "?")
    start = str(row.get("history_start") or "1900-01-01")
    date_id, target_id = _ident(date_column), _ident(target_column)
    commit = row.get("data_commit") or "?"
    lines = [
        "import duckdb, numpy as np, pandas as pd", "",
        f"COMMIT = {commit!r}      # every source is read at this lakeFS commit",
        f"HISTORY_END = {history_end!r}", "",
        "# sources",
    ]
    names = {"target": "target"}
    names.update({c.name: f"cov{i}" for i, c in enumerate(covariates)})
    for source in sources:
        url = source.get("url") or "<file URL>"
        lines.append(f"{names[source['role']]} = pd.read_parquet({url.split('?version=')[0]!r} + f'?version={{COMMIT}}')")
    where = "".join(f"\n          AND CAST({_ident(c)} AS VARCHAR) = {_sql_text(v)}" for c, v in filters)
    lines += [
        "", "# target series between two dates (see the rules above)",
        "def series(start, end):",
        '    df = duckdb.sql(f"""',
        f"        SELECT TRY_CAST({date_id} AS DATE) AS date, TRY_CAST({target_id} AS DOUBLE) AS truth",
        "        FROM target",
        f"        WHERE TRY_CAST({date_id} AS DATE) BETWEEN DATE '{{start}}' AND DATE '{{end}}'{where}",
        f"          AND isfinite(TRY_CAST({target_id} AS DOUBLE))",
        '        ORDER BY 1""").df()',
        '    assert not df["date"].duplicated().any(), "several observations per date; add a filter"',
        '    return df.assign(date=pd.to_datetime(df["date"]))', "",
        f"history = series({start!r}, HISTORY_END)",
        f"reference = series({str(row['test_start'])!r}, {str(row['test_end'])!r})",
        "grid = pd.DatetimeIndex(history['date'])",
        f"input_df = pd.DataFrame({{{date_column!r}: history['date'].dt.date, {target_column!r}: history['truth']}})",
    ]
    for covariate in covariates:
        var = names[covariate.name]
        how = {"sum": "sum(min_count=1)", "last": "last()"}.get(covariate.aggregation, "mean()")
        cols, outs = list(covariate.columns), covariate.output_columns()
        mask = "".join(f" & ({var}[{c!r}].astype(str) == {v!r})" for c, v in covariate.filters)
        lines += [
            "", f"# covariate {covariate.name!r}: rows after the previous grid date and up to each date, combined by "
                f"{covariate.aggregation}",
            f"rows = pd.DataFrame({{'date': pd.to_datetime({var}[{covariate.date_column!r}], errors='coerce').dt.normalize()}})",
            f"for c in {cols!r}:",
            f"    rows[c] = pd.to_numeric({var}[c], errors='coerce')",
            f"rows = rows[rows['date'].notna() & (rows['date'] <= HISTORY_END){mask}]",
            "step = grid.to_series().diff().dropna().median() if len(grid) > 1 else pd.Timedelta(days=1)",
            "slot = np.searchsorted(grid.values, rows['date'].values, side='left')",
            "keep = (slot < len(grid)) & (rows['date'].values > (grid[0] - step).to_datetime64())",
            f"agg = rows[keep].assign(slot=slot[keep]).groupby('slot')[{cols!r}].{how}.reindex(range(len(grid)))",
            f"for c, out in zip({cols!r}, {outs!r}):",
            "    input_df[out] = agg[c].to_numpy()",
        ]
    lines += [
        "",
        f"reference_df = pd.DataFrame({{{date_column!r}: reference['date'].dt.date, "
        f"{target_column!r}: reference['truth']}})",
        "input_df.to_parquet('input.parquet', index=False)",
        "reference_df.to_parquet('reference.parquet', index=False)   # same content; the file hash can differ "
        "with another pyarrow version",
    ]
    return "\n".join(lines)


def describe(row: dict, manifest: dict) -> dict:
    """What went into the two files, as data for the Data tab (equivalent to what ``app/benchmarks/materialize.py`` does).

    Returns ``{"commit", "sources", "windows", "rules", "merge", "code"}``: ``sources`` has one entry per dataset
    (role, QID, file, filters, date column, columns taken, links), ``windows`` one per file, ``rules`` the fixed
    rules for casting, filtering and windows, ``merge`` one entry per covariate (empty without) and ``code`` a
    Python script that re-creates both files.
    """
    filters = filters_from_row(row)
    covariates = covariates_from_row(row)
    date_column, target_column = row["date_column"], row["target_column"]
    history_end = str(row.get("history_end") or "?")
    start = row.get("history_start")
    input_info = manifest["input"]
    rows_in = input_info.get("rows", "?")

    by_role = {s["role"]: s for s in source_rows(row, manifest)}
    sources = []
    for role, qid, date_col, columns, flt in (
        [("target", row["dataset_qid"], date_column, [target_column], filters)]
        + [(c.name, c.dataset_qid, c.date_column, list(c.columns), c.filters) for c in covariates]
    ):
        source = by_role.get(role, {})
        sources.append({
            "role": role, "qid": qid, "catalog": source.get("catalog"), "file": source.get("file"),
            "url": source.get("url"), "filters": [f"{c} = {_sql_text(v)}" for c, v in flt] or ["none"],
            "date_column": date_col, "columns": columns,
        })

    windows = [
        {"file": "input.parquet", "from": str(start) if start else str(input_info.get("first_date") or "?"),
         "to": history_end, "rows": rows_in, "content": "target" + (" and covariates" if covariates else "")},
        {"file": "reference.parquet", "from": str(row["test_start"]), "to": str(row["test_end"]),
         "rows": manifest["reference"].get("rows", "?"), "content": "target only"},
    ]
    merge = [{
        "covariate": c.name, "joined_to": f"the target's {rows_in} dates",
        "rule": f"rows with {c.date_column} after the previous target date and up to the date itself, combined by "
                f"{c.aggregation} into that date; nothing is filled in, rows after {history_end} are never used",
        "columns": c.output_columns(),
    } for c in covariates]
    return {"commit": row.get("data_commit") or "?", "sources": sources, "windows": windows, "rules": RULES,
            "merge": merge, "code": _code(row, manifest, sources)}


def display(row: dict, manifest: dict) -> dict:
    """What to show: the description stored with the benchmark when it was finalized; for a benchmark whose stored
    description is in an older format (a list of steps, or without the rules and the code) or missing, one generated from its definition."""
    stored = manifest.get("recipe")
    return stored if isinstance(stored, dict) and "code" in stored else describe(row, manifest)
