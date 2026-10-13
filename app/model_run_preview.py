"""Resolve model-run components and preview data without executing metadata SQL."""

from dataclasses import dataclass
from io import StringIO
import json
import re
from urllib.parse import quote, urlsplit

import pandas as pd

from app.dataset_preview import (
    PreviewOptions, download_preview_bytes, load_dataset, query_dataset,
    validate_component_url,
)

MAX_METADATA_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_COMPONENTS = 20
MAX_PREVIEW_ROWS = 500


@dataclass(frozen=True)
class RunComponent:
    """Describe a saved component or explicitly identified original input."""

    label: str
    url: str
    extension: str
    is_input: bool
    original_input: bool = False


def resolve_model_run(rows: list[dict], qid: str) -> dict:
    """Resolve a browser identifier against server-held model-run records.

    Args:
        rows: Catalog rows fetched by the server.
        qid: Identifier selected by the browser.

    Returns:
        A copy of the uniquely matching catalog record.

    Raises:
        ValueError: If the identifier is unknown, ambiguous, or unsafe.
    """
    if not isinstance(qid, str) or not re.fullmatch(r"Q[0-9]+", qid):
        raise ValueError("Select a known model run.")
    matches = [row for row in rows if row.get("qid") == qid]
    if len(matches) != 1:
        raise ValueError("Select a known model run.")
    return dict(matches[0])


def fetch_run_metadata(qid: str, doip_base: str) -> dict:
    """Fetch bounded metadata using a trusted catalog run identifier.

    Args:
        qid: Validated catalog model-run identifier.
        doip_base: Configured DOIP service base URL.

    Returns:
        The metadata JSON object.

    Raises:
        ValueError: If the identifier, destination, size, or JSON is invalid.
    """
    if not isinstance(qid, str) or not re.fullmatch(r"Q[0-9]+", qid):
        raise ValueError("Invalid model-run identifier.")
    url = f"{doip_base.rstrip('/')}/doip/retrieve/{qid}"
    metadata = json.loads(download_preview_bytes(url, doip_base, MAX_METADATA_BYTES))
    if not isinstance(metadata, dict):
        raise ValueError("Model-run metadata must be a JSON object.")
    return metadata


def _saved_component_url(qid: str, path: str, doip_base: str) -> str:
    """Build a retrieve URL from a relative saved input/output component path.

    Args:
        qid: Trusted catalog run identifier.
        path: Relative component path from metadata.
        doip_base: Configured DOIP service base URL.

    Returns:
        A validated, encoded component URL.

    Raises:
        ValueError: If the path escapes the input/output component namespace.
    """
    if not isinstance(path, str):
        raise ValueError("Invalid saved component path.")
    relative = path.removeprefix("components/")
    parts = relative.split("/")
    if (
        len(parts) < 2 or parts[0] not in ("input", "output")
        or any(part in ("", ".", "..") for part in parts)
        or any(char in relative for char in ("%", "\\", "?", "#"))
        or any(ord(char) < 32 or ord(char) == 127 for char in relative)
    ):
        raise ValueError("Invalid saved component path.")
    url = f"{doip_base.rstrip('/')}/doip/retrieve/{qid}/{quote(relative, safe='/')}"
    return validate_component_url(url, doip_base)


def find_run_components(metadata: dict, qid: str, doip_base: str) -> list[RunComponent]:
    """Prefer saved inputs and return validated preview components.

    Args:
        metadata: Untrusted metadata JSON from DOIP.
        qid: Run identifier from the server-held catalog, not metadata.
        doip_base: Configured DOIP service base URL.

    Returns:
        Inputs followed by outputs; originals are used only without saved inputs.

    Raises:
        ValueError: If metadata components contain unsafe paths or too many entries.
    """
    if not isinstance(qid, str) or not re.fullmatch(r"Q[0-9]+", qid):
        raise ValueError("Invalid model-run identifier.")
    kernel = metadata.get("kernel", {})
    if not isinstance(kernel, dict):
        raise ValueError("Invalid model-run kernel metadata.")
    components = kernel.get("fdo:hasComponent", [])
    if isinstance(components, dict):
        components = [components]
    if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
        raise ValueError("Too many or invalid model-run components.")
    inputs, outputs = [], []
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("Invalid component metadata.")
        cid = component.get("componentId", "")
        if not isinstance(cid, str) or cid == "input_annotated.parquet":
            continue
        extension = next((ext for ext in (".parquet", ".csv", ".tsv") if cid.lower().endswith(ext)), None)
        if extension is None:
            continue
        path = component.get("@id", f"output/{cid}")
        url = _saved_component_url(qid, path, doip_base)
        is_input = path.removeprefix("components/").startswith("input/")
        item = RunComponent(cid, url, extension, is_input)
        (inputs if is_input else outputs).append(item)
    if not inputs:
        provenance = metadata.get("provenance", {})
        if not isinstance(provenance, dict):
            raise ValueError("Invalid provenance metadata.")
        originals = provenance.get("prov:used", [])
        if not isinstance(originals, list) or len(originals) > MAX_COMPONENTS:
            raise ValueError("Too many or invalid original inputs.")
        for entry in originals:
            url = entry.get("@id", "") if isinstance(entry, dict) else entry
            if not isinstance(url, str):
                raise ValueError("Invalid original input URL.")
            filename = urlsplit(url).path.rsplit("/", 1)[-1]
            if not filename.lower().endswith(".parquet"):
                continue
            validate_component_url(url, doip_base)
            inputs.append(RunComponent(
                f"{filename} (original input; transformations not reapplied)", url, ".parquet", True, True,
            ))
    if len(inputs) + len(outputs) > MAX_COMPONENTS:
        raise ValueError("Too many model-run preview components.")
    return inputs + outputs


def fetch_component_preview(component: RunComponent, doip_base: str, limit: int = 500) -> pd.DataFrame:
    """Preview a component through bounded downloads and generated queries only.

    Args:
        component: Server-resolved preview component.
        doip_base: Configured DOIP service base URL.
        limit: Maximum displayed rows, between one and 500.

    Returns:
        Preview rows with source row count where available.

    Raises:
        ValueError: If the limit, component, destination, or data is unsuitable.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PREVIEW_ROWS:
        raise ValueError("Preview row limit must be between 1 and 500.")
    if component.extension == ".parquet":
        dataset = load_dataset(component.url, doip_base)
        frame, _ = query_dataset(dataset, PreviewOptions(page_size=500))
        frame = frame.head(limit).copy()
        frame.attrs["total_rows"] = dataset.source_rows
    elif component.extension in (".csv", ".tsv"):
        data = download_preview_bytes(component.url, doip_base, MAX_TEXT_BYTES, partial=True)
        frame = pd.read_csv(
            StringIO(data.decode("utf-8", errors="replace")),
            sep="\t" if component.extension == ".tsv" else ",", nrows=limit,
        )
        frame.attrs["total_rows"] = None
    else:
        raise ValueError("Unsupported component format.")
    if component.original_input:
        frame["x_auto_converted"] = range(1, len(frame) + 1)
    return frame
