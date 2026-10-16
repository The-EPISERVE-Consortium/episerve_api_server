"""Read the inputs a model run actually used from its DOIP provenance."""

import re
from dataclasses import dataclass

from app.model_run_preview import fetch_run_metadata

_URL = re.compile(r"https?://[^?#\s]*/doip/retrieve/(Q[0-9]+)/([^/?#\s]+)(?:\?version=([0-9A-Fa-f]+))?")


@dataclass(frozen=True)
class RunInput:
    """One input of a model run: dataset, file, the lakeFS commit it was read at, and the SQL applied."""

    dataset_qid: str
    filename: str
    commit: str | None
    sql: str | None


def parse_run_inputs(metadata) -> list[RunInput]:
    """Extract the inputs from a run's FDO metadata (``provenance`` → ``prov:used``).

    Entries that are not DOIP retrieve URLs are skipped; a malformed structure gives an empty list.
    """
    provenance = metadata.get("provenance") if isinstance(metadata, dict) else None
    used = provenance.get("prov:used") if isinstance(provenance, dict) else None
    if isinstance(used, (dict, str)):
        used = [used]
    if not isinstance(used, list):
        return []
    inputs = []
    for entry in used:
        url = entry.get("@id") if isinstance(entry, dict) else entry
        sql = entry.get("schema:query") if isinstance(entry, dict) else None
        match = _URL.fullmatch(url) if isinstance(url, str) else None
        if match:
            commit = match.group(3).lower() if match.group(3) else None
            inputs.append(RunInput(match.group(1), match.group(2), commit, sql if isinstance(sql, str) else None))
    return inputs


def fetch_run_inputs(qid: str, doip_base: str) -> list[RunInput]:
    """Download the run's metadata from DOIP (bounded, validated) and parse its inputs.

    Raises:
        ValueError / requests.RequestException: If the metadata cannot be fetched.
    """
    return parse_run_inputs(fetch_run_metadata(qid, doip_base))
