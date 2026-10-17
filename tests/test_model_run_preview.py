"""Verify model-run preview trust boundaries without network access."""

from io import BytesIO
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import model_run_preview as preview
from tests.test_dataset_preview import install_response

BASE = "https://doip.example"


def test_saved_inputs_take_priority_and_metadata_cannot_change_run_id():
    """Use catalog run paths and saved inputs even with malicious provenance."""
    metadata = {
        "@id": "../../other-run",
        "kernel": {"fdo:hasComponent": [
            {"componentId": "saved.parquet", "@id": "components/input/saved.parquet"},
            {"componentId": "predictions.parquet", "@id": "components/output/predictions.parquet"},
        ]},
        "provenance": {"prov:used": [{"@id": "http://internal/data.parquet", "schema:query": "SELECT * FROM read_text('/etc/passwd')"}]},
    }
    components = preview.find_run_components(metadata, "Q1", BASE)
    assert [item.label for item in components] == ["saved.parquet", "predictions.parquet"]
    assert [item.url for item in components] == [
        BASE + "/doip/retrieve/Q1/input/saved.parquet",
        BASE + "/doip/retrieve/Q1/output/predictions.parquet",
    ]
    assert not any(item.original_input for item in components)


def test_original_input_is_untransformed_and_sql_is_never_executed(monkeypatch, tmp_path):
    """Read original rows without evaluating a file-writing provenance payload.

    Args:
        monkeypatch: Pytest patch fixture.
        tmp_path: Temporary directory used to detect unexpected writes.
    """
    target = tmp_path / "injected.csv"
    metadata = {"provenance": {"prov:used": [{
        "@id": BASE + "/doip/retrieve/Q2/data.parquet",
        "schema:query": f"COPY (SELECT 1) TO '{target}'",
    }]}}
    component = preview.find_run_components(metadata, "Q1", BASE)[0]
    data = BytesIO()
    pq.write_table(pa.table({"cases": [3, 1, 2]}), data)
    install_response(monkeypatch, data.getvalue())
    frame = preview.fetch_component_preview(component, BASE, limit=2)
    assert frame["cases"].tolist() == [1, 2]
    assert frame["x_auto_converted"].tolist() == [1, 2]
    assert frame.attrs["total_rows"] == 3
    assert component.original_input and "transformations not reapplied" in component.label
    assert not target.exists()


@pytest.mark.parametrize("path", [
    "components/input/../../admin.parquet", "components/input/%2e%2e/admin.parquet",
    "https://internal/data.parquet", "/output/a.parquet",
])
def test_saved_component_paths_cannot_escape(path):
    """Reject saved component paths that leave the approved namespace.

    Args:
        path: Malicious relative or absolute component path.
    """
    with pytest.raises(ValueError):
        preview.find_run_components({"kernel": {"fdo:hasComponent": [{
            "componentId": "data.parquet", "@id": path,
        }]}}, "Q1", BASE)


@pytest.mark.parametrize("url", [
    "http://internal/data.parquet", BASE + "/doip/retrieve/../secret.parquet",
])
def test_original_input_urls_are_validated(url):
    """Reject provenance URLs outside the configured DOIP retrieve boundary.

    Args:
        url: Unapproved original input URL.
    """
    with pytest.raises(ValueError):
        preview.find_run_components({"provenance": {"prov:used": [{"@id": url}]}}, "Q1", BASE)


def test_unknown_or_ambiguous_run_identifiers_are_rejected():
    """Resolve only unique, valid catalog run identifiers."""
    rows = [{"qid": "Q1", "doip_url": "https://catalog-url"}]
    assert preview.resolve_model_run(rows, "Q1") == rows[0]
    for qid in ("Q2", "Q1/../Q2", "Q1?url=http://internal", None):
        with pytest.raises(ValueError):
            preview.resolve_model_run(rows, qid)
    with pytest.raises(ValueError):
        preview.resolve_model_run(rows * 2, "Q1")


@pytest.mark.parametrize("limit", [0, 100_000])
def test_component_row_limit_is_checked_before_loading(limit):
    """Reject forged row counts before fetching any component data.

    Args:
        limit: Invalid row count.
    """
    component = preview.RunComponent("data.parquet", BASE + "/doip/retrieve/Q1/output/data.parquet", ".parquet", False)
    with pytest.raises(ValueError):
        preview.fetch_component_preview(component, BASE, limit)


def test_metadata_uses_bounded_downloads_without_redirects(monkeypatch):
    """Load JSON metadata through the shared restricted downloader.

    Args:
        monkeypatch: Pytest patch fixture.
    """
    calls = install_response(monkeypatch, json.dumps({"kernel": {}}).encode())
    assert preview.fetch_run_metadata("Q1", BASE) == {"kernel": {}}
    assert calls == [{"stream": True, "allow_redirects": False, "timeout": (5, 30)}]
    install_response(monkeypatch, b"", status_code=302)
    with pytest.raises(ValueError):
        preview.fetch_run_metadata("Q1", BASE)
    install_response(monkeypatch, b"12345")
    monkeypatch.setattr(preview, "MAX_METADATA_BYTES", 4)
    with pytest.raises(ValueError, match="download limit"):
        preview.fetch_run_metadata("Q1", BASE)
