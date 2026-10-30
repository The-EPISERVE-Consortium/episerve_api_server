import requests

# Enough for a few hundred rows of a typical CSV/TSV, without downloading a large file.
DEFAULT_PREVIEW_BYTES = 4 * 1024 * 1024


def fetch_text_head(url: str, max_bytes: int = DEFAULT_PREVIEW_BYTES, timeout: int = 60) -> str:
    """Fetch the start of a text component from the DOIP gateway.

    Asks the gateway for only the first ``max_bytes`` bytes (``?size=``), so a
    preview does not download the whole file. When the gateway cuts the file
    (HTTP 206), the incomplete last line is dropped so the text ends on a row
    boundary. A gateway that ignores ``size`` simply returns the whole file.

    Args:
        url: DOIP retrieve URL of the component (may already carry query parameters).
        max_bytes: Maximum number of bytes to fetch.
        timeout: Request timeout in seconds.

    Returns:
        str: The decoded text.

    Raises:
        requests.HTTPError: If the gateway answers with an error status.
    """
    resp = requests.get(url, params={"size": max_bytes}, timeout=timeout)
    resp.raise_for_status()
    data = resp.content
    if resp.status_code == 206:
        cut = data.rfind(b"\n")
        if cut != -1:
            data = data[: cut + 1]
    return data.decode("utf-8", errors="replace")


def fetch_fdo(qid: str, timeout: int = 20) -> dict | None:
    """The FDO record of an object from the DOIP server (``/doip/retrieve/<QID>``), or None when it does not exist.

    Raises:
        requests.RequestException: The DOIP server is unreachable or answers with another error.
    """
    from app.config import settings

    resp = requests.get(f"{settings.doip_url.rstrip('/')}/doip/retrieve/{qid}", timeout=timeout)
    if resp.status_code in (400, 404):
        return None
    resp.raise_for_status()
    body = resp.json()
    # The gateway answers an unknown QID with HTTP 200 and {"error": ..., "message": "No FDO metadata file found ..."}.
    if not isinstance(body, dict) or "error" in body or "kernel" not in body:
        return None
    return body


def latest_version(qid: str, timeout: int = 20) -> str | None:
    """The newest lakeFS commit of an object (``/doip/versions/<QID>?limit=1``), or None when it is not known."""
    from app.config import settings

    try:
        resp = requests.get(f"{settings.doip_url.rstrip('/')}/doip/versions/{qid}", params={"limit": 1},
                            timeout=timeout)
        if resp.status_code != 200:
            return None
        versions = resp.json().get("versions") or []
        return versions[0].get("commit_id") if versions else None
    except (requests.RequestException, ValueError, AttributeError):
        return None
