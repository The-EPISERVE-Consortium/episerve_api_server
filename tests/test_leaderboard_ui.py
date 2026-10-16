"""The stored leaderboard page: reads only, no external calls."""

import asyncio
from datetime import date

import httpx
import pytest
from nicegui import events, ui

from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry, Leaderboard
from app.main import app
from app.ui import leaderboard_page, pages
from tests.ui_harness import click, render

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 1, 31))
COMMIT = "0123abcd" * 8
ROW = {"id": 3, "name": "Flu", "dataset_qid": "Q1", "target_column": "v", "date_column": "datum",
       "filter_column": "region", "filter_value": "DE", "test_start": date(2022, 1, 1),
       "test_end": date(2022, 1, 31), "prediction_length": 5, "data_commit": COMMIT}


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


SPEC = {"benchmark_id": 3, "dataset_qid": "Q1", "data_commit": COMMIT,
        "input_data_files": [[f"https://doip.example/doip/retrieve/Q1/d.parquet?version={COMMIT}", "input.parquet"]],
        "data_transformation_sql": ["SELECT 1 FROM df"], "config": {"prediction_length": 5, "prediction_offset": 0},
        "test_start": "2022-01-01", "test_end": "2022-01-31"}


def _render(monkeypatch, *, row=ROW, board=None, logged_in=False, bid=3, after=None, rank_log=None, path=None,
            spec=SPEC):
    """Render the page in memory and return what a test needs to inspect it."""
    def read(benchmark_id, rank_by="interval_score", best_per_model=True):
        if rank_log is not None:
            rank_log.append((benchmark_id, rank_by, best_per_model))
        return board(benchmark_id) if callable(board) else board

    def get(benchmark_id):
        if row is None:
            raise leaderboard_page.bactions.NotFound("no")
        return row

    def setup():
        monkeypatch.setattr(leaderboard_page.bactions, "get", get)
        monkeypatch.setattr(leaderboard_page.actions, "read_leaderboard", read)

        def run_spec(benchmark_id):
            if isinstance(spec, Exception):
                raise spec
            return spec

        monkeypatch.setattr(leaderboard_page.bactions, "run_spec", run_spec)

    return render(monkeypatch, leaderboard_page, path or f"/ui/benchmarks/{bid}", logged_in=logged_in, setup=setup,
                  after=after, table_key="run_qid")


_click = click


def test_back_link_and_definition_are_shown(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    hrefs = {e.props.get("href"): e.text for e in out["find"](ui.link)}
    assert hrefs.get("/ui/benchmarks") == "← All benchmarks"
    assert "Flu" in out["labels"]
    # the facts live in the Data tab only; the overview does not repeat them
    for text in ("Dataset: Q1", "Target: v", "Filter: region = DE", "Test window: 2022-01-01 → 2022-01-31",
                 "Forecast length: 5 steps", f"Data commit: {COMMIT[:12]}"):
        assert text not in out["labels"]


def test_nav_has_no_separate_leaderboard_item():
    assert "/ui/leaderboard" not in [p for _, p in pages.NAV_ITEMS]
    assert ("Benchmarks", "/ui/benchmarks") in pages.NAV_ITEMS


def test_page_renders_the_requested_benchmark_from_the_database_only(monkeypatch):
    log = []
    out = _render(monkeypatch, board=_board([_entry(), _entry("Q11", "other", mae=2.0)]), rank_log=log)
    assert out["status"] == 200
    assert [r["model"] for r in out["table"].rows] == ["timesfm", "other"]
    assert log[0][0] == 3
    assert "Best entry: timesfm, ranked by interval score." in out["labels"]


def test_unknown_benchmark_shows_not_found(monkeypatch):
    log = []
    out = _render(monkeypatch, row=None, rank_log=log, bid=999)
    assert out["status"] == 200 and log == []
    assert "⚠ Benchmark not found." in out["labels"]


def test_start_run_button_opens_the_workflow_with_the_benchmark(monkeypatch):
    seen = []
    monkeypatch.setattr(leaderboard_page.ui.navigate, "to", lambda target, *a, **k: seen.append(target))

    async def scenario(out, client, find, drain):
        await _click(next(b for b in find(ui.button) if b.props.get("label") == "Start a run from this benchmark"),
                     client)

    _render(monkeypatch, board=_board([_entry()]), after=scenario)
    assert seen == ["/ui/run_workflow/1?benchmark=3"]


def test_old_leaderboard_url_redirects(monkeypatch):
    for query, target in (("?benchmark=3", "/ui/benchmarks/3"), ("", "/ui/benchmarks"), ("?benchmark=x", "/ui/benchmarks")):
        seen = []
        monkeypatch.setattr(leaderboard_page.ui.navigate, "to", lambda t, *a, **k: seen.append(t))

        async def fetch():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                return await http.get("/ui/leaderboard" + query)

        assert asyncio.run(fetch()).status_code == 200
        assert seen == [target]


def test_empty_states_and_login_hint(monkeypatch):
    out = _render(monkeypatch, board=_board([]))
    assert "No runs on this leaderboard yet. Log in to add runs." in out["labels"]
    out = _render(monkeypatch, board=_board([]), logged_in=True)
    assert "No runs on this leaderboard yet." in out["labels"]


def test_database_errors_show_a_label_instead_of_crashing(monkeypatch):
    def boom(benchmark_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(leaderboard_page.bactions, "get", boom)
    monkeypatch.setattr(leaderboard_page, "_is_logged_in", lambda: False)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            return await http.get("/ui/benchmarks/3")

    response = asyncio.run(fetch())
    assert response.status_code == 200
    assert "Could not load the benchmark." in response.text and "db down" not in response.text


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


def test_row_click_opens_the_run_in_a_new_window_without_a_server_round_trip(monkeypatch):
    """The handler runs in the browser (window.open from the click itself), so popup blockers allow it."""
    def inspect(out, client, find, drain):
        listeners = [l for l in out["table"]._event_listeners.values() if l.type == "rowClick"]
        out["js"] = [l.js_handler for l in listeners]
        out["python"] = [l.handler for l in listeners]

    out = _render(monkeypatch, board=_board([_entry()]), after=inspect)
    assert len(out["js"]) == 1 and out["python"] == [None]
    js = out["js"][0]
    assert "window.open" in js and "'_blank'" in js and "/ui/benchmarks/3/runs/" in js and "row.run_qid" in js


def _detail(monkeypatch, qid="Q10", **kw):
    kw.setdefault("board", _board([_entry()]))
    return _render(monkeypatch, path=f"/ui/benchmarks/3/runs/{qid}", **kw)


def test_run_page_shows_link_window_params_pins_and_per_date_rows(monkeypatch):
    def grab(out, client, find, drain):
        out["links"] = [(e.props.get("href"), e.props.get("target")) for e in find(ui.link)]
        out["tables"] = find(ui.table)
        out["charts"] = find(ui.echart)

    out = _detail(monkeypatch, after=grab)
    assert out["status"] == 200
    assert ("https://data.episerve.zib.de/dataset/q10", "_blank") in out["links"]
    assert ("/ui/benchmarks/3", "_self") in out["links"]
    labels = out["labels"]
    assert any(t.startswith("History window:") and "→ 2021-12-31" in t for t in labels)
    assert "history_length: 91" in labels
    assert "timesfm — run Q10" in labels
    assert f"Data: lakeFS commit {COMMIT} · CKAN last modified 2026-10-14T00:00:00Z · scored 2026-10-16 10:00:00" in labels
    detail = out["tables"][-1]
    assert [c["name"] for c in detail.columns] == ["date", "observed", "forecast", "q10", "q90", "error"]
    assert detail.rows[0]["error"] == "1.00"
    series = {s["name"]: s["data"] for s in out["charts"][0].options["series"]}
    assert series["Observed"] == [10.0, 20.0] and series["q10–q90"] == [4.0, 4.0]


def test_run_page_reads_every_entry_not_only_the_best_per_model(monkeypatch):
    log = []
    _detail(monkeypatch, rank_log=log)
    assert log == [(3, "interval_score", False)]


def test_run_page_for_an_unknown_run_says_so(monkeypatch):
    out = _detail(monkeypatch, qid="Q99")
    assert any(t.endswith("Run not found.") for t in out["labels"])


def test_run_page_for_an_unknown_benchmark_says_so(monkeypatch):
    def gone(benchmark_id, rank_by="interval_score", best_per_model=True):
        raise leaderboard_page.actions.NotFound("no")
    out = _detail(monkeypatch, board=lambda bid: gone(bid))
    assert any(t.endswith("Benchmark not found.") for t in out["labels"])


def test_run_page_read_failure_shows_generic_text_and_logs(monkeypatch, caplog):
    def broken(bid):
        raise RuntimeError("db secret")
    with caplog.at_level("ERROR"):
        out = _detail(monkeypatch, board=broken)
    assert any(t.endswith("Could not load the run.") for t in out["labels"])
    assert "db secret" not in out["text"]
    assert "could not load the run" in caplog.text


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
        await drain()
        select = next(e for e in find(ui.select) if e.props.get("label") == "Model run")
        assert list(select.options.values()) == ["timesfm · Q10 · 2026-01-02"] or list(select.options) == ["Q10"]
        out["labels_dialog"] = _labels(find)
        select.value = "Q10"
        await _click(_button(find, "Add"), client)
        await drain()

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert any("chronos (Q11): run status is failed" in t for t in out["labels_dialog"])
    assert added == [(3, "Q10")]


def test_add_run_shows_the_reason_when_scoring_fails(monkeypatch):
    from app.leaderboard.scoring import NotScorable

    monkeypatch.setattr(leaderboard_page.actions, "candidates", lambda bid: [
        {"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None}])

    def refuse(bid, qid):
        raise NotScorable("forecast covers 1 of 2 dates in the test window")

    monkeypatch.setattr(leaderboard_page.actions, "add_run", refuse)

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Add run"), client)
        await drain()
        next(e for e in find(ui.select) if e.props.get("label") == "Model run").value = "Q10"
        await _click(_button(find, "Add"), client)
        await drain()
        out["labels_after"] = _labels(find)

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert any("forecast covers 1 of 2 dates" in t for t in out["labels_after"])


def test_remove_button_on_the_run_page_removes_the_entry_and_returns_to_the_board(monkeypatch):
    removed, went = [], []
    monkeypatch.setattr(leaderboard_page.actions, "remove_entry", lambda bid, eid: removed.append((bid, eid)))
    monkeypatch.setattr(leaderboard_page.ui.navigate, "to", lambda target, *a, **k: went.append(target))

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Remove from leaderboard"), client)
        await drain()

    _detail(monkeypatch, logged_in=True, after=scenario)
    assert removed == [(3, 9)] and went == ["/ui/benchmarks/3"]


def test_anonymous_users_get_no_remove_button_on_the_run_page(monkeypatch):
    def grab(out, client, find, drain):
        out["buttons"] = [b.text for b in find(ui.button)]
    out = _detail(monkeypatch, after=grab)
    assert "Remove from leaderboard" not in out["buttons"]


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
        await drain()
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
        _expired(monkeypatch)
        await _click(_button(find, "Remove from leaderboard"), client)
        await drain()

    _detail(monkeypatch, logged_in=True, after=scenario)
    assert called == [] and notes and notes[-1][1]["type"] == "warning"


def test_unexpected_write_errors_are_generic_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(leaderboard_page.actions, "candidates", lambda bid: [
        {"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None}])

    def boom(bid, qid):
        raise RuntimeError("Can't connect to MySQL server on 'db.internal'")

    monkeypatch.setattr(leaderboard_page.actions, "add_run", boom)

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Add run"), client)
        await drain()
        next(e for e in find(ui.select) if e.props.get("label") == "Model run").value = "Q10"
        await _click(_button(find, "Add"), client)
        await drain()
        out["labels_after"] = _labels(find)

    with caplog.at_level("ERROR"):
        out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert "Could not add the run." in out["labels_after"]
    assert not any("db.internal" in t for t in out["labels_after"])
    assert any("db.internal" in (r.exc_text or "") for r in caplog.records)


def test_add_run_dialog_opens_with_a_loading_label_before_the_runs_arrive(monkeypatch):
    snapshot = {}

    def slow(bid):                       # runs in the worker thread while the dialog is already open
        snapshot["labels"] = _labels(snapshot["find_fn"])
        snapshot["selects"] = [e for e in snapshot["find_fn"](ui.select) if e.props.get("label") == "Model run"]
        return [{"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None}]

    monkeypatch.setattr(leaderboard_page.actions, "candidates", slow)

    async def scenario(out, client, find, drain):
        snapshot["find_fn"] = find
        await _click(_button(find, "Add run"), client)
        await drain()
        out["after"] = _labels(find)
        out["select_after"] = [e for e in find(ui.select) if e.props.get("label") == "Model run"]

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert "Add run" in snapshot["labels"] and "Loading runs…" in snapshot["labels"]
    assert snapshot["selects"] == []
    assert "Loading runs…" not in out["after"] and len(out["select_after"]) == 1
    assert list(out["select_after"][0].options) == ["Q10"]


def test_add_run_dialog_shows_a_generic_error_when_listing_fails_and_stays_usable(monkeypatch, caplog):
    def boom(bid):
        raise RuntimeError("Can't connect to 'doip.internal'")

    monkeypatch.setattr(leaderboard_page.actions, "candidates", boom)

    async def scenario(out, client, find, drain):
        await _click(_button(find, "Add run"), client)
        await drain()
        out["labels_after"] = _labels(find)
        out["has_cancel"] = _button(find, "Cancel") is not None

    with caplog.at_level("ERROR"):
        out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert "Could not list runs." in out["labels_after"] and "Loading runs…" not in out["labels_after"]
    assert not any("doip.internal" in t for t in out["labels_after"]) and out["has_cancel"]
    assert any("doip.internal" in (r.exc_text or "") for r in caplog.records)


# --- tabs: Overview | Data | Evaluation | Leaderboard -------------------------------------------------------------


def _tabs(out):
    tabs = out["find"](ui.tabs)[0]
    names = [(tab.props.get("name"), tab.props.get("label")) for tab in out["find"](ui.tab)]
    return tabs.value, names


def test_page_has_four_tabs_and_opens_on_the_overview(monkeypatch):
    value, names = _tabs(_render(monkeypatch, board=_board([_entry()])))
    assert value == "overview"
    assert names == [("overview", "Overview"), ("data", "Data"), ("evaluation", "Evaluation"),
                     ("leaderboard", "Leaderboard")]


@pytest.mark.parametrize("query,expected", [("?tab=leaderboard", "leaderboard"), ("?tab=data", "data"),
                                            ("?tab=evaluation", "evaluation"), ("?tab=nonsense", "overview"),
                                            ("", "overview")])
def test_the_tab_query_selects_the_open_tab(monkeypatch, query, expected):
    out = _render(monkeypatch, board=_board([_entry()]), path=f"/ui/benchmarks/3{query}")
    assert _tabs(out)[0] == expected


def _panel_of(element):
    node = element
    while node is not None:
        if isinstance(node, ui.tab_panel):
            return node.props.get("name")
        slot = getattr(node, "parent_slot", None)
        node = slot.parent if slot else None
    return None


def test_overview_has_description_and_how_it_works_but_no_actions(monkeypatch):
    row = {**ROW, "description": "Weekly wastewater viral load."}

    def grab(out, client, find, drain):
        out["buttons"] = [(b.props.get("label"), _panel_of(b)) for b in find(ui.button)]
        out["links"] = [(l.text, _panel_of(l)) for l in find(ui.link)]

    out = _render(monkeypatch, row=row, board=_board([_entry()]), after=grab)
    assert "Weekly wastewater viral load." in out["labels"]
    assert "How it works" in out["labels"]
    # no action buttons; the copy icon of the code block has no label
    assert [b for b, panel in out["buttons"] if panel == "overview" and b] == []
    assert not any(text == "Go to the leaderboard" for text, _ in out["links"])
    assert ("Start a run from this benchmark", "leaderboard") in out["buttons"]      # it lives on the leaderboard tab


def test_overview_without_a_description_uses_the_sentence_generated_from_the_definition(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    assert "Forecast of v for region = DE over a test window of 2022-01-01 → 2022-01-31." in out["labels"]


def test_data_tab_lists_the_facts_the_pin_the_input_link_and_the_sql(monkeypatch):
    def grab(out, client, find, drain):
        out["code"] = [e.content for e in find(ui.code)]
        out["links"] = [(e.text, e.props.get("href")) for e in find(ui.link)]
        out["facts"] = [dict((r["property"], r["value"]) for r in t.rows) for t in find(ui.table)
                        if t.props.get("row-key") == "property"]

    out = _render(monkeypatch, board=_board([_entry()]), after=grab)
    facts = out["facts"][0]
    assert facts["Data commit"] == COMMIT and facts["Filter"] == "region = DE" and facts["Dataset"] == "Q1"
    assert "SELECT 1 FROM df" in out["code"]
    url = f"https://doip.example/doip/retrieve/Q1/d.parquet?version={COMMIT}"
    assert (url, url) in out["links"]


def test_data_tab_survives_a_missing_run_spec(monkeypatch):
    def grab(out, client, find, drain):
        out["code"] = [e.content for e in find(ui.code)]
        out["facts"] = [t for t in find(ui.table) if t.props.get("row-key") == "property"]

    out = _render(monkeypatch, board=_board([_entry()]), spec=RuntimeError("db down"), after=grab)
    assert out["status"] == 200 and len(out["facts"]) == 1
    assert not any("SELECT" in c for c in out["code"])
    assert "The input URL and SQL are not available right now." in out["labels"]


def test_evaluation_tab_documents_metrics_ranking_and_rules_without_repeating_how_to_run(monkeypatch):
    def grab(out, client, find, drain):
        out["metrics"] = [t.rows for t in find(ui.table) if t.props.get("row-key") == "metric"]
        out["code"] = [(e.content, _panel_of(e)) for e in find(ui.code)]

    out = _render(monkeypatch, board=_board([_entry()]), after=grab)
    names = [r["metric"] for r in out["metrics"][0]]
    assert names == ["MAE", "RMSE", "80% coverage", "interval score"]
    assert out["metrics"][0][2]["better"] == "closest to 80%"
    labels = out["labels"]
    assert any("Ranking" in t for t in labels) and any("Which runs are accepted" in t for t in labels)
    assert any("filter region = DE" in t for t in labels)
    cli = [panel for content, panel in out["code"] if "episerve benchmark run-spec 3" in content]
    assert cli == ["overview"]                                  # the CLI steps are shown once, in "How it works"
    assert "scored once" not in " ".join(t for t in labels if t.startswith("2. "))   # step 2 does not repeat Evaluation


def test_leaderboard_table_has_date_and_run_columns_and_a_baseline_badge(monkeypatch):
    def grab(out, client, find, drain):
        out["slots"] = sorted(out["table"].slots)

    out = _render(monkeypatch, board=_board([_entry(), _entry("Q11", "x__baseline-naive", mae=9.0)]), after=grab)
    columns = [c["name"] for c in out["table"].columns]
    assert columns[:5] == ["rank", "model", "run", "date", "params"]
    assert next(c for c in out["table"].columns if c["name"] == "date")["sortable"] is True
    assert "body-cell-model" in out["slots"] and "body-cell-run" in out["slots"]
    assert "\\u200b" in str(out["table"].slots["body-cell-model"].template)      # may wrap after '__' only
    assert [r["baseline"] for r in out["table"].rows] == [False, True]


def test_the_run_cell_links_to_the_run_page_in_a_new_window(monkeypatch):
    def grab(out, client, find, drain):
        out["slot"] = str(out["table"].slots["body-cell-run"].template)

    out = _render(monkeypatch, board=_board([_entry()]), after=grab)
    assert "/ui/benchmarks/3/runs/" in out["slot"] and "_blank" in out["slot"] and "@click.stop" in out["slot"]


def test_the_table_is_paginated(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    assert out["table"].pagination["rowsPerPage"] == 25
    assert out["table"].props[":rows-per-page-options"] == "[10, 25, 50, 0]"      # bound, i.e. an array in the browser


def test_search_filters_rows_and_survives_a_rank_by_change(monkeypatch):
    async def scenario(out, client, find, drain):
        search = next(e for e in find(ui.input) if e.props.get("label") == "Search")
        search.value = "other"
        await drain()
        next(e for e in find(ui.select) if e.props.get("label") == "Rank by").value = "mae"
        await drain()
        out["rows_after"] = [r["model"] for t in find(ui.table) if t.props.get("row-key") == "run_qid" for r in t.rows]
        out["labels_after"] = [e.text for e in find(ui.label)]

    out = _render(monkeypatch, board=_board([_entry(), _entry("Q11", "other", mae=2.0)]), after=scenario)
    assert out["rows_after"] == ["other"]
    assert "Showing 1 of 2 entries" in out["labels_after"]
