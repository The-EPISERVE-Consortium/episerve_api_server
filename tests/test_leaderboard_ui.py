"""The leaderboard page renders anonymously and lists the nav entry."""

import asyncio

import httpx

from app.main import app
from app.ui import pages


def test_leaderboard_page_renders_without_login(monkeypatch):
    monkeypatch.setattr(pages.ckan_client, "list_processed_datasets",
                        lambda: [{"qid": "Q1", "name": "Wastewater"}])

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            return await http.get("/ui/leaderboard")

    response = asyncio.run(fetch())
    assert response.status_code == 200
    assert "Leaderboard" in response.text


def test_nav_has_leaderboard_item():
    assert ("Leaderboard", "/ui/leaderboard") in pages.NAV_ITEMS


def _run_compute(monkeypatch, loaders, set_values=None):
    """Render the page, press Compute, and return (client, elements-by-type helper)."""
    import re

    from nicegui import Client, events, ui

    SQL = "SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2021-12-31'"
    runs = [
        {"qid": "Q10", "model_name": "timesfm", "docker_tag": "latest", "status": "success",
         "input_dataset_qids": ["Q1"], "data_transformation_sql": [SQL],
         "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "r10"},
        {"qid": "Q11", "model_name": "other", "docker_tag": "latest", "status": "success",
         "input_dataset_qids": ["Q1"], "data_transformation_sql": ["SELECT 1"],
         "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "r11"},
    ]
    monkeypatch.setattr(pages.ckan_client, "list_processed_datasets",
                        lambda: [{"qid": "Q1", "name": "Wastewater", "last_modified": "2026-10-14"}])
    monkeypatch.setattr(pages.ckan_client, "list_model_runs", lambda: runs)
    monkeypatch.setattr(pages, "default_loaders", lambda: loaders)
    out = {}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get("/ui/leaderboard")
            client_id = re.search(r"'client_id': '([0-9a-f-]+)'", response.text).group(1)
            client = Client.instances[client_id]

            def find(kind):
                return [e for e in client.elements.values() if isinstance(e, kind)]

            inputs = {i.props.get("label"): i for i in find(ui.input)}
            inputs["Target column"].value = "v"
            inputs["Test start (C)"].value = "2022-01-01"
            inputs["Test end (D)"].value = "2022-01-31"
            button = next(b for b in find(ui.button) if b.props.get("label") == "Compute")
            with client:
                for listener in button._event_listeners.values():
                    if listener.type == "click":
                        listener.handler(events.GenericEventArguments(sender=button, client=client, args={}))
            out["client"], out["find"], out["ui"] = client, find, ui
            out["labels"] = [e.text for e in find(ui.label)]
            table = find(ui.table)
            if table:
                out["table"] = table[0]
                with client:
                    for listener in table[0]._event_listeners.values():
                        if listener.type == "rowClick":
                            listener.handler(events.GenericEventArguments(
                                sender=table[0], client=client, args=[{}, {"run_qid": "Q10"}, 0]))
                out["charts"] = find(ui.echart)

    asyncio.run(scenario())
    return out


def test_compute_shows_ranked_table_detail_chart_and_unscorable_reasons(monkeypatch):
    import pandas as pd

    dates = pd.to_datetime(["2022-01-05", "2022-01-12"])
    loaders = {
        "load_truth": lambda dataset, bench: pd.DataFrame({"date": dates, "truth": [10.0, 20.0]}),
        "load_predictions": lambda run, bench: pd.DataFrame(
            {"date": dates, "forecast": [11.0, 21.0], "q10": [9.0, 19.0], "q90": [13.0, 23.0]}),
        "load_params": lambda qid: {"history_length": 91},
    }
    out = _run_compute(monkeypatch, loaders)
    table = out["table"]
    assert [r["model"] for r in table.rows] == ["timesfm"]
    assert table.rows[0]["mae"] == "1.00"
    assert any(text.startswith("Target v, test window 2022-01-01") for text in out["labels"])
    assert any("Not scorable runs (1)" in str(e.props.get("label", "")) for e in out["find"](out["ui"].expansion))
    chart = out["charts"][0]
    series = {s["name"]: s["data"] for s in chart.options["series"]}
    assert any(t.startswith("other (Q11):") for t in out["labels"])
    assert series["Observed"] == [10.0, 20.0] and series["q90"] == [13.0, 23.0]


def test_compute_shows_error_label_when_truth_cannot_load(monkeypatch):
    from app.leaderboard.loaders import LoadError

    def boom(dataset, bench):
        raise LoadError("column 'v' not in dataset")

    out = _run_compute(monkeypatch, {"load_truth": boom, "load_predictions": None, "load_params": None})
    assert any("column 'v' not in dataset" in text for text in out["labels"])
    assert "table" not in out
