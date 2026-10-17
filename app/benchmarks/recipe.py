"""How a final benchmark's generated files came about, in words: layouts, sources and the steps to re-create them.

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
            "sha256": entry["sha256"], "url": object_url(manifest, kind),
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


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def steps(row: dict, manifest: dict) -> list[dict]:
    """The steps that turn the sources into the two files, in order.

    Each step is ``{"text", "code", "language"}``; ``code`` (a few lines, may be empty) is the technical core of the
    step, equivalent to what ``app/benchmarks/materialize.py`` does.
    """
    filters = filters_from_row(row)
    sources = source_rows(row, manifest)
    target = next((s for s in sources if s["role"] == "target"), None)
    date_column, target_column = row["date_column"], row["target_column"]
    history_end = row.get("history_end") or "?"
    start = row.get("history_start")
    commit = row.get("data_commit") or "?"
    out = [{
        "text": f"Read the target dataset {row['dataset_qid']} at the pinned data commit {commit[:12]}… "
                "(recorded when the benchmark was finalized; every source is read at this commit).",
        "language": "python",
        "code": f"df = pd.read_parquet({(target or {}).get('url') or '<file URL>'!r})",
    }]
    where = "".join(f" AND {column} = {_sql_text(value)}" for column, value in filters)
    out.append({
        "text": "Select the series: apply the filters, keep valid dates and finite values (at most one row per date "
                "is allowed).",
        "language": "sql",
        "code": f"CREATE VIEW series AS SELECT TRY_CAST({date_column} AS DATE) AS {date_column}, "
                f"TRY_CAST({target_column} AS DOUBLE) AS {target_column} FROM df\n"
                f"WHERE isfinite(TRY_CAST({target_column} AS DOUBLE)){where};",
    })
    lower = f" AND {date_column} >= DATE {_sql_text(str(start))}" if start else ""
    out.append({
        "text": f"Cut the windows. The first query is the input (it ends at the last observation before the test "
                f"start {row['test_start']}), the second the reference (the test window).",
        "language": "sql",
        "code": f"SELECT * FROM series WHERE {date_column} <= DATE {_sql_text(str(history_end))}{lower} "
                f"ORDER BY {date_column};\n"
                f"SELECT * FROM series WHERE {date_column} BETWEEN DATE {_sql_text(str(row['test_start']))} "
                f"AND DATE {_sql_text(str(row['test_end']))} ORDER BY {date_column};",
    })
    for covariate in covariates_from_row(row):
        source = next((s for s in sources if s["role"] == covariate.name), None)
        cond = "".join(f' & (cov["{c}"] == {v!r})' for c, v in covariate.filters)
        cols = list(covariate.columns)
        how = "sum(min_count=1)" if covariate.aggregation == "sum" else f"{covariate.aggregation}()"
        out.append({
            "text": f"Covariate {covariate.name} (dataset {covariate.dataset_qid}): the rows between two input dates "
                    f"are combined by {covariate.aggregation} into the later date (cov is read like df in step 1"
                    f"{', from ' + source['file'] if source and source.get('file') else ''}); dates without rows stay empty, "
                    f"nothing is filled in, and rows after {history_end} are never used. Output columns: "
                    f"{', '.join(covariate.output_columns())}.",
            "language": "python",
            "code": f"d = pd.to_datetime(cov[\"{covariate.date_column}\"])\n"
                    f"rows = cov[(d > grid[0] - grid.to_series().diff().median()) & (d <= \"{history_end}\"){cond}]\n"
                    f"aligned = rows.groupby(np.searchsorted(grid, d[rows.index], side=\"left\"))"
                    f"[{cols!r}].{how}.reindex(range(len(grid)))",
        })
    columns = manifest["input"].get("columns") or [date_column, target_column]
    out.append({
        "text": f"Write both Parquet files (input columns: {', '.join(columns)}) and record the SHA-256 of each; "
                "re-creating them gives the same content (the hash can differ with another pyarrow version).",
        "language": "python",
        "code": "input.to_parquet('input.parquet', index=False)\nreference.to_parquet('reference.parquet', index=False)\n"
                "hashlib.sha256(open('input.parquet', 'rb').read()).hexdigest()",
    })
    out.append({
        "text": f"Store both files as one FDO object ({manifest.get('qid') or manifest['input'].get('qid')}) in the "
                f"lakeFS repository {manifest['repo']}; the commit {manifest['commit'][:12]}… is the version a model "
                "run reads. Implementation: app/benchmarks/materialize.py in episerve_api_server.",
        "language": "", "code": "",
    })
    return out


def display_steps(row: dict, manifest: dict) -> list[dict]:
    """The steps to show: the ones stored with the benchmark when it was finalized; for a benchmark finalized before
    that, steps generated from its definition."""
    return manifest.get("recipe") or steps(row, manifest)
