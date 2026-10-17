"""How a final benchmark's generated files came about, in words: layouts, sources and the steps to re-create them.

Pure functions over the stored benchmark row and manifest (no I/O), so the Data tab can show them without a
call to any data store.
"""

from app.benchmarks.definition import covariates_from_row, filters_from_row, object_url
from app.config import settings


def _filters_text(filters) -> str:
    return " and ".join(f"{column} = {value!r}" for column, value in filters)


def file_layouts(row: dict, manifest: dict) -> list[dict]:
    """One entry per generated file: what a row is, its columns with roles, size, date range and download URL."""
    owner = {name: covariate.name for covariate in covariates_from_row(row) for name in covariate.output_columns()}
    layouts = []
    for kind, title, meaning in (
        ("input", "input.parquet", "one row per observation date of the target series in the input window"),
        ("reference", "reference.parquet", "one row per observation date of the target series in the test window"),
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
            "catalog": f"{settings.ckan_url.rstrip('/')}/dataset/{source['qid'].lower()}",
            "file": component,
            "url": f"{base}/doip/retrieve/{source['qid']}/{component}" + (f"?version={commit}" if commit else "")
            if component else None,
        })
    return rows


def steps(row: dict, manifest: dict) -> list[str]:
    """The steps that turn the sources into the two files, in order."""
    filters = filters_from_row(row)
    target = next((s for s in source_rows(row, manifest) if s["role"] == "target"), None)
    file_text = f"the file {target['file']} of " if target and target["file"] else "the file of "
    date_column, target_column = row["date_column"], row["target_column"]
    start = row.get("history_start")
    out = [
        f"Pin the data: all sources are read from the lakeFS repository data-processed at commit "
        f"{row.get('data_commit') or '?'}, recorded when the benchmark was finalized.",
        f"Target series: read {file_text}dataset {row['dataset_qid']}"
        + (f" and keep the rows where {_filters_text(filters)}" if filters else "")
        + f". Take the date from {date_column} and the value from {target_column}; rows whose date or value is "
          "empty or not a finite number are dropped. The result must have at most one row per date.",
        f"Cut the windows: input = the observations from {start or 'the start of the data'} to {row.get('history_end') or '?'} "
        f"(the last observation before the test start {row['test_start']}); reference = the observations from "
        f"{row['test_start']} to {row['test_end']}.",
    ]
    for covariate in covariates_from_row(row):
        where = f", keeping the rows where {_filters_text(covariate.filters)}" if covariate.filters else ""
        out.append(
            f"Covariate {covariate.name}: read dataset {covariate.dataset_qid}{where}; date column "
            f"{covariate.date_column}, numeric columns {', '.join(covariate.columns)}. Every row belongs to the first "
            f"input date on or after its own date (and after the previous input date); the rows of one input date "
            f"are combined by {covariate.aggregation} into {', '.join(covariate.output_columns())}. Input dates "
            f"without rows stay empty, nothing is filled in, and rows after {row.get('history_end') or '?'} are never used.")
    columns = ", ".join(manifest["input"].get("columns") or [date_column, target_column])
    out.append(f"Write input.parquet (columns {columns}) from the input window and reference.parquet (columns "
               f"{date_column}, {target_column}) from the test window, both as Parquet; the SHA-256 of each file is "
               "recorded.")
    out.append(f"Store both files as one FDO object with its own QID ({manifest.get('qid') or manifest['input'].get('qid')}) "
               f"in the lakeFS repository {manifest['repo']}; the commit {manifest['commit']} is the version a model "
               "run reads. The procedure is implemented in app/benchmarks/materialize.py of episerve_api_server; "
               "re-creating the files from the pinned sources gives files with the same SHA-256.")
    return out
