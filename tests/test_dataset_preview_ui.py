"""Exercise anonymous dataset preview callbacks entirely in memory."""

import asyncio
import re
from contextlib import nullcontext

import httpx
import pyarrow as pa
import pytest
from nicegui import Client, background_tasks, events, ui

from app.dataset_preview import LoadedDataset
from app.main import app
from app.ui import pages


@pytest.mark.parametrize("scheduler_style", ["wrapped"])
def test_anonymous_preview_resolves_selection_and_applies_controls(monkeypatch, scheduler_style):
    """Exercise loading, filtering, pagination, and forged selection handling.

    Args:
        monkeypatch: Pytest patch fixture used to replace catalog and downloads.
        scheduler_style: NiceGUI callback scheduling API to exercise.
    """
    trusted_url = "https://doip.example/doip/retrieve/Q1/data.parquet"
    catalog = [{
        "qid": "Q1", "name": "Dataset", "components": [{"name": "data.parquet", "url": trusted_url}],
    }]
    downloads = []

    def load(url, base):
        """Supply sample data without making network requests.

        Args:
            url: Server-resolved component URL.
            base: Configured DOIP service.

        Returns:
            An in-memory sample with more than one result page.
        """
        downloads.append(url)
        return LoadedDataset(pa.table({"cases": list(range(120))}), source_rows=200)

    def catalog_rows():
        """Return trusted catalog records.

        Returns:
            The test catalog.
        """
        return catalog

    monkeypatch.setattr(pages.ckan_client, "list_processed_datasets", catalog_rows)
    monkeypatch.setattr(pages, "load_dataset", load)

    async def scenario():
        """Invoke page callbacks with a local ASGI client and forged event data."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get("/ui/datasets")
            assert response.status_code == 200
            client_id = re.search(r"'client_id': '([0-9a-f-]+)'", response.text).group(1)
            client = Client.instances[client_id]

            def elements(kind):
                """Find page elements by type.

                Args:
                    kind: NiceGUI element class.

                Returns:
                    Matching elements belonging to this page.
                """
                return [element for element in client.elements.values() if isinstance(element, kind)]

            async def trigger(element, event, args=None):
                """Await a registered callback in its page context.

                Args:
                    element: Event target.
                    event: Event name.
                    args: Browser event payload.
                """
                listeners = [listener for listener in element._event_listeners.values() if listener.type == event]
                pending = []

                async def run_callback(awaitable, context):
                    """Run a scheduled callback in its supplied UI context.

                    Args:
                        awaitable: Callback returned by a page event handler.
                        context: Parent slot supplied by newer NiceGUI versions.

                    Returns:
                        The callback's result.
                    """
                    with context if context is not None else nullcontext():
                        return await awaitable

                def schedule(awaitable=None, *, coroutine=None, context=None, **kwargs):
                    """Capture async callbacks for deterministic in-process execution.

                    Args:
                        awaitable: Callback scheduled by NiceGUI's event wrapper.
                        coroutine: Legacy keyword form of the callback.
                        context: Optional parent slot for the callback.
                        **kwargs: Task options unused by this local scheduler.

                    Returns:
                        The scheduled callback task.
                    """
                    callback = awaitable if awaitable is not None else coroutine
                    task = asyncio.create_task(run_callback(callback, context))
                    pending.append(task)
                    return task

                def defer(awaitable=None, **kwargs):
                    """Exercise current, explicit-context, and direct scheduling paths.

                    Args:
                        awaitable: Callback scheduled by NiceGUI.
                        **kwargs: Callback scheduling options.

                    Returns:
                        The scheduled callback task.
                    """
                    if scheduler_style == "context":
                        kwargs.setdefault("context", element.parent_slot)
                    if scheduler_style == "direct":
                        return background_tasks.create(awaitable, **kwargs)
                    return schedule(awaitable, **kwargs)

                with monkeypatch.context() as patches, client:
                    patches.setattr(background_tasks, "create", schedule)
                    patches.setattr(background_tasks, "create_or_defer", defer)
                    for listener in listeners:
                        payload = {"added": True, **(args or {})} if event == "selection" else args or {}
                        result = listener.handler(events.GenericEventArguments(sender=element, client=client, args=payload))
                        if asyncio.iscoroutine(result):
                            await result
                    for task in pending:
                        await task

            catalog_table = elements(ui.table)[0]
            assert all(element.props.get("label") != "SQL query" for element in elements(ui.input))
            await trigger(catalog_table, "selection", {"rows": [{
                "qid": "Q1", "components": [{"name": "forged", "url": "file:///etc/passwd"}],
            }]})
            assert downloads == [trusted_url]
            result_table = elements(ui.table)[-1]
            assert len(result_table.rows) == 100
            assert result_table.rows[0]["c0"] == "0"
            assert any("first 120 of 200" in label.text for label in elements(ui.label))
            next_button = next(button for button in elements(ui.button) if button.text == "Next")
            await trigger(next_button, "click")
            assert len(elements(ui.table)[-1].rows) == 20
            assert elements(ui.table)[-1].rows[0]["c0"] == "100"
            selects = {element.props.get("label"): element for element in elements(ui.select)}
            selects["Filter column"].value = "cases"
            selects["Condition"].value = ">="
            next(element for element in elements(ui.input) if element.props.get("label") == "Filter value").value = "115"
            apply_button = next(button for button in elements(ui.button) if button.text == "Apply")
            await trigger(apply_button, "click")
            assert [row["c0"] for row in elements(ui.table)[-1].rows] == ["115", "116", "117", "118", "119"]
            assert any(label.text == "Page 1 of 1" for label in elements(ui.label))
            add_button = next(button for button in elements(ui.button) if button.text == "Add filter")
            await trigger(add_button, "click")
            columns = [element for element in elements(ui.select) if element.props.get("label") == "Filter column"]
            conditions = [element for element in elements(ui.select) if element.props.get("label") == "Condition"]
            values = [element for element in elements(ui.input) if element.props.get("label") == "Filter value"]
            assert len(columns) == 2
            columns[1].value = "cases"
            conditions[1].value = "<"
            values[1].value = "118"
            await trigger(apply_button, "click")
            assert [row["c0"] for row in elements(ui.table)[-1].rows] == ["115", "116", "117"]
            values[0].value = "0"
            await trigger(apply_button, "click")
            assert len(elements(ui.table)[-1].rows) == 100
            values[1].value = "1"
            await trigger(next_button, "click")
            assert [row["c0"] for row in elements(ui.table)[-1].rows] == [str(value) for value in range(100, 118)]
            values[0].value = "115"
            values[1].value = "118"
            await trigger(apply_button, "click")
            remove_buttons = [button for button in elements(ui.button) if button.text == "Remove filter"]
            await trigger(remove_buttons[1], "click")
            await trigger(apply_button, "click")
            assert [row["c0"] for row in elements(ui.table)[-1].rows] == ["115", "116", "117", "118", "119"]
            await trigger(remove_buttons[0], "click")
            await trigger(apply_button, "click")
            assert len(elements(ui.table)[-1].rows) == 100
            await trigger(next_button, "click")
            assert len(elements(ui.table)[-1].rows) == 20
            await trigger(catalog_table, "selection", {"rows": [{"qid": "Q1"}]})
            assert len([element for element in elements(ui.select) if element.props.get("label") == "Filter column"]) == 1
            assert len(elements(ui.table)[-1].rows) == 100
            await trigger(catalog_table, "selection", {"added": False, "rows": [{"qid": "Q1"}], "keys": ["Q1"]})
            assert downloads == [trusted_url, trusted_url] and len(elements(ui.table)) == 1
            await trigger(catalog_table, "selection", {"rows": [{"qid": "unknown", "components": [{"url": trusted_url}]}]})
            assert downloads == [trusted_url, trusted_url]
            assert len(elements(ui.table)) == 1
            client.delete()

    asyncio.run(scenario())
