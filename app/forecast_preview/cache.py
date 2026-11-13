"""MariaDB preview cache with cross-process generation locking."""

from collections.abc import Callable
import hashlib
import json

from app import db

FORMAT_VERSION = 4
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


class PreviewBusy(ValueError):
    """Another worker is generating this version of the preview."""


def get_or_create(qid: str, commit: str, build: Callable[[], dict]) -> dict:
    """Return a stored preview or generate it once under a database advisory lock.

    Args:
        qid: Validated run identifier.
        commit: Exact run artifact version.
        build: Builds a preview; incomplete previews set cacheable to False.

    Returns:
        Preview data with a cached flag indicating whether generation was skipped.

    Raises:
        PreviewBusy: Another worker holds the generation lock.
        ValueError: The generated payload exceeds the cache size limit.
        Exception: Storage or preview generation failed; no success is cached.
    """
    key = (qid, commit, FORMAT_VERSION)
    lock = 'forecast-preview:' + hashlib.sha256(repr(key).encode()).hexdigest()[:40]
    conn = db.connect()
    acquired = False
    try:
        with conn.cursor() as cur:
            payload = _read(cur, key)
            if payload is not None:
                return {**payload, 'cached': True}
            cur.execute('SELECT GET_LOCK(%s, 1) AS acquired', (lock,))
            acquired = (cur.fetchone() or {}).get('acquired') == 1
            if not acquired:
                raise PreviewBusy('This preview is being prepared. Please retry shortly.')
            # End the initial read snapshot so a previous worker's insert is visible.
            conn.commit()
            payload = _read(cur, key)
            if payload is not None:
                return {**payload, 'cached': True}
            payload = build()
            encoded = json.dumps(payload, allow_nan=False, separators=(',', ':'))
            if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
                raise ValueError('The chart data exceeds the preview size limit.')
            if payload.get('cacheable', True):
                cur.execute(
                    'INSERT INTO model_run_preview_cache (run_qid, artifact_commit, format_version, payload) '
                    'VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE payload = VALUES(payload), generated_at = CURRENT_TIMESTAMP',
                    (*key, encoded),
                )
                conn.commit()
            return {**payload, 'cached': False}
    finally:
        try:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))
        finally:
            conn.close()


def _read(cur, key: tuple) -> dict | None:
    """Read one exact cache entry.

    Args:
        cur: Open database cursor.
        key: Run identifier, artifact commit, and format version.

    Returns:
        Decoded preview, or None on a cache miss.
    """
    cur.execute(
        'SELECT payload FROM model_run_preview_cache WHERE run_qid = %s AND artifact_commit = %s AND format_version = %s', key)
    row = cur.fetchone()
    return json.loads(row['payload']) if row else None
