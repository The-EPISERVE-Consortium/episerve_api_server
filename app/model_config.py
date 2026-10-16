"""Check a model-run config against the parameters the model declares in its catalog entry.

The parameters are the FDO ``additionalProperty`` list (``name``, ``valueRequired``,
``minValue``, ``maxValue``); the model container validates the same rules at start-up, so
checking here only fails a bad run early instead of after a Kubernetes job has been created.
"""

import logging

from app.clients import ckan as ckan_client

logger = logging.getLogger(__name__)


def config_problems(config: dict, params: list[dict]) -> list[str]:
    """Return one message per violated rule; unknown keys are ignored (the model only warns)."""
    problems = []
    for p in params:
        name = p.get("name")
        value = config.get(name)
        if value is None:
            if p.get("valueRequired"):
                problems.append(f"missing required parameter '{name}'")
            continue
        low, high = p.get("minValue"), p.get("maxValue")
        if low is None and high is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"parameter '{name}' must be a number, got {value!r}")
        elif low is not None and value < low:
            problems.append(f"parameter '{name}' is {value}, below the minimum {low}")
        elif high is not None and value > high:
            problems.append(f"parameter '{name}' is {value}, above the maximum {high}")
    return problems


def model_params(model_image: str) -> list[dict] | None:
    """Declared parameters of the catalog model with this image, or None if it cannot be determined.

    A catalog failure or an image the catalog does not know returns None: such a run is not
    blocked here and the model container does its own validation.
    """
    try:
        models = ckan_client.list_models()
    except Exception:
        logger.warning("Model catalog unavailable; skipping config validation", exc_info=True)
        return None
    image = model_image.rsplit(":", 1)[0] if ":" in model_image.rsplit("/", 1)[-1] else model_image
    for m in models:
        if m.get("docker_image") == image:
            return m.get("additional_properties") or []
    return None
