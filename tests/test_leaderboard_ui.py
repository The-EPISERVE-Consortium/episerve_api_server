"""The stored leaderboard page: reads only, no external calls."""

import asyncio
import re
from datetime import date

import httpx
from nicegui import Client, background_tasks, events, ui

from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry, Leaderboard
from app.main import app
from app.ui import leaderboard_page, pages

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 1, 31))
COMMIT = "0123abcd" * 8
BOARDS = [{"id": 2, "name": "COVID 2022", "entry_count": 1}, {"id": 3, "name": "Flu", "entry_count": 2}]


def _entry(qid="Q10", model="timesfm", mae=1.0, **overrides):
    base = dict(
        run_qid=qid, model_name=model, docker_tag="latest", params={"history_length": 91},
        history_start=None, history_end=date(2021, 12, 31), run_timestamp="2026-01-01T00:00:00Z",
        n_scored=2, n_expected=2, metrics={"mae": mae, "rmse": mae, "coverage": 0.8, "interval_score": 4.0},
        points=[{"date": "2022-01-05", "truth": 10.0, "forecast": 11.0, "q10": 9.0, "q90": 13.0},
                {"date": "2022-01-12", "truth": 20.0, "forecast": 21.0, "q10": 19.0, "q90": 23.0}],
        entry_id=9, truth_commit=COMMIT, truth_as_of="2026-10-14T00:00:00Z", scored_at="2026-10-16 10:00:00",
    )
    return Entry(**{**base, **overrides})


def _board(entries, bid=3):
    return Leaderboard(BENCH, "interval_score", 2, "2026-10-14T00:00:00Z", entries, name="Flu", benchmark_id=bid)


def _render(monkeypatch, *, boards=BOARDS, board=None, logged_in=False, query="", after=None, rank_log=None):
    """Render the page in memory and return what a test needs to inspect it."""
    def external(*args, **kwargs):
        raise AssertionError("the page must not call CKAN, DOIP or lakeFS")

    monkeypatch.setattr(pages.ckan_client, "list_processed_datasets", external)
    monkeypatch.setattr(pages.ckan_client, "list_model_runs", external)
    monkeypatch.setattr(leaderboard_page.store, "list_benchmarks", lambda: boards)

    def read(bid, rank_by="interval_score", best_per_model=True):
        if rank_log is not None:
            rank_log.append((bid, rank_by, best_per_model))
        return board(bid) if callable(board) else board

    monkeypatch.setattr(leaderboard_page.actions, "read_leaderboard", read)
    monkeypatch.setattr(leaderboard_page, "_is_logged_in", lambda: logged_in)
    out = {}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get("/ui/leaderboard" + query)
            out["status"] = response.status_code
            client_id = re.search(r"'client_id': '([0-9a-f-]+)'", response.text).group(1)
            client = Client.instances[client_id]

            def find(kind):
                return [e for e in client.elements.values() if isinstance(e, kind)]

            tables = [t for t in find(ui.table) if t.props.get("row-key") == "run_qid"]
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


async def _click(button, client, args=None):
    for listener in button._event_listeners.values():
        if listener.type == "click":
            res = listener.handler(events.GenericEventArguments(sender=button, client=client, args=args or {}))
            if asyncio.iscoroutine(res):
                await res


def test_nav_has_leaderboard_item():
    assert ("Leaderboard", "/ui/leaderboard") in pages.NAV_ITEMS


def test_page_renders_the_first_benchmark_from_the_database_only(monkeypatch):
    log = []
    out = _render(monkeypatch, board=_board([_entry(), _entry("Q11", "other", mae=2.0)]), rank_log=log)
    assert out["status"] == 200
    assert [r["model"] for r in out["table"].rows] == ["timesfm", "other"]
    assert log[0][0] == 2          # the first benchmark in the list is selected by default
    assert any(t.startswith("Target v, test window 2022-01-01") for t in out["labels"])


def test_query_parameter_selects_the_benchmark(monkeypatch):
    log = []
    _render(monkeypatch, board=_board([_entry()]), query="?benchmark=3", rank_log=log)
    assert log[0][0] == 3
    log.clear()
    _render(monkeypatch, board=_board([_entry()]), query="?benchmark=999", rank_log=log)
    assert log[0][0] == 2          # unknown id falls back to the first benchmark


def test_empty_states_and_login_hint(monkeypatch):
    out = _render(monkeypatch, boards=[], board=None)
    assert "No benchmarks yet. Log in to create one." in out["labels"]
    out = _render(monkeypatch, board=_board([]))
    assert "No runs on this leaderboard yet. Log in to add runs." in out["labels"]
    out = _render(monkeypatch, board=_board([]), logged_in=True)
    assert "No runs on this leaderboard yet." in out["labels"]


def test_database_errors_show_a_label_instead_of_crashing(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(leaderboard_page.store, "list_benchmarks", boom)
    monkeypatch.setattr(leaderboard_page, "_is_logged_in", lambda: False)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            return await http.get("/ui/leaderboard")

    response = asyncio.run(fetch())
    assert response.status_code == 200 and "db down" in response.text


def test_rank_by_and_all_runs_rerender_from_the_database(monkeypatch):
    log = []

    async def change(out, client, find, drain):
        select = next(e for e in find(ui.select) if e.props.get("label") == "Rank by")
        select.value = "mae"
        await drain()
        next(e for e in find(ui.switch) if e.text == "Show all runs").value = True
        await drain()

    _render(monkeypatch, board=_board([_entry()]), after=change, rank_log=log)
    assert log[0][1:] == ("interval_score", True)
    assert ("mae" in [call[1] for call in log]) and log[-1][2] is False


def test_columns_are_sortable_and_metrics_sort_on_raw_values(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    columns = {c["name"]: c for c in out["table"].columns}
    for name in ("rank", "model", "history", "mae", "rmse", "coverage", "interval_score"):
        assert columns[name]["sortable"] is True
    for name in ("mae", "rmse", "coverage", "interval_score"):
        assert f"rowA.raw.{name}" in columns[name][":sort"]
    assert out["table"].rows[0]["raw"]["mae"] == 1.0


def test_model_filter_rerenders_only_matching_rows(monkeypatch):
    async def pick(out, client, find, drain):
        select = next(e for e in find(ui.select) if e.props.get("label") == "Model")
        assert list(select.options) == ["All models", "other", "timesfm"]
        tables = lambda: [t for t in find(ui.table) if t.props.get("row-key") == "run_qid"]  # noqa: E731
        assert [len(t.rows) for t in tables()] == [2]
        select.value = "other"
        await drain()
        assert len(tables()) == 1                    # replaced, not duplicated
        out["rows_after"] = [r["model"] for r in tables()[0].rows]
        out["labels_after"] = [e.text for e in find(ui.label)]
        number = next(e for e in find(ui.number) if e.props.get("label") == "Min. history (days)")
        number.value = 10_000
        await drain()
        out["rows_min"] = [r["model"] for r in tables()[0].rows]

    out = _render(monkeypatch, board=_board([_entry(), _entry("Q11", "other", mae=2.0)]), after=pick)
    assert out["rows_after"] == ["other"]
    assert "Showing 1 of 2 entries" in out["labels_after"]
    assert out["rows_min"] == []


def test_detail_dialog_shows_link_window_params_pins_and_per_date_rows(monkeypatch):
    async def open_row(out, client, find, drain):
        table = out["table"]
        for listener in table._event_listeners.values():
            if listener.type == "rowClick":
                res = listener.handler(events.GenericEventArguments(
                    sender=table, client=client, args=[{}, {"run_qid": "Q10"}, 0]))
                if asyncio.iscoroutine(res):
                    await res
        out["dialog_labels"] = [e.text for e in find(ui.label)]
        out["links"] = [(e.props.get("href"), e.props.get("target")) for e in find(ui.link)]
        out["tables"] = find(ui.table)
        out["charts"] = find(ui.echart)

    out = _render(monkeypatch, board=_board([_entry()]), after=open_row)
    assert ("https://data.episerve.zib.de/dataset/q10", "_blank") in out["links"]
    assert any(t.startswith("History window:") and "→ 2021-12-31" in t for t in out["dialog_labels"])
    assert "history_length: 91" in out["dialog_labels"]
    assert "timesfm — run Q10" in out["dialog_labels"]
    assert f"Data: lakeFS commit {COMMIT} · CKAN last modified 2026-10-14T00:00:00Z · scored 2026-10-16 10:00:00" in out["dialog_labels"]
    detail = out["tables"][-1]
    assert [c["name"] for c in detail.columns] == ["date", "observed", "forecast", "q10", "q90", "error"]
    assert detail.rows[0]["error"] == "1.00"
    series = {s["name"]: s["data"] for s in out["charts"][0].options["series"]}
    assert series["Observed"] == [10.0, 20.0] and series["q10–q90"] == [4.0, 4.0]


def test_non_numeric_benchmark_parameter_falls_back_to_the_first_benchmark(monkeypatch):
    log = []
    out = _render(monkeypatch, board=_board([_entry()]), query="?benchmark=abc", rank_log=log)
    assert out["status"] == 200
    assert log[0][0] == 2


def test_model_filter_survives_a_rank_by_change(monkeypatch):
    async def scenario(out, client, find, drain):
        next(e for e in find(ui.select) if e.props.get("label") == "Model").value = "other"
        await drain()
        next(e for e in find(ui.select) if e.props.get("label") == "Rank by").value = "mae"
        await drain()
        out["labels_after"] = [e.text for e in find(ui.label)]
        out["rows_after"] = [r["model"] for t in find(ui.table) if t.props.get("row-key") == "run_qid"
                             for r in t.rows]

    out = _render(monkeypatch, board=_board([_entry(), _entry("Q11", "other", mae=2.0)]), after=scenario)
    assert out["rows_after"] == ["other"]
    assert "Showing 1 of 2 entries" in out["labels_after"]


def _labels(find):
    return [e.text for e in find(ui.label)]


def _button(find, label):
    return next((b for b in find(ui.button) if b.props.get("label") == label), None)


def test_anonymous_users_see_no_management_controls(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    labels = {b.props.get("label") for b in out["find"](ui.button)}
    assert not labels & {"New benchmark", "Add run", "Retire benchmark"}


def test_logged_in_users_see_management_controls(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True)
    labels = {b.props.get("label") for b in out["find"](ui.button)}
    assert {"New benchmark", "Add run", "Retire benchmark"} <= labels


def test_add_run_dialog_lists_addable_runs_and_explains_the_others(monkeypatch):
    monkeypatch.setattr(leaderboard_page.actions, "candidates", lambda bid: [
        {"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None},
        {"qid": "Q11", "model_name": "chronos", "run_timestamp": "2026-01-01T00:00:00Z", "reason": "run status is failed, not success"},
    ])
    added = []
    monkeypatch.setattr(leaderboard_page.actions, "add_run", lambda bid, qid: added.append((bid, qid)) or 42)

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Add run"), client)
        select = next(e for e in find(ui.select) if e.props.get("label") == "Model run")
        assert list(select.options.values()) == ["timesfm · Q10 · 2026-01-02"] or list(select.options) == ["Q10"]
        out["labels_dialog"] = _labels(find)
        select.value = "Q10"
        await _click(_button(find, "Add"), client)
        await drain()

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert any("chronos (Q11): run status is failed" in t for t in out["labels_dialog"])
    assert added == [(2, "Q10")]


def test_add_run_shows_the_reason_when_scoring_fails(monkeypatch):
    from app.leaderboard.scoring import NotScorable

    monkeypatch.setattr(leaderboard_page.actions, "candidates", lambda bid: [
        {"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None}])

    def refuse(bid, qid):
        raise NotScorable("forecast covers 1 of 2 dates in the test window")

    monkeypatch.setattr(leaderboard_page.actions, "add_run", refuse)

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Add run"), client)
        next(e for e in find(ui.select) if e.props.get("label") == "Model run").value = "Q10"
        await _click(_button(find, "Add"), client)
        await drain()
        out["labels_after"] = _labels(find)

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert any("forecast covers 1 of 2 dates" in t for t in out["labels_after"])


def test_remove_button_in_the_detail_dialog_removes_the_entry(monkeypatch):
    removed = []
    monkeypatch.setattr(leaderboard_page.actions, "remove_entry", lambda bid, eid: removed.append((bid, eid)))

    async def scenario(out, client, find, drain):
        table = out["table"]
        for listener in table._event_listeners.values():
            if listener.type == "rowClick":
                res = listener.handler(events.GenericEventArguments(
                    sender=table, client=client, args=[{}, {"run_qid": "Q10"}, 0]))
                if asyncio.iscoroutine(res):
                    await res
        await _click(_button(find, "Remove from leaderboard"), client)
        await drain()

    _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert removed == [(2, 9)]


def test_new_benchmark_dialog_validates_and_creates(monkeypatch):
    created = []
    monkeypatch.setattr(leaderboard_page.actions, "create_benchmark", lambda **kw: created.append(kw) or 8)

    async def scenario(out, client, find, drain):
        # _render makes CKAN raise while the page loads; the dialog may call it once it opens.
        monkeypatch.setattr(leaderboard_page.ckan_client, "list_processed_datasets",
                            lambda: [{"qid": "Q1", "name": "Wastewater"}])
        await _click(_button(find, "New benchmark"), client)
        inputs = {i.props.get("label"): i for i in find(ui.input)}
        inputs["Name"].value = "Flu 2022"
        inputs["Target column"].value = "v"
        inputs["Test start (C)"].value = "not-a-date"
        inputs["Test end (D)"].value = "2022-01-31"
        await _click(_button(find, "Create"), client)
        await drain()
        out["labels_bad"] = _labels(find)
        assert created == []
        inputs["Test start (C)"].value = "2022-01-01"
        await _click(_button(find, "Create"), client)
        await drain()

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert any("YYYY-MM-DD" in t for t in out["labels_bad"])
    assert created and created[0]["name"] == "Flu 2022" and created[0]["dataset_qid"] == "Q1"
    assert created[0]["target_column"] == "v" and created[0]["date_column"] == "datum"
