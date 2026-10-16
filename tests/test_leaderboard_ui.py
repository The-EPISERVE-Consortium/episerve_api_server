"""The stored leaderboard page: reads only, no external calls."""

import asyncio
from datetime import date

import httpx
from nicegui import events, ui

from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry, Leaderboard
from app.main import app
from app.ui import leaderboard_page, pages
from tests.ui_harness import click, render

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
    def read(bid, rank_by="interval_score", best_per_model=True):
        if rank_log is not None:
            rank_log.append((bid, rank_by, best_per_model))
        return board(bid) if callable(board) else board

    def setup():
        monkeypatch.setattr(leaderboard_page.bactions, "list_final", lambda: boards)
        monkeypatch.setattr(leaderboard_page.actions, "read_leaderboard", read)

    return render(monkeypatch, leaderboard_page, "/ui/leaderboard" + query, logged_in=logged_in, setup=setup,
                  after=after, table_key="run_qid")


_click = click


def test_benchmark_definitions_link_is_present(monkeypatch):
    for boards in (BOARDS, []):
        out = _render(monkeypatch, boards=boards, board=_board([_entry()]))
        hrefs = {e.props.get("href"): e.text for e in out["find"](ui.link)}
        assert hrefs.get("/ui/benchmarks") == "Benchmark definitions"


def test_nav_has_benchmarks_before_leaderboard():
    items = [p for _, p in pages.NAV_ITEMS]
    assert items.index("/ui/benchmarks") + 1 == items.index("/ui/leaderboard")


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
    assert "No benchmarks yet. Design one on the Benchmarks page." in out["labels"]
    out = _render(monkeypatch, boards=[], board=None, logged_in=True)
    assert "No benchmarks yet. Design one on the Benchmarks page." in out["labels"]
    out = _render(monkeypatch, board=_board([]))
    assert "No runs on this leaderboard yet. Log in to add runs." in out["labels"]
    out = _render(monkeypatch, board=_board([]), logged_in=True)
    assert "No runs on this leaderboard yet." in out["labels"]


def test_database_errors_show_a_label_instead_of_crashing(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(leaderboard_page.bactions, "list_final", boom)
    monkeypatch.setattr(leaderboard_page, "_is_logged_in", lambda: False)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            return await http.get("/ui/leaderboard")

    response = asyncio.run(fetch())
    assert response.status_code == 200
    assert "Could not load benchmarks." in response.text and "db down" not in response.text


def test_leaderboard_read_failure_shows_generic_text_and_logs(monkeypatch, caplog):
    def boom(bid, rank_by="interval_score", best_per_model=True):
        raise RuntimeError("Can't connect to MySQL server on 'db.internal'")

    with caplog.at_level("ERROR"):
        out = _render(monkeypatch, board=boom)
    assert "⚠ Could not load the leaderboard." in out["labels"]
    assert not any("db.internal" in t for t in out["labels"])
    assert any("db.internal" in (r.exc_text or "") for r in caplog.records)


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
    assert "Add run" in labels
    assert not labels & {"New benchmark", "Retire benchmark"}   # management moved to /ui/benchmarks


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


def _capture_notify(monkeypatch):
    notes = []
    monkeypatch.setattr(leaderboard_page.ui, "notify", lambda message, **kw: notes.append((message, kw)))
    return notes


def _expired(monkeypatch):
    monkeypatch.setattr(leaderboard_page, "_is_logged_in", lambda: False)


def test_expired_login_blocks_add_run(monkeypatch):
    monkeypatch.setattr(leaderboard_page.actions, "candidates", lambda bid: [
        {"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None}])
    called = []
    monkeypatch.setattr(leaderboard_page.actions, "add_run", lambda bid, qid: called.append(qid) or 1)
    notes = _capture_notify(monkeypatch)

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Add run"), client)
        next(e for e in find(ui.select) if e.props.get("label") == "Model run").value = "Q10"
        _expired(monkeypatch)
        await _click(_button(find, "Add"), client)
        await drain()

    _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert called == [] and notes and notes[-1][1]["type"] == "warning" and "expired" in notes[-1][0]


def test_expired_login_blocks_remove_entry(monkeypatch):
    called = []
    monkeypatch.setattr(leaderboard_page.actions, "remove_entry", lambda bid, eid: called.append(eid))
    notes = _capture_notify(monkeypatch)

    async def scenario(out, client, find, drain):
        table = out["table"]
        for listener in table._event_listeners.values():
            if listener.type == "rowClick":
                res = listener.handler(events.GenericEventArguments(
                    sender=table, client=client, args=[{}, {"run_qid": "Q10"}, 0]))
                if asyncio.iscoroutine(res):
                    await res
        _expired(monkeypatch)
        await _click(_button(find, "Remove from leaderboard"), client)
        await drain()

    _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert called == [] and notes and notes[-1][1]["type"] == "warning"


def test_unexpected_write_errors_are_generic_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(leaderboard_page.actions, "candidates", lambda bid: [
        {"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None}])

    def boom(bid, qid):
        raise RuntimeError("Can't connect to MySQL server on 'db.internal'")

    monkeypatch.setattr(leaderboard_page.actions, "add_run", boom)

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Add run"), client)
        next(e for e in find(ui.select) if e.props.get("label") == "Model run").value = "Q10"
        await _click(_button(find, "Add"), client)
        await drain()
        out["labels_after"] = _labels(find)

    with caplog.at_level("ERROR"):
        out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert "Could not add the run." in out["labels_after"]
    assert not any("db.internal" in t for t in out["labels_after"])
    assert any("db.internal" in (r.exc_text or "") for r in caplog.records)
