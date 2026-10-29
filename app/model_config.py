"""Check a model-run config against the parameters the model declares in its model card.

The parameters are the card's ``episerve:parameters`` (a JSON Schema of ``config.json``), stored
by the sync flow in the catalog entry. The check is ``episerve_specs.check_model_config``, so it
gives the same result as anywhere else the card is used; checking here fails a bad run early
instead of after a Kubernetes job has been created.
"""

import logging

from episerve_specs import check_model_config, model_config_defaults

from app.clients import ckan as ckan_client

logger = logging.getLogger(__name__)


def config_problems(config: dict, parameters: dict) -> list[str]:
    """Return one message per violated rule (the parameter first, when there is one)."""
    return [f"{i.path}: {i.message}" if i.path else i.message for i in check_model_config(parameters, config)]


def config_template(parameters: dict | None) -> dict:
    """A config to start from: every declared parameter, with its default or null."""
    props = (parameters or {}).get("properties") or {}
    defaults = model_config_defaults(parameters or {})
    return {name: defaults.get(name) for name in props}


def model_params(model_image: str) -> dict | None:
    """The declared parameters of the catalog model with this image, or None if they cannot be determined.

    A catalog failure, an image the catalog does not know or a model without parameters in its card
    returns None: such a run is not blocked here and the model container does its own validation.
    """
    try:
        models = ckan_client.list_models()
    except Exception:
        logger.warning("Model catalog unavailable; skipping config validation", exc_info=True)
        return None
    image = model_image.rsplit(":", 1)[0] if ":" in model_image.rsplit("/", 1)[-1] else model_image
    for m in models:
        if m.get("docker_image") == image:
            return m.get("parameters")
    return None
