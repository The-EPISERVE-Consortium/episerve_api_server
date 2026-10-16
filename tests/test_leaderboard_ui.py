"""The leaderboard page renders anonymously and lists the nav entry."""

import asyncio

import httpx
import pytest

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


def _run_compute(monkeypatch, loaders, values=None, after=None, runs_extra=None):
    """Render the page, press Compute, and return (client, elements-by-type helper)."""
    import re

    from contextlib import nullcontext

    from nicegui import Client, background_tasks, events, ui

    SQL = "SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2021-12-31'"
    runs = [
        {"qid": "Q10", "model_name": "timesfm", "docker_tag": "latest", "status": "success",
         "input_dataset_qids": ["Q1"], "data_transformation_sql": [SQL],
         "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "r10"},
        {"qid": "Q11", "model_name": "other", "docker_tag": "latest", "status": "success",
         "input_dataset_qids": ["Q1"], "data_transformation_sql": ["SELECT 1"],
         "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "r11"},
    ]
    runs.extend(runs_extra or [])
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
            for label, value in (values or {}).items():
                inputs[label].value = value
            button = next(b for b in find(ui.button) if b.props.get("label") == "Compute")
            tasks = []

            def capture(awaitable=None, *, coroutine=None, **kwargs):
                tasks.append(asyncio.create_task(_in_slot(awaitable or coroutine, kwargs.get("context"))))
                return tasks[-1]

            async def _in_slot(awaitable, context):
                with context if context is not None else nullcontext():
                    return await awaitable

            with monkeypatch.context() as patches, client:
                patches.setattr(background_tasks, "create", capture)
                patches.setattr(background_tasks, "create_or_defer", capture)
                for listener in button._event_listeners.values():
                    if listener.type == "click":
                        res = listener.handler(events.GenericEventArguments(sender=button, client=client, args={}))
                        if asyncio.iscoroutine(res):
                            await res
                for task in tasks:
                    await task
            out["client"], out["find"], out["ui"] = client, find, ui
            out["labels"] = [e.text for e in find(ui.label)]
            table = find(ui.table)
            if table:
                out["table"] = table[0]
                with client:
                    for listener in table[0]._event_listeners.values():
                        if listener.type == "rowClick":
                            res = listener.handler(events.GenericEventArguments(
                                sender=table[0], client=client, args=[{}, {"run_qid": "Q10"}, 0]))
                            if asyncio.iscoroutine(res):
                                await res
                out["charts"] = find(ui.echart)
                if after:
                    pending = []

                    def defer(awaitable=None, *, coroutine=None, **kwargs):
                        pending.append(awaitable or coroutine)    # UI updates run when drained

                    async def drain():
                        while pending:
                            await pending.pop(0)

                    with monkeypatch.context() as patches, client:
                        patches.setattr(background_tasks, "create", defer)
                        patches.setattr(background_tasks, "create_or_defer", defer)
                        res = after(out, client, find, ui, drain)
                        if asyncio.iscoroutine(res):
                            await res

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
    assert any(t.startswith("other (Q11):") for t in out["labels"])
    assert any("Not scorable runs (1)" in str(e.props.get("label", "")) for e in out["find"](out["ui"].expansion))
    chart = out["charts"][0]
    series = {s["name"]: s["data"] for s in chart.options["series"]}
    assert series["Observed"] == [10.0, 20.0] and series["q10–q90"] == [4.0, 4.0]


def test_compute_shows_error_label_when_truth_cannot_load(monkeypatch):
    from app.leaderboard.loaders import LoadError

    def boom(dataset, bench):
        raise LoadError("column 'v' not in dataset")

    out = _run_compute(monkeypatch, {"load_truth": boom, "load_predictions": None, "load_params": None})
    assert any("column 'v' not in dataset" in text for text in out["labels"])
    assert "table" not in out


@pytest.mark.parametrize("values,message", [
    ({"Test start (C)": "not-a-date"}, "YYYY-MM-DD"),
    ({"Test start (C)": None}, "YYYY-MM-DD"),
    ({"Test start (C)": "2022-02-01"}, "not after the test end"),
    ({"Target column": "   "}, "Choose a dataset"),
    ({"Filter column (optional)": "typ"}, "filter value"),
])
def test_compute_rejects_invalid_input_without_building(monkeypatch, values, message):
    def must_not_run(*args, **kwargs):
        raise AssertionError("board must not be computed")

    out = _run_compute(monkeypatch, {"load_truth": must_not_run, "load_predictions": must_not_run,
                                     "load_params": must_not_run}, values)
    assert any(message in text for text in out["labels"])
    assert "table" not in out


def _two_model_loaders():
    import pandas as pd

    dates = pd.to_datetime(["2022-01-05", "2022-01-12"])
    return {
        "load_truth": lambda dataset, bench: pd.DataFrame({"date": dates, "truth": [10.0, 20.0]}),
        "load_predictions": lambda run, bench: pd.DataFrame(
            {"date": dates, "forecast": [11.0, 21.0], "q10": [9.0, 19.0], "q90": [13.0, 23.0]}),
        "load_params": lambda qid: {"history_length": 91},
    }


_SECOND_RUN = {"qid": "Q12", "model_name": "timesfm2", "docker_tag": "latest", "status": "success",
               "input_dataset_qids": ["Q1"],
               "data_transformation_sql": ["SELECT * FROM df WHERE CAST(datum AS DATE) <= DATE '2021-12-31'"],
               "run_timestamp": "2026-01-01T00:00:00Z", "predictions_resource_id": "r12"}


def test_columns_are_sortable_and_metrics_sort_on_raw_values(monkeypatch):
    out = _run_compute(monkeypatch, _two_model_loaders())
    columns = {c["name"]: c for c in out["table"].columns}
    for name in ("rank", "model", "history", "mae", "rmse", "coverage", "interval_score"):
        assert columns[name]["sortable"] is True
    for name in ("mae", "rmse", "coverage", "interval_score"):
        assert f"rowA.raw.{name}" in columns[name][":sort"]
    assert out["table"].rows[0]["raw"]["mae"] == 1.0


def test_model_filter_rerenders_only_matching_rows(monkeypatch):
    async def pick(out, client, find, ui, drain):
        select = next(e for e in find(ui.select) if e.props.get("label") == "Model")
        assert list(select.options) == ["All models", "timesfm", "timesfm2"]
        board = lambda: [t for t in find(ui.table) if t.props.get("row-key") == "run_qid"]  # noqa: E731
        assert [len(t.rows) for t in board()] == [2]
        select.value = "timesfm2"
        await drain()
        assert len(board()) == 1                      # the table was replaced, not duplicated
        out["rows_after"] = [r["model"] for r in board()[0].rows]
        out["labels_after"] = [e.text for e in find(ui.label)]
        number = next(e for e in find(ui.number) if e.props.get("label") == "Min. history (days)")
        number.value = 10_000
        await drain()
        out["rows_min"] = [r["model"] for r in board()[0].rows]

    out = _run_compute(monkeypatch, _two_model_loaders(), after=pick, runs_extra=[_SECOND_RUN])
    assert out["rows_after"] == ["timesfm2"]
    assert "Showing 1 of 2 entries" in out["labels_after"]
    assert out["rows_min"] == []


def test_detail_dialog_shows_link_window_params_and_per_date_rows(monkeypatch):
    def texts(out, client, find, ui, drain):
        out["dialog_labels"] = [e.text for e in find(ui.label)]
        out["links"] = [(e.props.get("href"), e.props.get("target")) for e in find(ui.link)]
        out["tables"] = find(ui.table)
        out["charts"] = find(ui.echart)

    out = _run_compute(monkeypatch, _two_model_loaders(), after=texts)
    assert ("https://data.episerve.zib.de/dataset/q10", "_blank") in out["links"]
    assert any(t.startswith("History window:") and "→ 2021-12-31" in t for t in out["dialog_labels"])
    assert "history_length: 91" in out["dialog_labels"]
    assert "timesfm — run Q10" in out["dialog_labels"]
    detail = out["tables"][-1]
    assert [c["name"] for c in detail.columns] == ["date", "observed", "forecast", "q10", "q90", "error"]
    assert detail.rows[0]["error"] == "1.00" and detail.rows[1]["observed"] == "20.00"
