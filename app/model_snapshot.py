"""Read model image identities from current and legacy run provenance."""

import re

DIGEST_PATTERN = r'sha256:[0-9a-f]{64}'


def image_repository(reference: str) -> str:
    """Remove an image digest or tag without confusing a registry port.

    Args:
        reference: Container image reference.

    Returns:
        Image repository without its tag or digest.
    """
    image = reference.strip().split('@', 1)[0]
    return image.rsplit(':', 1)[0] if ':' in image.rsplit('/', 1)[-1] else image


def run_model_snapshot(fdo: dict) -> dict:
    """Extract the recorded model snapshot without resolving moving tags.

    Args:
        fdo: Run FDO containing modern or legacy software-agent provenance.

    Returns:
        Model repository, requested tag, image digest and source Git revision.
        Unknown snapshot fields are None; a digest is never treated as a tag.
    """
    provenance = fdo.get('provenance') or {}
    agent = provenance.get('prov:wasAssociatedWith') or provenance.get('prov:wasAttributedTo') or {}
    if isinstance(agent, str):
        agent = {'@id': agent}
    if not isinstance(agent, dict):
        agent = {}
    reference = agent.get('@id') or ''
    reference = reference if isinstance(reference, str) else ''
    properties = agent.get('schema:additionalProperty') or []
    if isinstance(properties, dict):
        properties = [properties]
    values = {}
    for prop in properties if isinstance(properties, list) else []:
        if not isinstance(prop, dict):
            continue
        name, value = prop.get('schema:name'), prop.get('schema:value')
        if isinstance(name, str) and isinstance(value, str):
            values[name] = value if name not in values or values[name] == value else None
    digest = reference.split('@', 1)[1] if '@' in reference else None
    if not isinstance(digest, str) or not re.fullmatch(DIGEST_PATTERN, digest):
        digest = None
    if 'model_digest' in values and values['model_digest'] != digest:
        digest = None
    tag = values.get('model_tag')
    if tag is None and '@' not in reference and ':' in reference.rsplit('/', 1)[-1]:
        tag = reference.rsplit(':', 1)[1]
    source = values.get('model_source_revision')
    if not digest or not isinstance(source, str) or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', source):
        source = None
    profile = fdo.get('profile') or {}
    image = image_repository(reference) or profile.get('url') or ''
    return {'model_image': image, 'docker_tag': tag or '',
            'model_digest': digest, 'model_source_revision': source}
