"""Verify structured previews against injected controls and untrusted URLs."""

from dataclasses import replace
from io import BytesIO

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import dataset_preview as preview


@pytest.fixture
def dataset():
    """Provide typed sample data for query integration tests.

    Returns:
        A dataset containing sortable numeric and text columns.
    """
    return preview.LoadedDataset(pa.table({
        "cases": list(range(60)),
        "region": ["north" if i % 2 else "south" for i in range(60)],
    }), source_rows=60)


def test_filter_sort_projection_and_pagination(dataset):
    """Return the correct filtered page with a matching row count."""
    options = preview.PreviewOptions(
        columns=("cases",), filters=(preview.PreviewFilter("region", value="north"),),
        sort_column="cases", direction="DESC", page_size=25,
    )
    first, total = preview.query_dataset(dataset, options)
    second, second_total = preview.query_dataset(dataset, replace(options, page=2))
    assert total == second_total == 30
    assert list(first.columns) == ["cases"]
    assert first["cases"].tolist() == list(range(59, 10, -2))
    assert second["cases"].tolist() == [9, 7, 5, 3, 1]


def test_filter_value_cannot_execute_sql(dataset, tmp_path):
    """Treat a file-writing SQL payload as literal filter data."""
    target = tmp_path / "injected.csv"
    payload = f"north'; COPY (SELECT 1) TO '{target}'; --"
    result, total = preview.query_dataset(dataset, preview.PreviewOptions(
        filters=(preview.PreviewFilter("cases", ">=", "0"), preview.PreviewFilter("region", value=payload)),
    ))
    assert result.empty and total == 0
    assert not target.exists()


def test_schema_names_with_quotes_are_identifiers(tmp_path):
    """Safely query a real column whose name resembles executable SQL."""
    target = tmp_path / "injected.csv"
    name = f'cases" FROM dataset; COPY (SELECT 1) TO \'{target}\'; --'
    data = preview.LoadedDataset(pa.table({name: [2, 1]}), source_rows=2)
    result, total = preview.query_dataset(data, preview.PreviewOptions(columns=(name,)))
    assert result[name].tolist() == [1, 2]
    assert total == 2 and not target.exists()


@pytest.mark.parametrize("changes", [
    {"columns": ("read_text('/etc/passwd')",)},
    {"sort_column": "cases; SELECT 1"},
    {"filters": (preview.PreviewFilter("cases OR TRUE"),)},
    {"filters": (preview.PreviewFilter("cases", "= ?; SELECT 1; --"),)},
    {"direction": "ASC; SELECT 1"},
    {"page": 0}, {"page": True}, {"page": 10_000},
    {"page_size": 100_000}, {"page_size": "100"},
    {"columns": ()}, {"filters": (preview.PreviewFilter("region", value="x" * 4097),)},
    {"filters": ("raw SQL",)},
    {"filters": (preview.PreviewFilter("cases"),) * (preview.MAX_FILTERS + 1)},
    {"filters": (preview.PreviewFilter("cases", ">", "0"), preview.PreviewFilter("region", "OR TRUE"))},
])
def test_invalid_controls_are_rejected(dataset, changes):
    """Reject forged controls before executing a database query.

    Args:
        dataset: Typed test data.
        changes: Invalid options supplied by the parameterized case.
    """
    with pytest.raises(ValueError):
        preview.query_dataset(dataset, replace(preview.PreviewOptions(), **changes))


def test_contains_uses_literal_text_and_null_filter():
    """Keep substring wildcards literal and handle null filters."""
    data = preview.LoadedDataset(pa.table({"text": ["100%", "100x", None]}), 3)
    result, total = preview.query_dataset(data, preview.PreviewOptions(
        filters=(preview.PreviewFilter("text", "contains", "%"),),
    ))
    assert total == 1 and result["text"].tolist() == ["100%"]
    result, total = preview.query_dataset(data, preview.PreviewOptions(
        filters=(preview.PreviewFilter("text", "is null"),),
    ))
    assert total == 1 and result["text"].isna().all()


def test_multiple_filters_use_and_and_keep_pagination_consistent(dataset):
    """Apply multiple conditions, including repeated columns, before pagination.

    Args:
        dataset: Typed test data.
    """
    options = preview.PreviewOptions(
        filters=(
            preview.PreviewFilter("region", "contains", "north"),
            preview.PreviewFilter("cases", ">=", "5"),
            preview.PreviewFilter("cases", "<", "60"),
        ),
        page_size=25,
    )
    first, total = preview.query_dataset(dataset, options)
    second, second_total = preview.query_dataset(dataset, replace(options, page=2))
    assert total == second_total == 28
    assert first["cases"].tolist() == list(range(5, 55, 2))
    assert second["cases"].tolist() == [55, 57, 59]


def test_null_filters_do_not_shift_bound_values():
    """Bind each value to its own condition when null checks require no value."""
    data = preview.LoadedDataset(pa.table({
        "cases": [1, 2, 3, 4], "region": ["north", "north", "south", "north"],
        "note": [None, "done", None, None],
    }), 4)
    result, total = preview.query_dataset(data, preview.PreviewOptions(filters=(
        preview.PreviewFilter("region", "=", "north"),
        preview.PreviewFilter("note", "is null"),
        preview.PreviewFilter("cases", ">", "1"),
    )))
    assert total == 1 and result["cases"].tolist() == [4]


def test_forged_component_fields_are_ignored():
    """Resolve browser identifiers to server catalog components exclusively."""
    trusted = {"name": "data.parquet", "url": "https://doip.example/doip/retrieve/Q1/data.parquet"}
    rows = [{"qid": "Q1", "components": [trusted]}]
    forged = {"qid": "Q1", "components": [{"url": "http://internal/secret"}]}
    assert preview.resolve_component(rows, forged["qid"]) == trusted
    with pytest.raises(ValueError):
        preview.resolve_component(rows, "unknown")
    with pytest.raises(ValueError):
        preview.resolve_component(rows * 2, "Q1")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://doip.example/doip/retrieve/Q1/a.parquet",
    "https://internal/doip/retrieve/Q1/a.parquet",
    "https://doip.example.evil/doip/retrieve/Q1/a.parquet",
    "https://doip.example:444/doip/retrieve/Q1/a.parquet",
    "https://doip.example:0/doip/retrieve/Q1/a.parquet",
    "https://user:pass@doip.example/doip/retrieve/Q1/a.parquet",
    "https://doip.example/admin", "https://doip.example/doip/retrieve/",
    "https://doip.example/doip/retrieve/../admin",
    "https://doip.example/doip/retrieve/%2e%2e/admin",
    "https://doip.example/doip/retrieve/%252e%252e/admin",
    "https://doip.example/doip/retrieve/Q1/a%5cb.parquet",
    "https://doip.example/doip/retrieve/Q1/a.parquet#fragment",
    "https://doip.example/doip/retrieve/Q1/a\n.parquet",
])
def test_unapproved_urls_are_rejected_before_download(monkeypatch, url):
    """Block filesystem access, origin changes, and normalized path escapes.

    Args:
        monkeypatch: Pytest patch fixture.
        url: Unapproved component URL.
    """
    def unexpected_request(*args, **kwargs):
        """Fail if URL validation allows an outbound request.

        Args:
            *args: Unexpected positional request arguments.
            **kwargs: Unexpected keyword request arguments.

        Raises:
            AssertionError: Always, because invalid URLs must not be fetched.
        """
        pytest.fail("Invalid URL reached the network")

    monkeypatch.setattr(preview.requests, "get", unexpected_request)
    with pytest.raises(ValueError):
        preview.load_dataset(url, "https://doip.example")


class FakeResponse:
    """Represent an in-memory streaming response without network access."""

    def __init__(self, content: bytes, status_code: int = 200):
        """Initialize a fake response.

        Args:
            content: Response bytes.
            status_code: HTTP response status.
        """
        self.content = content
        self.status_code = status_code

    def __enter__(self):
        """Enter the response context.

        Returns:
            This response.
        """
        return self

    def __exit__(self, *args):
        """Exit the response context.

        Args:
            *args: Exception context arguments.
        """

    def raise_for_status(self):
        """Reject fake HTTP errors.

        Raises:
            RuntimeError: If the status denotes an error.
        """
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def iter_content(self, chunk_size):
        """Yield response data in chunks.

        Args:
            chunk_size: Maximum chunk length.

        Yields:
            Response byte chunks.
        """
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]


def install_response(monkeypatch, content: bytes, status_code: int = 200):
    """Install a fake downloader and record request options.

    Args:
        monkeypatch: Pytest patch fixture.
        content: Response payload.
        status_code: Response status.

    Returns:
        A list of recorded request keyword arguments.
    """
    calls = []

    def get(url, **kwargs):
        """Return an in-memory response and capture downloader settings.

        Args:
            url: Requested component URL.
            **kwargs: Downloader options.

        Returns:
            An in-memory response.
        """
        calls.append(kwargs)
        return FakeResponse(content, status_code)

    monkeypatch.setattr(preview.requests, "get", get)
    return calls


def test_approved_parquet_loads_without_duckdb_network_access(monkeypatch):
    """Load approved Parquet bytes and query only the bounded in-memory sample."""
    data = BytesIO()
    pq.write_table(pa.table({"cases": list(range(12))}), data)
    calls = install_response(monkeypatch, data.getvalue())
    monkeypatch.setattr(preview, "MAX_SAMPLE_ROWS", 5)
    loaded = preview.load_dataset("https://doip.example:443/doip/retrieve/Q1/a.parquet", "https://doip.example")
    result, total = preview.query_dataset(loaded, preview.PreviewOptions())
    assert loaded.source_rows == 12 and loaded.table.num_rows == 5
    assert result["cases"].tolist() == [0, 1, 2, 3, 4] and total == 5
    assert calls == [{"stream": True, "allow_redirects": False, "timeout": (5, 30)}]


@pytest.mark.parametrize("status", [206, 301, 302, 307, 308])
def test_redirects_and_partial_files_are_rejected(monkeypatch, status):
    """Reject responses that could change destinations or truncate Parquet files.

    Args:
        monkeypatch: Pytest patch fixture.
        status: Rejected response status.
    """
    install_response(monkeypatch, b"", status_code=status)
    with pytest.raises(ValueError):
        preview.load_dataset("https://doip.example/doip/retrieve/Q1/a.parquet", "https://doip.example")


def test_download_limit_is_enforced_without_content_length(monkeypatch):
    """Stop streamed downloads exceeding the actual byte budget."""
    install_response(monkeypatch, b"12345")
    monkeypatch.setattr(preview, "MAX_DOWNLOAD_BYTES", 4)
    with pytest.raises(ValueError, match="download limit"):
        preview.load_dataset("https://doip.example/doip/retrieve/Q1/a.parquet", "https://doip.example")


def test_decoded_sample_limit_is_enforced(monkeypatch):
    """Reject a Parquet sample whose decoded size exceeds its budget."""
    data = BytesIO()
    pq.write_table(pa.table({"text": ["x" * 100]}), data)
    install_response(monkeypatch, data.getvalue())
    monkeypatch.setattr(preview, "MAX_SAMPLE_BYTES", 32)
    with pytest.raises(ValueError, match="sample limit"):
        preview.load_dataset("https://doip.example/doip/retrieve/Q1/a.parquet", "https://doip.example")


def test_case_ambiguous_schema_is_rejected(monkeypatch):
    """Reject names that differ only in case before DuckDB resolves identifiers."""
    data = BytesIO()
    pq.write_table(pa.table({"Name": [1], "name": [2]}), data)
    install_response(monkeypatch, data.getvalue())
    with pytest.raises(ValueError, match="unique names"):
        preview.load_dataset("https://doip.example/doip/retrieve/Q1/a.parquet", "https://doip.example")


def test_empty_parquet_preserves_schema_and_returns_empty_page(monkeypatch):
    """Handle empty components without losing selectable schema columns."""
    data = BytesIO()
    pq.write_table(pa.table({"cases": pa.array([], type=pa.int64())}), data)
    install_response(monkeypatch, data.getvalue())
    loaded = preview.load_dataset("https://doip.example/doip/retrieve/Q1/a.parquet", "https://doip.example")
    result, total = preview.query_dataset(loaded, preview.PreviewOptions())
    assert result.empty and list(result.columns) == ["cases"] and total == 0
