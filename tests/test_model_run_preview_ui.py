"""Exercise model-run preview callbacks with mocked backend responses."""

import asyncio
from contextlib import nullcontext
from io import BytesIO
import re

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from nicegui import Client, background_tasks, events, ui

from app import dataset_preview
from app.main import app
from app.ui import pages
from tests.test_dataset_preview import FakeResponse


@pytest.mark.parametrize("saved_input", [True, False])
def test_model_run_selection_and_row_limit_ignore_metadata_sql(monkeypatch, tmp_path, saved_input):
    """Exercise safe initial/reloaded previews and reject forged selections.

    Args:
        monkeypatch: Pytest patch fixture.
        tmp_path: Temporary directory for detecting unexpected file writes.
        saved_input: Whether metadata contains a saved transformed input.
    """
    base = "https://doip.example"
    target = tmp_path / "injected.csv"
    components = [{"componentId": "predictions.parquet", "@id": "components/output/predictions.parquet"}]
    if saved_input:
        components.append({"componentId": "saved.parquet", "@id": "components/input/saved.parquet"})
    original_url = "http://internal/original.parquet" if saved_input else base + "/doip/retrieve/Q2/original.parquet"
    metadata = {
        "@id": "../../Q999",
        "kernel": {"fdo:hasComponent": components},
        "provenance": {"prov:used": [{"@id": original_url, "schema:query": f"COPY (SELECT 1) TO '{target}'"}]},
    }
    metadata_calls, downloads = [], []
    parquet = BytesIO()
    pq.write_table(pa.table({"cases": list(range(60))}), parquet)

    def catalog_runs():
        """Return the server-held run catalog.

        Returns:
            One run whose preview identifier is Q1.
        """
        return [{"qid": "Q1", "model_name": "Test model", "doip_url": "http://ignored-catalog-url"}]

    def catalog_datasets():
        """Return an empty dataset catalog for input-name lookup.

        Returns:
            No dataset records.
        """
        return []

    def fetch_metadata(qid, doip_base):
        """Return mocked metadata and record its server-resolved identity.

        Args:
            qid: Run identifier selected by the server.
            doip_base: Configured DOIP service.

        Returns:
            Metadata including malicious provenance SQL and a forged metadata ID.
        """
        metadata_calls.append((qid, doip_base))
        return metadata

    def get(url, **kwargs):
        """Serve in-memory Parquet only for approved, expected destinations.

        Args:
            url: Requested component URL.
            **kwargs: Restricted download settings.

        Returns:
            A fake streaming Parquet response.
        """
        expected_input = base + "/doip/retrieve/Q1/input/saved.parquet" if saved_input else original_url
        assert url in (expected_input, base + "/doip/retrieve/Q1/output/predictions.parquet")
        assert kwargs["allow_redirects"] is False
        downloads.append(url)
        return FakeResponse(parquet.getvalue())

    monkeypatch.setattr(pages.settings, "doip_url", base)
    monkeypatch.setattr(pages.ckan_client, "list_model_runs", catalog_runs)
    monkeypatch.setattr(pages.ckan_client, "list_processed_datasets", catalog_datasets)
    monkeypatch.setattr(pages, "fetch_run_metadata", fetch_metadata)
    monkeypatch.setattr(dataset_preview.requests, "get", get)

    async def scenario():
        """Drive the model-run page and callbacks using an in-process ASGI client."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get("/ui/model-runs")
            assert response.status_code == 200
            client_id = re.search(r"'client_id': '([0-9a-f-]+)'", response.text).group(1)
            client = Client.instances[client_id]

            def elements(kind):
                """Find page elements by type.

                Args:
                    kind: NiceGUI element class.

                Returns:
                    Matching page elements.
                """
                return [element for element in client.elements.values() if isinstance(element, kind)]

            async def trigger(element, event, args=None, value=None):
                """Await callbacks across NiceGUI scheduling APIs.

                Args:
                    element: Event target.
                    event: Event type to invoke, or None for a value assignment.
                    args: Browser event payload.
                    value: Value assigned to a row-limit control.
                """
                pending = []

                async def run_callback(awaitable, context):
                    """Await an event callback in its parent slot.

                    Args:
                        awaitable: Scheduled event callback.
                        context: Optional UI parent slot.
                    """
                    with context if context is not None else nullcontext():
                        await awaitable

                def schedule(awaitable=None, *, coroutine=None, context=None, **kwargs):
                    """Capture callback tasks instead of deferring to app startup.

                    Args:
                        awaitable: Scheduled callback.
                        coroutine: Legacy keyword form of the callback.
                        context: Optional UI parent slot.
                        **kwargs: Unused scheduling options.

                    Returns:
                        The scheduled task.
                    """
                    task = asyncio.create_task(run_callback(awaitable if awaitable is not None else coroutine, context))
                    pending.append(task)
                    return task

                with monkeypatch.context() as patches, client:
                    patches.setattr(background_tasks, "create", schedule)
                    patches.setattr(background_tasks, "create_or_defer", schedule)
                    if event is None:
                        element.value = value
                    else:
                        for listener in list(element._event_listeners.values()):
                            if listener.type == event:
                                payload = {"added": True, **(args or {})}
                                result = listener.handler(events.GenericEventArguments(sender=element, client=client, args=payload))
                                if asyncio.iscoroutine(result):
                                    await result
                    for task in pending:
                        await task

            catalog_table = elements(ui.table)[0]
            await trigger(catalog_table, "selection", {"rows": [{"qid": "Q1", "doip_url": "http://internal/secret"}]})
            assert metadata_calls == [("Q1", base)]
            assert len(downloads) == 2
            assert len(elements(ui.table)) == 3
            assert all(len(table.rows) == 60 for table in elements(ui.table)[1:])
            assert elements(ui.code)[0].content == metadata["provenance"]["prov:used"][0]["schema:query"]
            if not saved_input:
                assert any("transformations not reapplied" in label.text for label in elements(ui.label))
            number = next(number for number in elements(ui.number) if number.props.get("label") == "Max rows")
            await trigger(number, None, value=25)
            assert len(downloads) == 4
            assert all(len(table.rows) == 25 for table in elements(ui.table)[1:])
            assert downloads[:2] == downloads[2:]
            await trigger(number, None, value=100_000)
            assert len(downloads) == 4
            await trigger(catalog_table, "selection", {"rows": [{"qid": "Q999", "doip_url": base + "/doip/retrieve/Q1"}]})
            assert metadata_calls == [("Q1", base)] and len(downloads) == 4
            assert len(elements(ui.table)) == 1
            assert not target.exists()
            client.delete()

    asyncio.run(scenario())
