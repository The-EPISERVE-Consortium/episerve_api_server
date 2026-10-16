"""Shared in-memory harness for the NiceGUI page tests (no browser, no network)."""

import asyncio
import re

import httpx
from nicegui import Client, background_tasks, events, ui

from app.main import app
from app.ui import pages


def render(monkeypatch, module, path, *, logged_in=False, setup=None, after=None, table_key=None):
    """Render ``path`` in memory and return what a test needs to inspect the page.

    ``module`` is the page module whose ``_is_logged_in`` is faked. CKAN raises while the page
    loads (a scenario may replace it later); ``setup`` patches the page's own data sources;
    ``after(out, client, find, drain)`` runs against the rendered page with deferred
    background tasks collected so ``await drain()`` runs them.
    """
    def external(*args, **kwargs):
        raise AssertionError("the page must not call CKAN, DOIP or lakeFS")

    monkeypatch.setattr(pages.ckan_client, "list_processed_datasets", external)
    monkeypatch.setattr(pages.ckan_client, "list_model_runs", external)
    monkeypatch.setattr(module, "_is_logged_in", lambda: logged_in)
    if setup:
        setup()
    out = {}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get(path)
            out["status"] = response.status_code
            out["text"] = response.text
            match = re.search(r"'client_id': '([0-9a-f-]+)'", response.text)
            if match is None:
                return
            client = Client.instances[match.group(1)]

            def find(kind):
                return [e for e in client.elements.values() if isinstance(e, kind)]

            tables = [t for t in find(ui.table) if table_key and t.props.get("row-key") == table_key]
            out.update(client=client, find=find, labels=[e.text for e in find(ui.label)],
                       table=tables[0] if tables else None)
            if after:
                pending = []

                def defer(awaitable=None, *, coroutine=None, **kwargs):
                    pending.append(awaitable or coroutine)

                async def drain():
                    while pending:
                        await pending.pop(0)

                with monkeypatch.context() as patches, client:
                    patches.setattr(background_tasks, "create", defer)
                    patches.setattr(background_tasks, "create_or_defer", defer)
                    res = after(out, client, find, drain)
                    if asyncio.iscoroutine(res):
                        await res

    asyncio.run(scenario())
    return out


async def click(button, client, args=None):
    for listener in button._event_listeners.values():
        if listener.type == "click":
            res = listener.handler(events.GenericEventArguments(sender=button, client=client, args=args or {}))
            if asyncio.iscoroutine(res):
                await res


def labels(find):
    return [e.text for e in find(ui.label)]


def button(find, label):
    return next((b for b in find(ui.button) if b.props.get("label") == label), None)
