"""Exercise pinned submissions, provenance parsing and API responses."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.clients import lakefs, prefect, ckan
from app.main import app
from app.model_snapshot import run_model_snapshot, image_repository
from app.routers import model_runs
from tests.conftest import ADMIN
from tests.fakes import bearer

IMAGE = 'ghcr.io/the-episerve-consortium/model__prediction__generic__timesfm'
DIGEST = 'sha256:' + 'a' * 64
SOURCE = 'b' * 40
BODY = {'model_image': IMAGE, 'input_data_files': [], 'config': {}}


def fdo():
    """Build an FDO matching the current model runner.

    Returns:
        Run metadata with a digest-pinned software agent.
    """
    return {'kernel': {}, 'profile': {'url': IMAGE}, 'provenance': {'prov:wasAssociatedWith': {
        '@id': f'{IMAGE}@{DIGEST}', 'schema:softwareVersion': DIGEST,
        'schema:additionalProperty': [
            {'schema:name': key, 'schema:value': value}
            for key, value in [('model_tag', 'v1'), ('model_digest', DIGEST), ('model_source_revision', SOURCE)]
        ],
    }}}


@pytest.mark.parametrize('embedded', [False, True])
def test_pinned_submission_preserves_identity_and_skips_current_card(monkeypatch, embedded):
    """Historical runs are validated by their container rather than today's card.

    Args:
        monkeypatch: Fixture restoring client substitutions.
        embedded: Whether the digest is embedded or supplied as a separate field.
    """
    calls = []
    monkeypatch.setattr(model_runs.prefect_client, 'trigger_model_run', lambda **kw: calls.append(kw) or {
        'prefect_flow_run_id': 'run', 'status': 'SCHEDULED'})
    monkeypatch.setattr(ckan, 'list_models', lambda: (_ for _ in ()).throw(AssertionError('No current card')))
    body = {**BODY, 'model_image': f'{IMAGE}@{DIGEST}'} if embedded else {**BODY, 'model_digest': DIGEST}
    response = TestClient(app).post('/model-runs', json=body, headers=bearer(ADMIN))
    assert response.status_code == 202
    assert calls[0]['model_image'] == body['model_image']
    assert calls[0]['model_digest'] == (None if embedded else DIGEST)
    assert calls[0]['executed_by'] == 'episerve_user_1'


@pytest.mark.parametrize('fields', [
    {'model_digest': 'latest'}, {'model_digest': ''}, {'model_image': IMAGE + '@sha256:abc'},
    {'model_image': IMAGE + '@' + DIGEST, 'model_digest': 'sha256:' + 'c' * 64},
])
def test_invalid_digests_are_rejected_before_scheduling(monkeypatch, fields):
    """Invalid or conflicting pins cannot silently fall back to latest.

    Args:
        monkeypatch: Fixture restoring the scheduler stub.
        fields: Invalid snapshot fields.
    """
    monkeypatch.setattr(model_runs.prefect_client, 'trigger_model_run',
                        lambda **kw: (_ for _ in ()).throw(AssertionError('Must not schedule')))
    response = TestClient(app).post('/model-runs', json={**BODY, **fields}, headers=bearer(ADMIN))
    assert response.status_code == 422


def test_prefect_payload_forwards_digest(monkeypatch):
    """Transmit the exact image digest rather than dropping it from the payload.

    Args:
        monkeypatch: Fixture restoring HTTP and deployment stubs.
    """
    calls = []
    monkeypatch.setattr(prefect, '_deployment_id', lambda name: 'deployment')
    monkeypatch.setattr(prefect.httpx, 'post', lambda url, **kw: calls.append(kw) or SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'id': 'run'}))
    prefect.trigger_model_run([], IMAGE, '{}', model_digest=DIGEST)
    assert json.loads(calls[0]['content'])['parameters']['model_digest'] == DIGEST
    prefect.trigger_model_run([], IMAGE, '{}')
    assert 'model_digest' not in json.loads(calls[1]['content'])['parameters']


def test_lakefs_reads_current_snapshot_and_keeps_the_requested_tag():
    """A SHA-256 digest is distinct from the requested Docker tag."""
    parsed = lakefs._parse_run_fdo('Q1', fdo())
    assert parsed['model_image'] == IMAGE
    assert parsed['docker_tag'] == 'v1'
    assert parsed['model_digest'] == DIGEST
    assert parsed['model_source_revision'] == SOURCE


@pytest.mark.parametrize('predicate', ['prov:wasAssociatedWith', 'prov:wasAttributedTo'])
def test_legacy_provenance_stays_readable(predicate):
    """Old tagged runs have no invented immutable image or source identifiers.

    Args:
        predicate: Legacy software attribution field.
    """
    agent = {'@id': IMAGE + ':latest'} if predicate == 'prov:wasAssociatedWith' else IMAGE + ':latest'
    snapshot = run_model_snapshot({'provenance': {predicate: agent}})
    assert snapshot['docker_tag'] == 'latest'
    assert snapshot['model_digest'] is None
    assert snapshot['model_source_revision'] is None


def test_missing_source_does_not_lose_the_image_digest():
    """An unlabeled image still retains its executable identity."""
    record = fdo()
    record['provenance']['prov:wasAssociatedWith']['schema:additionalProperty'].pop()
    snapshot = run_model_snapshot(record)
    assert snapshot['model_digest'] == DIGEST
    assert snapshot['model_source_revision'] is None


def test_repository_normalization_distinguishes_digest_colons_and_registry_ports():
    """Remove the version without corrupting a repository address."""
    assert image_repository(IMAGE + '@' + DIGEST) == IMAGE
    assert image_repository('registry.example:5000/project/model:v1') == 'registry.example:5000/project/model'


def test_detail_response_reads_authoritative_snapshot_from_doip(monkeypatch):
    """Detail responses expose provenance before the CKAN sync indexes it.

    Args:
        monkeypatch: Fixture restoring catalog and DOIP clients.
    """
    row = lakefs._parse_run_fdo('Q1', {})
    row['docker_tag'] = 'old-catalog-value'
    monkeypatch.setattr(ckan, 'list_model_runs', lambda: [row])
    monkeypatch.setattr(model_runs.doip_client, 'fetch_fdo', lambda qid: fdo())
    response = TestClient(app).get('/model-runs/Q1')
    assert response.status_code == 200
    data = response.json()
    assert data['model_digest'] == DIGEST
    assert data['model_source_revision'] == SOURCE
    assert data['docker_tag'] == 'v1'


def test_run_listing_preserves_indexed_snapshot_fields(monkeypatch):
    """The catalog client and response schema retain indexed snapshot properties.

    Args:
        monkeypatch: Fixture restoring the catalog HTTP helper.
    """
    package = {'extras': [{'key': key, 'value': value} for key, value in (
        ('qid', 'Q1'), ('model_image', IMAGE), ('model_digest', DIGEST), ('model_source_revision', SOURCE))]}
    monkeypatch.setattr(ckan, '_get', lambda action, params: {'results': [package]})
    response = TestClient(app).get('/model-runs')
    assert response.status_code == 200
    assert response.json()[0]['model_digest'] == DIGEST
    assert response.json()[0]['model_source_revision'] == SOURCE


def test_lineage_carries_the_recorded_image_and_source():
    """OpenLineage retains both immutable execution and source identities."""
    from app.lineage import event_for

    record = fdo()
    record['provenance']['@type'] = 'prov:Activity'
    event, _ = event_for('Q1', record)
    facet = event['run']['facets']['episerve']
    assert facet['model_image'] == IMAGE + '@' + DIGEST
    assert facet['model_digest'] == DIGEST
    assert facet['model_source_revision'] == SOURCE


def test_unavailable_provenance_is_not_presented_as_verified_metadata(monkeypatch):
    """A detail request reports an upstream outage rather than guessing pins.

    Args:
        monkeypatch: Fixture restoring upstream client stubs.
    """
    monkeypatch.setattr(ckan, 'list_model_runs', lambda: [lakefs._parse_run_fdo('Q1', {})])
    monkeypatch.setattr(model_runs.doip_client, 'fetch_fdo', lambda qid: (_ for _ in ()).throw(RuntimeError('Down')))
    response = TestClient(app).get('/model-runs/Q1')
    assert response.status_code == 502
    assert response.json()['detail'] == 'Run provenance is unavailable'
