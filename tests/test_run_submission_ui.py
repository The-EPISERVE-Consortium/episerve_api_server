"""Regression coverage for authorization and configuration at UI submission time."""

import json
from types import SimpleNamespace

import pytest
from nicegui import ui

from app.clients import prefect
from app.ui import session, trigger_page
from app.ui.workflow import pages
from app.users import User
from tests.conftest import ADMIN
from tests.ui_harness import button, click, render

DATASET = {"qid": "Q1", "name": "Dataset", "data_path": "https://doip.example/input.parquet"}
MODEL = {
    "name": "Forecast", "docker_image": "example/forecast", "docker_tag": "latest",
    "parameters": {
        "type": "object", "required": ["history_length"],
        "properties": {"history_length": {"type": "integer", "minimum": 3}},
    },
}


def _render_submission(monkeypatch, path, config, after, *, parameters=True):
    """Render a ready-to-submit workflow with local dependencies.

    Args:
        monkeypatch: Fixture replacing external services and session lookup.
        path: Simple or full workflow route to render.
        config: Configuration to place in the form or stored workflow.
        after: Callback receiving the client, element finder, and mutable session.
        parameters: Whether the selected model declares a configuration schema.

    Returns:
        Requests sent to the fake Prefect service.
    """
    model = {**MODEL, "parameters": MODEL["parameters"] if parameters else None}
    identity = {"user": ADMIN}
    submitted = []
    state = {
        "datasets": [DATASET], "model": model, "config": json.dumps(config),
        "sql": {}, "filenames": {}, "benchmark": None,
    }

    def setup():
        """Replace the session and catalog without bypassing authorization checks."""
        monkeypatch.setattr(session, "current_user", lambda: identity["user"])
        monkeypatch.setattr(trigger_page.ckan_client, "list_processed_datasets", lambda: [DATASET])
        monkeypatch.setattr(trigger_page.ckan_client, "list_models", lambda: [model])
        monkeypatch.setattr(pages, "workflow_state", lambda: state)
        monkeypatch.setattr(prefect, "trigger_model_run", submit)
        monkeypatch.setattr(pages.settings, "prefect_api_url", "http://prefect.example")

    def submit(**kwargs):
        """Record a submitted request without contacting Prefect.

        Args:
            **kwargs: Model-run parameters passed by the UI.

        Returns:
            A successful flow-run response.
        """
        submitted.append(kwargs)
        return {"prefect_flow_run_id": "run1", "status": "SCHEDULED"}

    async def ready(out, client, find, drain):
        """Select inputs in the simple form, then execute the test action.

        Args:
            out: Harness response metadata.
            client: NiceGUI page client.
            find: Helper selecting page elements by type.
            drain: Helper running deferred callbacks.
        """
        assert out["status"] == 200
        if path == "/ui/trigger":
            for table in find(ui.table):
                table.selected = [model if table.props["row-key"] == "name" else DATASET]
            find(ui.codemirror)[-1].value = json.dumps(config)
            await click(button(find, "Pre-flight Check"), client)
        await after(client, find, identity)

    render(monkeypatch, SimpleNamespace(_is_logged_in=None), path, setup=setup, after=ready)
    return submitted


@pytest.mark.parametrize("path", ["/ui/trigger", "/ui/run_workflow/5"])
@pytest.mark.parametrize("revoked", [False, True], ids=["expired-session", "revoked-right"])
def test_submission_rechecks_current_authorization(monkeypatch, path, revoked):
    """Reject submission when authorization changes after the page was rendered.

    Args:
        monkeypatch: Fixture replacing dependencies.
        path: Workflow page under test.
        revoked: Whether the user remains logged in but loses run permissions.
    """
    destinations = []
    monkeypatch.setattr(ui.navigate, "to", lambda target: destinations.append(target))

    async def after(client, find, identity):
        """Expire the session or revoke rights before clicking the existing button.

        Args:
            client: Rendered NiceGUI client.
            find: Element lookup helper.
            identity: Mutable current-user fixture.
        """
        identity["user"] = User(ADMIN.id, ADMIN.email, ADMIN.name, frozenset()) if revoked else None
        await click(button(find, "Trigger Run"), client)

    submitted = _render_submission(monkeypatch, path, {"history_length": 10}, after)
    assert submitted == []
    assert destinations == ["/ui" if revoked else "/login"]


@pytest.mark.parametrize(
    "config,parameters,expected_error",
    [
        ({}, True, "'history_length' is a required property"),
        ({"history_length": 2}, True, "less than the minimum"),
        ({"history_length": "ten"}, True, "not of type"),
        ({"history_length": 10}, True, None),
        ({"custom": 10}, False, None),
    ],
)
def test_simple_submission_validates_model_parameters(monkeypatch, config, parameters, expected_error):
    """Apply model schema validation while preserving valid and schema-free runs.

    Args:
        monkeypatch: Fixture replacing dependencies.
        config: Configuration entered in the simple form.
        parameters: Whether a model schema is available.
        expected_error: Validation fragment expected, or None for a valid run.
    """
    notes = []
    monkeypatch.setattr(ui, "notify", lambda message, **kwargs: notes.append(message))

    async def after(client, find, identity):
        """Click the final submission button.

        Args:
            client: Rendered NiceGUI client.
            find: Element lookup helper.
            identity: Mutable current-user fixture.
        """
        await click(button(find, "Trigger Run"), client)

    submitted = _render_submission(monkeypatch, "/ui/trigger", config, after, parameters=parameters)
    if expected_error:
        assert submitted == []
        assert any(expected_error in note for note in notes)
    else:
        assert len(submitted) == 1
        assert json.loads(submitted[0]["config_json"]) == config
        assert submitted[0]["executed_by"] == f"episerve_user_{ADMIN.id}"
