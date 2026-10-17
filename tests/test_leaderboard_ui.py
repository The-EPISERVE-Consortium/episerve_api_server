"""The stored leaderboard page: reads only, no external calls."""

from datetime import date

from nicegui import ui

from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry, Leaderboard
from app.ui import leaderboard_page
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


def test_page_renders_the_requested_benchmark_from_the_database_only(monkeypatch):
    log = []
    out = _render(monkeypatch, board=_board([_entry(), _entry("Q11", "other", mae=2.0)]), rank_log=log)
    assert out["status"] == 200
    assert [r["model"] for r in out["table"].rows] == ["timesfm", "other"]
    assert log[0][0] == 3
    assert "Best entry: timesfm, ranked by interval score." in out["labels"]


def test_start_run_button_opens_the_workflow_with_the_benchmark(monkeypatch):
    seen = []
    monkeypatch.setattr(leaderboard_page.ui.navigate, "to", lambda target, *a, **k: seen.append(target))

    async def scenario(out, client, find, drain):
        await _click(next(b for b in find(ui.button) if b.props.get("label") == "Start a run from this benchmark"),
                     client)

    _render(monkeypatch, board=_board([_entry()]), after=scenario)
    assert seen == ["/ui/run_workflow/1?benchmark=3"]


def test_leaderboard_read_failure_shows_generic_text_and_logs(monkeypatch, caplog):
    def boom(bid, rank_by="interval_score", best_per_model=True):
        raise RuntimeError("Can't connect to MySQL server on 'db.internal'")

    with caplog.at_level("ERROR"):
        out = _render(monkeypatch, board=boom)
    assert "⚠ Could not load the leaderboard." in out["labels"]
    assert not any("db.internal" in t for t in out["labels"])
    assert any("db.internal" in (r.exc_text or "") for r in caplog.records)


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


def test_run_page_read_failure_shows_generic_text_and_logs(monkeypatch, caplog):
    def broken(bid):
        raise RuntimeError("db secret")
    with caplog.at_level("ERROR"):
        out = _detail(monkeypatch, board=broken)
    assert any(t.endswith("Could not load the run.") for t in out["labels"])
    assert "db secret" not in out["text"]
    assert "could not load the run" in caplog.text


def _labels(find):
    return [e.text for e in find(ui.label)]


def _button(find, label):
    return next((b for b in find(ui.button) if b.props.get("label") == label), None)


def test_anonymous_users_see_no_management_controls(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    labels = {b.props.get("label") for b in out["find"](ui.button)}
    assert not labels & {"New benchmark", "Add run", "Retire benchmark"}


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


# --- tabs: Overview | Data | Evaluation | Leaderboard -------------------------------------------------------------


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


# --- two tabs: Leaderboard (default) and Get Started with a section menu ---------------------------------------


def test_page_opens_on_the_leaderboard_and_has_a_get_started_tab_with_sections(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    tabs = out["find"](ui.tabs)
    names = [t.props.get("name") for t in out["find"](ui.tab)]
    assert names == ["leaderboard", "get-started", "overview", "data", "evaluation", "run"]
    assert tabs[0].value == "leaderboard"


def test_a_section_name_in_the_query_opens_get_started_at_that_section(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]), path="/ui/benchmarks/3?tab=evaluation")
    outer, inner = out["find"](ui.tabs)
    assert (outer.value, inner.value) == ("get-started", "evaluation")


def test_the_header_of_the_page_holds_only_the_title_and_the_api_box(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    assert "Flu" in out["labels"]
    assert "curl http://test/benchmarks/3/leaderboard" in out["labels"]
    assert not any(t.startswith(("Organized by", "Dataset:", "Test window:")) for t in out["labels"])


def test_the_run_page_shows_the_api_box_too(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]), path="/ui/benchmarks/3/runs/Q10")
    assert "curl http://test/benchmarks/3/leaderboard?all_runs=true" in out["labels"]
