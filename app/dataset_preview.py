"""Load approved dataset samples and query them using structured controls."""

from dataclasses import dataclass
from io import BytesIO
from urllib.parse import unquote, urlsplit

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_SAMPLE_BYTES = 64 * 1024 * 1024
MAX_SAMPLE_ROWS = 50_000
PAGE_SIZES = (25, 100, 250, 500)
FILTER_OPERATORS = ("=", "!=", "<", "<=", ">", ">=", "contains", "is null", "is not null")


@dataclass(frozen=True)
class LoadedDataset:
    """Store a server-loaded Arrow sample and its source row count."""

    table: pa.Table
    source_rows: int


@dataclass(frozen=True)
class PreviewOptions:
    """Describe a preview without accepting executable query text."""

    columns: tuple[str, ...] | None = None
    filter_column: str | None = None
    operator: str = "="
    value: str = ""
    sort_column: str | None = None
    direction: str = "ASC"
    page: int = 1
    page_size: int = 100


def resolve_component(rows: list[dict], qid: str) -> dict:
    """Resolve a dataset identifier using only server-held catalog rows.

    Args:
        rows: Catalog records obtained by the server.
        qid: Selected dataset identifier, supplied by the browser.

    Returns:
        A copy of the first catalog component.

    Raises:
        ValueError: If the identifier is ambiguous or has no component.
    """
    if not isinstance(qid, str) or not qid:
        raise ValueError("Select a known dataset.")
    matches = [row for row in rows if row.get("qid") == qid]
    if len(matches) != 1:
        raise ValueError("Select a known dataset.")
    components = matches[0].get("components", [])
    if not components:
        raise ValueError("No downloadable components for this dataset.")
    return dict(components[0])


def validate_component_url(url: str, doip_base: str) -> str:
    """Allow component downloads only from the configured DOIP retrieve path.

    Args:
        url: Component URL from the server-held catalog.
        doip_base: Trusted DOIP service base URL from configuration.

    Returns:
        The validated component URL.

    Raises:
        ValueError: If the URL changes origin or contains an unsafe path.
    """
    if not isinstance(url, str) or not isinstance(doip_base, str):
        raise ValueError("Invalid component URL.")
    try:
        target, base = urlsplit(url), urlsplit(doip_base)
        target_port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
        base_port = base.port if base.port is not None else (443 if base.scheme == "https" else 80)
        target_origin = (target.scheme, target.hostname, target_port)
        base_origin = (base.scheme, base.hostname, base_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid component URL.") from exc
    path = unquote(target.path)
    prefix = base.path.rstrip("/") + "/doip/retrieve/"
    if (
        base.scheme not in ("http", "https") or not base.hostname
        or target_origin != base_origin or target.username is not None
        or target.password is not None or target.fragment
        or not path.startswith(prefix) or path == prefix
        or any(part in (".", "..") for part in path.split("/"))
        or "\\" in path or "%" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in url + path)
    ):
        raise ValueError("Dataset previews require a URL on the configured DOIP retrieve service.")
    return url


def load_dataset(url: str, doip_base: str) -> LoadedDataset:
    """Download an approved Parquet component and decode a bounded row sample.

    Args:
        url: Server-resolved catalog component URL.
        doip_base: Trusted DOIP service base URL.

    Returns:
        The first available rows and the original Parquet row count.

    Raises:
        ValueError: If the URL, download, schema, or decoded sample is unsuitable.
        requests.HTTPError: If the service returns an error.
    """
    validate_component_url(url, doip_base)
    content = BytesIO()
    with requests.get(url, stream=True, allow_redirects=False, timeout=(5, 30)) as response:
        if response.status_code != 200:
            response.raise_for_status()
            raise ValueError("Preview downloads must return a complete file without redirects.")
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if content.tell() + len(chunk) > MAX_DOWNLOAD_BYTES:
                raise ValueError("This component exceeds the 64 MiB preview download limit.")
            content.write(chunk)
    content.seek(0)
    parquet = pq.ParquetFile(content)
    schema = parquet.schema_arrow
    names = schema.names
    if not names or len({name.casefold() for name in names}) != len(names):
        raise ValueError("Preview columns must have unique names, ignoring case.")
    batches = []
    row_count = byte_count = 0
    for batch in parquet.iter_batches(batch_size=1024):
        batch = batch.slice(0, MAX_SAMPLE_ROWS - row_count)
        byte_count += batch.nbytes
        if byte_count > MAX_SAMPLE_BYTES:
            raise ValueError("The decoded preview exceeds the 64 MiB sample limit.")
        batches.append(batch)
        row_count += batch.num_rows
        if row_count >= MAX_SAMPLE_ROWS:
            break
    table = pa.Table.from_batches(batches, schema=schema)
    return LoadedDataset(table=table, source_rows=parquet.metadata.num_rows)


def _column_sql(name: str, names: list[str]) -> str:
    """Validate and quote a schema identifier for generated SQL.

    Args:
        name: Requested column name.
        names: Exact names in the loaded schema.

    Returns:
        A safely quoted SQL identifier.

    Raises:
        ValueError: If the column is unknown or cannot be represented.
    """
    if not isinstance(name, str) or name not in names or "\x00" in name:
        raise ValueError("Select a column from the dataset schema.")
    return '"' + name.replace('"', '""') + '"'


def query_dataset(dataset: LoadedDataset, options: PreviewOptions) -> tuple[pd.DataFrame, int]:
    """Filter, sort, and paginate loaded data with server-generated SQL.

    Args:
        dataset: Arrow data already loaded by approved server-side code.
        options: Structured preview choices; values never become executable SQL.

    Returns:
        The requested page and the number of matching rows in the loaded sample.

    Raises:
        ValueError: If any control contains an unsupported value.
    """
    names = dataset.table.column_names
    if (
        type(options.page) is not int or options.page < 1
        or options.page > max(1, (dataset.table.num_rows + 24) // 25)
        or type(options.page_size) is not int or options.page_size not in PAGE_SIZES
        or options.direction not in ("ASC", "DESC")
        or options.operator not in FILTER_OPERATORS
        or not isinstance(options.value, str) or len(options.value) > 4096
    ):
        raise ValueError("Invalid preview controls.")
    selected = names if options.columns is None else options.columns
    if not isinstance(selected, (tuple, list)) or not selected or len(selected) > len(names):
        raise ValueError("Select at least one dataset column.")
    projection = ", ".join(_column_sql(name, names) for name in selected)
    where = ""
    values = []
    if options.filter_column is not None:
        column = _column_sql(options.filter_column, names)
        if options.operator in ("is null", "is not null"):
            where = f" WHERE {column} {options.operator.upper()}"
        elif options.operator == "contains":
            where = f" WHERE contains(CAST({column} AS VARCHAR), ?)"
            values.append(options.value)
        else:
            where = f" WHERE {column} {options.operator} ?"
            values.append(options.value)
    # Always order by a schema column so pagination is reproducible for ordinary data.
    sort = options.sort_column if options.sort_column is not None else names[0]
    order = f" ORDER BY {_column_sql(sort, names)} {options.direction}"
    with duckdb.connect(config={
        "enable_external_access": "false",
        "allow_unsigned_extensions": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "memory_limit": "128MB",
        "threads": "1",
        "temp_directory": "",
    }) as connection:
        connection.register("dataset", dataset.table)
        connection.execute("SET lock_configuration = true")
        total = connection.execute("SELECT COUNT(*) FROM dataset" + where, values).fetchone()[0]
        result = connection.execute(
            f"SELECT {projection} FROM dataset{where}{order} LIMIT ? OFFSET ?",
            [*values, options.page_size, (options.page - 1) * options.page_size],
        ).df()
    return result, total
