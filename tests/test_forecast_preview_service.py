"""Version, authorization, reconstruction, and endpoint tests for run previews."""

import json

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from app.forecast_preview import cache, service
from app.main import app
from tests.test_forecast_preview_data import parquet

COMMIT = 'a' * 64
SOURCE_COMMIT = 'b' * 64
SOURCE = f'https://doip.example/doip/retrieve/Q2/input.parquet?version={SOURCE_COMMIT}'


@pytest.fixture
def world(monkeypatch):
    """Supply a public run, versioned artifacts, and a controlled cache boundary.

    Args:
        monkeypatch: Fixture replacing database and upstream network access.

    Returns:
        Mutable recorded artifacts, access flags, and generated cache entries.
    """
    state = {'stored': {}, 'downloads': [], 'restricted': set(), 'fdo_missing': set()}
    state['package'] = {'extras': [], 'groups': [{'name': 'type-model-run'}]}
    state['workflow'] = {'input_data_files': [[SOURCE, 'input.parquet']],
                         'data_transformation_sql': ['SELECT day, value * 2 AS target FROM df']}
    state['config'] = {'history_length': 1}
    state['input'] = parquet(pd.DataFrame({'day': ['2026-01-01', '2026-01-02'], 'value': [2, 4]}))
    state['predictions'] = b'day\ttarget\ttarget_q10\ttarget_q90\n2026-01-03\t8\t7\t9\n'
    monkeypatch.setattr(service.settings, 'doip_url', 'https://doip.example')
    monkeypatch.setattr(service.ckan, '_get', lambda *a: state['package'])
    monkeypatch.setattr(service.doip, 'latest_version', lambda qid: COMMIT)

    def fdo(qid, version=None):
        """Return current or historical access metadata.

        Args:
            qid: Object identifier.
            version: Requested historical commit.

        Returns:
            Access metadata or a missing-object result.
        """
        if qid in state['fdo_missing']:
            return None
        return {'profile': {'accessRights': 'restricted' if qid in state['restricted'] else 'open'}}

    def download(url, base, limit):
        """Read only the fixture's pinned public artifact URLs.

        Args:
            url: Requested source or run component.
            base: Trusted service origin.
            limit: Download byte bound.

        Returns:
            Saved component bytes.
        """
        state['downloads'].append(url)
        if url == SOURCE:
            return state['input']
        assert f'version={COMMIT}' in url or 'version=' + 'c' * 64 in url
        if '/input/config_prefect.json?' in url:
            return json.dumps(state['workflow']).encode()
        if '/input/config.json?' in url:
            return json.dumps(state['config']).encode()
        assert '/output/predictions.tsv?' in url
        return state['predictions']

    def cached(qid, commit, build):
        """Simulate exact-version cache lookup after access checks.

        Args:
            qid: Run identifier.
            commit: Exact artifact version.
            build: Callback creating a cache miss result.

        Returns:
            Preview annotated with its cache-hit state.
        """
        key = (qid, commit, cache.FORMAT_VERSION)
        if key in state['stored']:
            return {**state['stored'][key], 'cached': True}
        result = build()
        if result['cacheable']:
            state['stored'][key] = result
        return {**result, 'cached': False}

    monkeypatch.setattr(service.doip, 'fetch_fdo', fdo)
    monkeypatch.setattr(service, 'download_preview_bytes', download)
    monkeypatch.setattr(cache, 'get_or_create', cached)
    return state


def test_preview_uses_all_supplied_rows_and_exact_versions(world):
    """Do not confuse the model's history parameter with the supplied input.

    Args:
        world: Public run fixtures with history_length one and two supplied rows.
    """
    result = service.preview('q1')
    assert result['artifact_commit'] == COMMIT and not result['cached']
    assert result['inputs'][0]['series'][0]['points'] == [
        {'x': '2026-01-01', 'value': 4.}, {'x': '2026-01-02', 'value': 8.}]
    assert result['predictions'][0]['points'][0]['q90'] == 9.
    assert 'selected or processed' in result['note']
    world['downloads'].clear()
    assert service.preview('Q1')['cached']
    assert SOURCE not in world['downloads']
    assert not any('/output/' in url for url in world['downloads'])
    assert not service.preview('Q1', 'c' * 64)['cached']
    assert len(world['stored']) == 2


@pytest.mark.parametrize('restricted', ['Q1', 'Q2'])
def test_restrictions_are_checked_even_after_preview_is_cached(world, restricted):
    """Refuse cached content after current source or run access changes.

    Args:
        world: Public run fixtures and cache.
        restricted: Object whose access is revoked after caching.
    """
    service.preview('Q1')
    world['restricted'].add(restricted)
    with pytest.raises(service.PreviewError) as failure:
        service.preview('Q1')
    assert failure.value.status == 403


def test_private_catalog_run_is_denied_before_any_cache_or_download(world):
    """Keep private catalog entries out of the public preview endpoint.

    Args:
        world: Public run fixture changed to private.
    """
    world['package']['private'] = True
    with pytest.raises(service.PreviewError) as failure:
        service.preview('Q1')
    assert failure.value.status == 403 and not world['downloads']


def test_missing_source_still_shows_predictions_and_is_not_cached(world):
    """Permit later retries after a source becomes available again.

    Args:
        world: Public run fixture with temporarily missing source metadata.
    """
    world['fdo_missing'].add('Q2')
    result = service.preview('Q1')
    assert result['predictions'] and not result['inputs'][0]['series']
    assert result['warnings'] and not result['cacheable'] and not world['stored']
    world['fdo_missing'].clear()
    assert service.preview('Q1')['inputs'][0]['series']


@pytest.mark.parametrize('source', [
    'https://elsewhere.example/doip/retrieve/Q2/input.parquet?version=' + SOURCE_COMMIT,
    'https://doip.example/doip/retrieve/Q2/input.parquet',
    'https://doip.example/doip/retrieve/Q2/input.parquet?version=main',
    'lakefs://model-runs/main/secret.parquet?version=' + SOURCE_COMMIT,
])
def test_untrusted_or_unpinned_sources_are_rejected(world, source):
    """Never fetch arbitrary hosts, branches, or privileged internal paths.

    Args:
        world: Public run fixture.
        source: Unsafe source URL from an untrusted saved configuration.
    """
    world['workflow']['input_data_files'][0][0] = source
    with pytest.raises(service.PreviewError):
        service.preview('Q1')
    assert source not in world['downloads']


def test_failed_run_without_predictions_remains_retryable(world):
    """Reject missing predictions without publishing a cache entry.

    Args:
        world: Public fixture whose output is invalid.
    """
    world['predictions'] = b''
    with pytest.raises(service.PreviewError, match='No readable predictions'):
        service.preview('Q1')
    assert not world['stored']


def test_ambiguous_predictions_have_a_table_fallback(world):
    """Return output values without selecting between two possible time columns.

    Args:
        world: Fixture containing ambiguous time columns.
    """
    world['predictions'] = b'start\tend\ttarget\n2026-01-03\t2026-01-04\t8\n'
    result = service.preview('Q1')
    assert result['prediction_table']['rows'] == [['2026-01-03', '2026-01-04', '8']]
    assert not result['predictions'] and result['warnings']


def test_internal_benchmark_path_requires_public_benchmark(world, monkeypatch):
    """Refuse internal data before reading it when its benchmark is private.

    Args:
        world: Run fixtures with an older internal source URL.
        monkeypatch: Fixture replacing the benchmark lookup.
    """
    from app.benchmarks import store
    world['workflow']['input_data_files'][0][0] = (
        'lakefs://benchmarks/main/2/input.parquet?version=' + SOURCE_COMMIT)
    monkeypatch.setattr(store, 'get', lambda bid: None)
    with pytest.raises(service.PreviewError) as failure:
        service.preview('Q1')
    assert failure.value.status == 403


def test_endpoint_returns_json_and_disables_http_caching(world):
    """Expose successful preview JSON without public browser caching.

    Args:
        world: Public fixtures and in-memory cache.
    """
    response = TestClient(app).get('/model-runs/Q1/forecast-preview')
    assert response.status_code == 200
    assert response.json()['input_label'] == 'Input supplied'
    assert response.headers['cache-control'] == 'no-store'


def test_endpoint_busy_response_has_retry_after(world, monkeypatch):
    """Allow clients to retry a preview being generated by another worker.

    Args:
        world: Public fixture.
        monkeypatch: Fixture simulating cross-process lock contention.
    """
    def busy(*args):
        """Raise the cache lock contention result.

        Args:
            *args: Unused cache arguments.

        Raises:
            cache.PreviewBusy: Preview is already being built.
        """
        raise cache.PreviewBusy('Preparing preview')
    monkeypatch.setattr(cache, 'get_or_create', busy)
    response = TestClient(app).get('/model-runs/Q1/forecast-preview')
    assert response.status_code == 503 and response.headers['retry-after'] == '3'


def test_benchmark_reference_uses_same_snapshot_without_input_sql(world, monkeypatch):
    """Load declared truth at the input commit and recheck access on cache hits.

    Args:
        world: Public run fixtures and in-memory cache.
        monkeypatch: Fixture replacing FDO metadata and reference downloads.
    """
    import hashlib
    reference = parquet(pd.DataFrame({'day': ['2026-01-03'], 'target': [11]}))
    original_fdo = service.doip.fetch_fdo
    original_download = service.download_preview_bytes
    url = SOURCE.replace('input.parquet', 'reference.parquet')

    def fdo(qid, version=None):
        """Declare reference data only in the pinned benchmark metadata.

        Args:
            qid: Requested object identifier.
            version: Requested immutable version.

        Returns:
            Public-access metadata with optional benchmark manifest.
        """
        result = original_fdo(qid, version)
        if qid == 'Q2' and version == SOURCE_COMMIT:
            result['profile']['episerveBenchmark'] = {'files': {
                'input': {'component': 'input.parquet'},
                'reference': {'component': 'reference.parquet', 'sha256': hashlib.sha256(reference).hexdigest()}}}
        return result

    def download(source, base, limit):
        """Serve reference data separately from the input fixture.

        Args:
            source: Pinned component URL.
            base: Configured DOIP origin.
            limit: Maximum download size.

        Returns:
            Original Parquet or run artifact bytes.
        """
        if source == url:
            world['downloads'].append(source)
            return world.get('reference_override', reference)
        return original_download(source, base, limit)

    monkeypatch.setattr(service.doip, 'fetch_fdo', fdo)
    monkeypatch.setattr(service, 'download_preview_bytes', download)
    result = service.preview('Q1')
    recorded = result['inputs'][0]['reference']
    assert recorded['url'] == url and recorded['commit'] == SOURCE_COMMIT
    assert recorded['series'][0]['points'] == [{'x': '2026-01-03', 'value': 11.}]
    assert result['inputs'][0]['series'][0]['points'][0]['value'] == 4.
    assert result['cacheable']
    world['downloads'].clear()
    assert service.preview('Q1')['cached']
    assert url not in world['downloads']
    world['stored'].clear()
    world['reference_override'] = b'invalid parquet and checksum'
    incomplete = service.preview('Q1')
    assert not incomplete['cacheable'] and incomplete['warnings']
    assert incomplete['predictions'] and incomplete['inputs'][0]['series']
    assert not incomplete['inputs'][0]['reference']['series']
    world['restricted'].add('Q2')
    with pytest.raises(service.PreviewError) as failure:
        service.preview('Q1')
    assert failure.value.status == 403


def test_ordinary_source_observations_reuse_pinned_download_and_cache(world):
    """Expand only the observation view while retaining the supplied history.

    Args:
        world: Public run fixtures and an in-memory preview cache.
    """
    world['input'] = parquet(pd.DataFrame({'day': ['2026-01-01', '2026-01-02', '2026-01-03'],
                                         'value': [2, 4, 6], 'region': ['Berlin'] * 3}))
    world['workflow']['data_transformation_sql'] = [
        "SELECT day, value * 2 AS target FROM df WHERE day < '2026-01-03' AND region = 'Berlin' ORDER BY day"]
    result = service.preview('Q1')
    supplied = result['inputs'][0]
    assert len(supplied['series'][0]['points']) == 2
    assert supplied['reference']['kind'] == 'source'
    assert supplied['reference']['url'] == SOURCE
    assert supplied['reference']['commit'] == SOURCE_COMMIT
    assert supplied['reference']['series'][0]['points'] == [{'x': '2026-01-03', 'value': 12.}]
    assert world['downloads'].count(SOURCE) == 1
    assert result['cacheable'] and not result['warnings']
    world['downloads'].clear()
    assert service.preview('Q1')['cached']
    assert SOURCE not in world['downloads']
    world['restricted'].add('Q2')
    with pytest.raises(service.PreviewError) as failure:
        service.preview('Q1')
    assert failure.value.status == 403


def test_ambiguous_observation_sql_keeps_input_and_predictions_cacheable(world):
    """Explain unsupported inference without breaking or repeatedly rebuilding charts.

    Args:
        world: Public run fixtures with valid but ambiguous input SQL.
    """
    world['workflow']['data_transformation_sql'] = [
        "SELECT day, value AS target FROM df WHERE day < '2026-01-03' OR value > 0"]
    result = service.preview('Q1')
    assert result['inputs'][0]['series'] and result['predictions']
    assert 'OR' in result['inputs'][0]['observation_note']
    assert 'reference' not in result['inputs'][0]
    assert result['cacheable'] and not result['warnings']


def test_ambiguous_axes_offer_cached_manual_columns_without_changing_input(world):
    """Treat readable multidimensional files as a supported manual preview.

    Args:
        world: Public run fixtures with ambiguous calendar axes.
    """
    world['predictions'] = b'week\tweek_start\ttarget\n2026-W01\t2025-12-29\t0\n'
    world['input'] = parquet(pd.DataFrame({'week': ['2026-W01', '2026-W01'],
                                         'week_start': ['2025-12-29'] * 2, 'value': [2, 4]}))
    world['workflow']['data_transformation_sql'] = []
    result = service.preview('Q1')
    assert result['cacheable'] and result['warnings']
    assert result['predictions'] == [] and result['inputs'][0]['series'] == []
    assert result['prediction_manual']['rows'] == [['2026-W01', '2025-12-29', 0.]]
    assert result['inputs'][0]['manual']['rows'] == [
        ['2026-W01', '2025-12-29', 2.], ['2026-W01', '2025-12-29', 4.]]
    assert result['inputs'][0]['manual']['columns'][0]['duplicate']
    world['downloads'].clear()
    assert service.preview('Q1')['cached']
    assert SOURCE not in world['downloads']
    world['restricted'].add('Q2')
    with pytest.raises(service.PreviewError):
        service.preview('Q1')
