"""Authorize public artifacts, resolve versions, and build generic forecast previews."""

import hashlib
import json
import logging
import re
from urllib.parse import parse_qs, unquote, urlsplit

import requests

from app.clients import ckan, doip
from app.config import settings
from app.dataset_preview import download_preview_bytes, validate_component_url
from app.forecast_preview import cache, data, observations

logger = logging.getLogger(__name__)
QID = re.compile(r'Q[0-9]{1,30}')
COMMIT = re.compile(r'[0-9a-f]{64}')
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024


class PreviewError(ValueError):
    """A user-facing preview failure with its HTTP status."""

    def __init__(self, message: str, status: int = 422):
        """Initialize a bounded public error message.

        Args:
            message: Safe explanation to return to the visitor.
            status: HTTP response status.
        """
        super().__init__(message)
        self.status = status


def _public_fdo(qid: str, version: str | None = None) -> bool:
    """Check current and historical DOIP access metadata without service credentials.

    Args:
        qid: Validated platform object identifier.
        version: Optional pinned historical version.

    Returns:
        Whether the requested object metadata exists.

    Raises:
        PreviewError: The object is restricted.
    """
    fdo = doip.fetch_fdo(qid, version=version)
    if fdo and (fdo.get('profile') or {}).get('accessRights') == 'restricted':
        raise PreviewError('Preview unavailable: the run or one of its inputs is restricted.', 403)
    return fdo is not None


def _artifact(qid: str, commit: str, component: str) -> bytes:
    """Read an existing run component at one exact artifact version.

    Args:
        qid: Validated public run identifier.
        commit: Resolved immutable artifact version.
        component: Server-defined component path.

    Returns:
        Bounded component bytes without redirects or credentials.
    """
    url = f'{settings.doip_url.rstrip("/")}/doip/retrieve/{qid}/{component}?version={commit}'
    return download_preview_bytes(url, settings.doip_url, MAX_ARTIFACT_BYTES)


def _source(url: str, filename: str, sql: str | None) -> dict:
    """Validate a saved input reference without fetching its data.

    Args:
        url: Source reference from the saved workflow configuration.
        filename: Destination filename originally supplied to the model.
        sql: Recorded workflow transformation, or None.

    Returns:
        A source descriptor containing immutable version and retrieval details.

    Raises:
        PreviewError: The source is unpinned or uses an unsupported location.
    """
    if not isinstance(url, str) or not isinstance(filename, str) or (sql is not None and not isinstance(sql, str)):
        raise PreviewError('The saved input configuration is invalid.')
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    versions = query.get('version', [])
    if len(versions) != 1 or not COMMIT.fullmatch(versions[0]) or set(query) != {'version'}:
        raise PreviewError('An input has no unambiguous pinned source version.')
    if parts.scheme == 'lakefs':
        # Older benchmarks used numeric directories instead of generated QID objects.
        match = re.fullmatch(r'/[^/]+/([0-9]+)/input\.parquet', parts.path)
        if parts.netloc != settings.lakefs_benchmarks_repo or not match or parts.fragment:
            raise PreviewError('This internal input location cannot be previewed.')
        return {'url': url, 'filename': filename, 'sql': sql, 'commit': versions[0],
                'benchmark_id': int(match[1]), 'path': f'{match[1]}/input.parquet'}
    try:
        validate_component_url(url, settings.doip_url)
    except ValueError as exc:
        raise PreviewError('An input is outside the configured data service.') from exc
    prefix = urlsplit(settings.doip_url).path.rstrip('/') + '/doip/retrieve/'
    relative = unquote(parts.path)[len(prefix):]
    qid, _, component = relative.partition('/')
    if not QID.fullmatch(qid) or not component or not component.lower().endswith('.parquet'):
        raise PreviewError('This source is not a supported Parquet component.')
    return {'url': url, 'filename': filename, 'sql': sql, 'qid': qid, 'component': component,
            'commit': versions[0], 'catalog': f'https://data.episerve.zib.de/dataset/{qid.lower()}?version={versions[0]}'}


def _reference_source(source: dict) -> dict | None:
    """Find benchmark observations declared beside the exact supplied component.

    Args:
        source: Validated input descriptor at an immutable object version.

    Returns:
        Reference descriptor at the same commit, or None for ordinary inputs.
    """
    if 'qid' not in source or not source['available']:
        return None
    fdo = doip.fetch_fdo(source['qid'], version=source['commit']) or {}
    files = ((fdo.get('profile') or {}).get('episerveBenchmark') or {}).get('files') or {}
    supplied, reference = files.get('input') or {}, files.get('reference') or {}
    if supplied.get('component') != source['component'] or reference.get('component') != 'reference.parquet':
        return None
    url = (f'{settings.doip_url.rstrip("/")}/doip/retrieve/{source["qid"]}/reference.parquet'
           f'?version={source["commit"]}')
    return {**_source(url, 'reference.parquet', None), 'sha256': reference.get('sha256')}


def _authorize(source: dict) -> bool:
    """Check present access before any cache read or privileged source retrieval.

    Args:
        source: Validated source descriptor.

    Returns:
        Whether the source remains verifiably public and readable.

    Raises:
        PreviewError: Source access is restricted or its benchmark is not public.
    """
    if 'benchmark_id' in source:
        from app.benchmarks import store
        row = store.get(source['benchmark_id'])
        if not row or not store.is_public(row):
            raise PreviewError('The source benchmark is not public.', 403)
        return True
    current = _public_fdo(source['qid'])
    pinned = _public_fdo(source['qid'], source['commit'])
    return current and pinned


def _read_source(source: dict) -> bytes:
    """Read an authorized source through DOIP or its historical benchmark path.

    Args:
        source: Validated and freshly authorized source descriptor.

    Returns:
        Size-bounded original Parquet bytes.

    Raises:
        ValueError: The source exceeds the download limit.
    """
    if 'benchmark_id' not in source:
        return download_preview_bytes(source['url'], settings.doip_url, MAX_SOURCE_BYTES)
    import lakefs
    from app.clients.lakefs import _client
    repo = lakefs.Repository(settings.lakefs_benchmarks_repo, client=_client())
    content = repo.ref(source['commit']).object(source['path']).reader().read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError('Input exceeds the preview download limit.')
    return content


def _build(qid: str, commit: str, sources: list[dict], config: dict) -> dict:
    """Construct supplied-input and recorded-output series without model rules.

    Args:
        qid: Public run identifier.
        commit: Exact run artifact commit.
        sources: Validated input descriptors with current availability checks.
        config: Saved model configuration, displayed without interpretation.

    Returns:
        Bounded chart JSON; incomplete history is retryable and not cached.
    """
    try:
        output = data.predictions_frame(_artifact(qid, commit, 'output/predictions.tsv'))
    except Exception as exc:
        raise PreviewError('No readable predictions are available for this run version.') from exc
    inputs, warnings = [], []
    try:
        predictions = data.series(output, forecast=True)
    except ValueError:
        predictions = []
        warnings.append('Prediction columns could not be mapped to a chart. The recorded output is shown as a table.')
    for source in sources:
        description = {key: value for key, value in source.items() if key not in ('path', 'available', 'reference')}
        content = None
        try:
            if not source['available']:
                raise ValueError('The source version is unavailable.')
            content = _read_source(source)
            frame = data.input_frame(content, source['sql'])
            description['table'] = data.table(frame)
            description['series'] = data.series(frame, forecast=False)
        except Exception:
            logger.warning('Could not reconstruct supplied input for %s', qid, exc_info=True)
            description['series'] = []
            warnings.append(f'Input supplied as {source["filename"]} could not be reconstructed. Predictions are still available.')
        reference = source.get('reference')
        if reference:
            recorded = {key: value for key, value in reference.items() if key != 'sql'}
            try:
                content = _read_source(reference)
                if reference.get('sha256') and hashlib.sha256(content).hexdigest() != reference['sha256']:
                    raise ValueError('Benchmark reference checksum mismatch.')
                # Reference data are observations, never model inputs: do not replay input SQL.
                frame = data.input_frame(content, None)
                recorded['table'] = data.table(frame)
                recorded['series'] = data.series(frame, forecast=False)
            except Exception:
                logger.warning('Could not read benchmark reference for %s', qid, exc_info=True)
                recorded['series'] = []
                warnings.append('Benchmark observations could not be loaded. Inputs and predictions are still available.')
            description['reference'] = recorded
        elif content is not None and description['series']:
            try:
                recorded = observations.reference(content, source['sql'], predictions)
                if recorded:
                    description['reference'] = {**{key: source[key] for key in (
                        'url', 'filename', 'commit', 'catalog') if key in source}, **recorded}
            except ValueError as exc:
                description['observation_note'] = f'Observed values from the pinned source are unavailable: {exc}'
            except Exception:
                logger.warning('Could not reconstruct source observations for %s', qid, exc_info=True)
                warnings.append('Observed values could not be loaded. Inputs and predictions are still available.')
        inputs.append(description)
    return {'run_qid': qid, 'artifact_commit': commit, 'format_version': cache.FORMAT_VERSION,
            'input_label': 'Input supplied', 'forecast_label': 'Recorded predictions',
            'note': 'The model may have selected or processed these inputs internally.',
            'config': config, 'inputs': inputs, 'predictions': predictions, 'prediction_table': data.table(output), 'warnings': warnings,
            'cacheable': not warnings}


def preview(qid: str, version: str | None = None) -> dict:
    """Resolve a public run and fetch or populate its immutable preview cache.

    Args:
        qid: Run QID from the request.
        version: Optional exact run artifact commit; otherwise resolve its newest version.

    Returns:
        Chart data for supplied inputs and recorded predictions.

    Raises:
        PreviewError: Invalid identifier, missing artifacts, or denied access.
        cache.PreviewBusy: Another process is building the same preview.
        Exception: Upstream services or the database are unavailable.
    """
    qid = qid.upper()
    if not QID.fullmatch(qid) or (version is not None and not COMMIT.fullmatch(version)):
        raise PreviewError('Use a valid run QID and full artifact commit.', 400)
    try:
        package = ckan._get('package_show', {'id': qid.lower()})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (403, 404):
            raise PreviewError('Model run not found.', 404) from exc
        raise
    extras = {e['key']: e['value'] for e in package.get('extras', [])}
    if package.get('private') or extras.get('access') == 'restricted':
        raise PreviewError('This run is not publicly accessible.', 403)
    if extras.get('dataset_type') != 'model-run' and not any(
            group.get('name') == 'type-model-run' for group in package.get('groups', [])):
        raise PreviewError('Model run not found.', 404)
    if not _public_fdo(qid):
        raise PreviewError('Run metadata is unavailable.', 404)
    commit = version or doip.latest_version(qid)
    if not commit or not COMMIT.fullmatch(commit) or not _public_fdo(qid, commit):
        raise PreviewError('Run artifact version not found.', 404)
    try:
        workflow = json.loads(_artifact(qid, commit, 'input/config_prefect.json'))
        config = json.loads(_artifact(qid, commit, 'input/config.json'))
    except Exception as exc:
        raise PreviewError('The saved run configuration could not be read.') from exc
    if not isinstance(workflow, dict) or not isinstance(config, dict):
        raise PreviewError('The saved run configuration is invalid.')
    files = workflow.get('input_data_files')
    queries = workflow.get('data_transformation_sql') or []
    if (not isinstance(files, list) or not 1 <= len(files) <= 4 or not isinstance(queries, list)
            or len(queries) not in (0, len(files))):
        raise PreviewError('The saved input list cannot be previewed.')
    sources = []
    for i, item in enumerate(files):
        if not isinstance(item, list) or len(item) != 2:
            raise PreviewError('The saved input list is invalid.')
        source = _source(item[0], item[1], queries[i] if queries else None)
        source['available'] = _authorize(source)
        source['reference'] = _reference_source(source)
        sources.append(source)
    # Never serve an earlier cached input after its public metadata disappeared.
    if not all(source['available'] for source in sources):
        return {**_build(qid, commit, sources, config), 'cached': False}
    return cache.get_or_create(qid, commit, lambda: _build(qid, commit, sources, config))
