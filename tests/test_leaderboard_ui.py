"""The stored leaderboard page: reads only, no external calls."""

import asyncio
from datetime import date

import pytest
from nicegui import events, ui

from app.leaderboard.entries import Benchmark
from app.leaderboard.service import Entry, Leaderboard
from app.ui import leaderboard_page
from tests.ui_harness import click, render

BENCH = Benchmark("Q1", "v", "datum", date(2022, 1, 1), date(2022, 1, 31))
COMMIT = "0123abcd" * 8
ROW = {"id": 3, "name": "Flu", "dataset_qid": "Q1", "target_column": "v", "date_column": "datum",
       "filters": '[["region", "DE"]]', "test_start": date(2022, 1, 1),
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
    assert log[0] == (3, "mae", False)      # ranked by MAE, every run is read; the top ones are cut in the page
    assert "Best entry: timesfm, ranked by interval score." in out["labels"]


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


CANDIDATES = [
    {"qid": "Q10", "model_name": "timesfm", "run_timestamp": "2026-01-02T00:00:00Z", "reason": None,
     "docker_tag": "v2", "params": {"history_length": 91}, "history_start": None,
     "history_end": date(2025, 12, 31)},
    {"qid": "Q11", "model_name": "chronos", "run_timestamp": "2026-01-01T00:00:00Z",
     "reason": "run status is failed, not success"},
    {"qid": "Q12", "model_name": "chronos", "run_timestamp": "2026-01-01T00:00:00Z",
     "reason": "already on this leaderboard"},
]


def _candidates(monkeypatch, found=CANDIDATES):
    monkeypatch.setattr(leaderboard_page.actions, "candidates", lambda bid: found)


async def _load_candidates(find):
    """Run the timer that fills the candidates table once the page is shown."""
    for timer in find(ui.timer):
        res = timer.callback()
        if asyncio.iscoroutine(res):
            await res


def _candidate_table(find):
    return next((t for t in find(ui.table) if t.props.get("row-key") == "qid"), None)


async def _add_selected(find, client, *qids):
    """Select the rows with these QIDs in the candidates table and press 'Add selected'."""
    table = _candidate_table(find)
    table.selected = [r for r in table.rows if r["qid"] in qids]
    await _click(_button(find, "Add selected"), client)


def test_anonymous_users_see_no_management_controls(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    labels = {b.props.get("label") for b in out["find"](ui.button)}
    assert not labels & {"New benchmark", "Retire benchmark"}


def test_candidates_table_lists_only_fitting_runs_with_checkboxes_and_two_buttons(monkeypatch):
    _candidates(monkeypatch)

    async def grab(out, client, find, drain):
        await _load_candidates(find)
        out["labels"] = _labels(find)
        table = _candidate_table(find)
        out["rows"] = [dict(r) for r in table.rows]
        out["selection"] = table.props["selection"]
        out["buttons"] = [(b.props.get("label") or b.text) for b in find(ui.button)]
        out["slots"] = set(table.slots)

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=grab)
    assert "Potential candidates for this leaderboard" in out["labels"]
    assert out["rows"] == [{"qid": "Q10", "model": "timesfm", "tag": "v2", "date": "2026-01-02",
                            "params": "history_length=91", "history": "… → 2025-12-31"}]  # failed / added: not listed
    assert out["selection"] == "multiple"
    assert "Select all" in out["buttons"] and "Add selected" in out["buttons"]
    assert "Add to leaderboard" not in out["buttons"] and "body-cell-add" not in out["slots"]


def test_select_all_selects_every_row_and_shows_the_count(monkeypatch):
    two = [dict(CANDIDATES[0]), dict(CANDIDATES[0], qid="Q13")]
    _candidates(monkeypatch, two)

    async def scenario(out, client, find, drain):
        await _load_candidates(find)
        out["before"] = _button(find, "Add selected").enabled
        await _click(_button(find, "Select all"), client)
        out["selected"] = [r["qid"] for r in _candidate_table(find).selected]
        out["after"] = (_button(find, "Add selected (2)") or _button(find, "Add selected")).enabled

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert out["before"] is False and out["after"] is True
    assert out["selected"] == ["Q10", "Q13"]


def test_adding_the_selection_scores_each_run_and_reports_the_ones_that_were_refused(monkeypatch):
    from app.leaderboard.scoring import NotScorable

    two = [dict(CANDIDATES[0]), dict(CANDIDATES[0], qid="Q13")]
    _candidates(monkeypatch, two)
    added = []

    def add(bid, qid):
        if qid == "Q13":
            raise NotScorable("forecast covers 1 of 2 dates in the test window")
        added.append((bid, qid))
        return 42

    monkeypatch.setattr(leaderboard_page.actions, "add_run", add)
    notes = _capture_notify(monkeypatch)

    async def scenario(out, client, find, drain):
        await _load_candidates(find)
        await _add_selected(find, client, "Q10", "Q13")
        await drain()

    _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert added == [(3, "Q10")]
    assert any(kw["type"] == "positive" and "Q10" in msg for msg, kw in notes)
    assert any(kw["type"] == "negative" and "Q13" in msg and "forecast covers 1 of 2 dates" in msg for msg, kw in notes)


def test_candidates_table_says_so_when_there_is_nothing_to_add(monkeypatch):
    _candidates(monkeypatch, [CANDIDATES[1], CANDIDATES[2]])
    async def grab(out, client, find, drain):
        await _load_candidates(find)
        out["labels"] = _labels(find)

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=grab)
    assert "No other matching runs right now." in out["labels"]


def test_anonymous_users_get_no_candidates_and_no_backend_call(monkeypatch):
    monkeypatch.setattr(leaderboard_page.actions, "candidates",
                        lambda bid: (_ for _ in ()).throw(AssertionError("must not be called")))
    out = _render(monkeypatch, board=_board([_entry()]))
    assert "Potential candidates for this leaderboard" not in out["labels"]


def _capture_notify(monkeypatch):
    notes = []
    monkeypatch.setattr(leaderboard_page.ui, "notify", lambda message, **kw: notes.append((message, kw)))
    return notes


def _expired(monkeypatch):
    monkeypatch.setattr(leaderboard_page, "_is_logged_in", lambda: False)


def test_expired_login_blocks_add_run(monkeypatch):
    _candidates(monkeypatch)
    called = []
    monkeypatch.setattr(leaderboard_page.actions, "add_run", lambda bid, qid: called.append(qid) or 1)
    notes = _capture_notify(monkeypatch)

    async def scenario(out, client, find, drain):
        await _load_candidates(find)
        _expired(monkeypatch)
        await _add_selected(find, client, "Q10")
        await drain()

    _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert called == [] and notes and notes[-1][1]["type"] == "warning" and "expired" in notes[-1][0]


def test_unexpected_write_errors_are_generic_and_logged(monkeypatch, caplog):
    _candidates(monkeypatch)

    def boom(bid, qid):
        raise RuntimeError("Can't connect to MySQL server on 'db.internal'")

    monkeypatch.setattr(leaderboard_page.actions, "add_run", boom)
    notes = _capture_notify(monkeypatch)

    async def scenario(out, client, find, drain):
        await _load_candidates(find)
        await _add_selected(find, client, "Q10")
        await drain()

    with caplog.at_level("ERROR"):
        _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert notes[-1][0] == "Q10: could not be added" and "db.internal" not in notes[-1][0]
    assert any("db.internal" in (r.exc_text or "") for r in caplog.records)


# --- tabs: Overview | Data | Evaluation | Leaderboard -------------------------------------------------------------


def test_data_tab_shows_the_file_link_and_the_sql_for_the_training_and_the_evaluation_data(monkeypatch):
    def grab(out, client, find, drain):
        out["code"] = [(e.content, _panel_of(e)) for e in find(ui.code)]
        out["links"] = [(e.text, e.props.get("href")) for e in find(ui.link)]

    out = _render(monkeypatch, board=_board([_entry()]), after=grab)
    url = f"https://doip.example/doip/retrieve/Q1/d.parquet?version={COMMIT}"
    assert (url, url) in out["links"]
    sql = [content for content, panel in out["code"] if panel == "data"]
    assert len(sql) == 2
    assert "SELECT 1 FROM df" in sql[0]                                        # the training data: the benchmark's input SQL
    assert "DATE '2022-01-01'" in sql[1] and "DATE '2022-01-31'" in sql[1]    # the evaluation data: the test window


def test_data_tab_survives_a_missing_run_spec(monkeypatch):
    def grab(out, client, find, drain):
        out["code"] = [e.content for e in find(ui.code)]
        out["metrics"] = [t for t in find(ui.table) if t.props.get("row-key") == "metric"]

    out = _render(monkeypatch, board=_board([_entry()]), spec=RuntimeError("db down"), after=grab)
    assert out["status"] == 200 and len(out["metrics"]) == 1
    assert len(out["code"]) == 1 and "DATE '2022-01-01'" in out["code"][0]        # the evaluation SQL needs no run spec
    assert "The data file link and the training SQL are not available right now." in out["labels"]


def test_data_tab_has_only_the_data_and_the_evaluation_criteria(monkeypatch):
    def grab(out, client, find, drain):
        out["tables"] = {t.props.get("row-key"): _panel_of(t) for t in find(ui.table)}

    out = _render(monkeypatch, board=_board([_entry()]), after=grab)
    for heading in ("Training data", "Evaluation data", "Evaluation criteria"):
        assert heading in out["labels"]
    assert out["tables"]["metric"] == "data" and "property" not in out["tables"]        # no fact tables
    assert not any(t in out["labels"] for t in ("Ranking", "Which runs are accepted", "Metrics"))


# --- three tabs: Overview (default), Data and Leaderboard ----------------------------------------------------


def _panel_of(element):
    node = element
    while node is not None:
        if isinstance(node, ui.tab_panel):
            return node.props.get("name")
        slot = getattr(node, "parent_slot", None)
        node = slot.parent if slot else None
    return None


def test_page_has_overview_data_and_leaderboard_tabs_and_opens_on_the_overview(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    assert [t.props.get("name") for t in out["find"](ui.tab)] == ["overview", "data", "leaderboard"]
    assert out["find"](ui.tabs)[0].value == "overview"


@pytest.mark.parametrize("query,expected", [("?tab=leaderboard", "leaderboard"), ("?tab=evaluation", "data"),
                                            ("?tab=get-started", "overview"), ("?tab=run", "leaderboard")])
def test_the_query_picks_the_tab_and_former_names_map_to_the_new_ones(monkeypatch, query, expected):
    out = _render(monkeypatch, board=_board([_entry()]), path=f"/ui/benchmarks/3{query}")
    assert out["find"](ui.tabs)[0].value == expected


def test_overview_has_the_general_facts_in_a_table_but_no_how_to_run(monkeypatch):
    row = {**ROW, "description": "Weekly wastewater viral load.", "entry_count": 4}

    def grab(out, client, find, drain):
        out["code"] = [_panel_of(e) for e in find(ui.code)]
        out["buttons"] = [(b.props.get("label"), _panel_of(b)) for b in find(ui.button)]
        out["facts"] = next(t for t in find(ui.table) if t.props.get("row-key") == "caption")

    out = _render(monkeypatch, row=row, board=_board([_entry()]), after=grab)
    assert "Weekly wastewater viral load." in out["labels"]
    assert [(r["caption"], r["value"]) for r in out["facts"].rows] == [
        ("Series", "v · region = DE"), ("Forecast horizon", "5 steps"), ("Runs", "4")]
    assert "overview" not in out["code"]
    assert [b for b, panel in out["buttons"] if panel == "overview" and b] == []


def test_only_an_admin_can_edit_the_description_and_it_is_saved_for_a_final_benchmark(monkeypatch):
    saved = []
    monkeypatch.setattr(leaderboard_page.bactions, "set_description", lambda bid, text: saved.append((bid, text)))

    def edit_buttons(find):
        return [b for b in find(ui.button) if b.props.get("aria-label") == "Edit description"]

    monkeypatch.setattr(leaderboard_page, "_is_admin", lambda: False)
    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True)
    assert edit_buttons(out["find"]) == []

    async def scenario(out, client, find, drain):
        await _click(edit_buttons(find)[0], client)
        next(t for t in find(ui.textarea)).value = "New text"
        await _click(_button(find, "Save"), client)
        await drain()

    monkeypatch.setattr(leaderboard_page, "_is_admin", lambda: True)
    _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=scenario)
    assert saved == [(3, "New text")]


def test_the_header_of_the_page_holds_only_the_title_and_the_api_box(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]))
    assert "Flu" in out["labels"]
    assert "curl http://test/benchmarks/3/leaderboard?rank_by=mae&all_runs=true" in out["labels"]
    assert not any(t.startswith(("Organized by", "Dataset:", "Test window:")) for t in out["labels"])


def test_the_run_page_has_no_remove_button_for_anybody(monkeypatch):
    def grab(out, client, find, drain):
        out["buttons"] = [b.text for b in find(ui.button)]

    out = _detail(monkeypatch, logged_in=True, after=grab)
    assert "Remove from leaderboard" not in out["buttons"]


def test_the_run_page_shows_the_api_box_too(monkeypatch):
    out = _render(monkeypatch, board=_board([_entry()]), path="/ui/benchmarks/3/runs/Q10")
    assert "curl http://test/benchmarks/3/leaderboard?all_runs=true" in out["labels"]


def test_the_leaderboard_tab_has_no_start_button_and_no_command_line_block(monkeypatch):
    def grab(out, client, find, drain):
        out["buttons"] = [b.props.get("label") for b in find(ui.button) if _panel_of(b) == "leaderboard"]
        out["code"] = [e.content for e in find(ui.code) if _panel_of(e) == "leaderboard"]
        out["expansions"] = [e.props.get("label") for e in find(ui.expansion)]

    out = _render(monkeypatch, board=_board([_entry()]), logged_in=True, after=grab)
    assert "Start a run from this benchmark" not in out["buttons"]
    assert out["code"] == [] and out["expansions"] == []
    assert "Add run" not in out["buttons"]                   # runs are added from the candidates table below


def test_column_titles_of_the_leaderboard_have_a_tooltip_with_the_explanation(monkeypatch):
    def grab(out, client, find, drain):
        table = next(t for t in find(ui.table) if t.props.get("row-key") == "run_qid")
        out["slots"] = {name: slot.template for name, slot in table.slots.items() if name.startswith("header-cell-")}

    out = _render(monkeypatch, board=_board([_entry()]), after=grab)
    assert {"header-cell-mae", "header-cell-rmse", "header-cell-coverage", "header-cell-interval_score",
            "header-cell-history", "header-cell-scored"} <= set(out["slots"])
    assert "<q-tooltip" in out["slots"]["header-cell-mae"] and "Mean absolute error" in out["slots"]["header-cell-mae"]
    assert "{{ props.col.label }}" in out["slots"]["header-cell-mae"]       # the title itself stays


def _five():
    return _board([_entry(f"Q{i}", "timesfm" if i % 2 else "chronos", mae=float(i)) for i in range(1, 6)])


def test_without_show_all_runs_only_the_top_three_are_listed_and_the_page_says_so(monkeypatch):
    def grab(out, client, find, drain):
        out["labels"] = _labels(find)

    out = _render(monkeypatch, board=_five(), after=grab)
    assert [r["run_qid"] for r in out["table"].rows] == ["Q1", "Q2", "Q3"]       # the best MAE first
    assert [r["rank"] for r in out["table"].rows] == [1, 2, 3]
    assert any(t.startswith("The top 3 of 5 runs") for t in out["labels"])


def test_show_all_runs_lists_every_run(monkeypatch):
    async def scenario(out, client, find, drain):
        next(sw for sw in find(ui.switch) if sw.text == "Show all runs").value = True
        await drain()
        table = next(t for t in find(ui.table) if t.props.get("row-key") == "run_qid")
        out["qids"] = [r["run_qid"] for r in table.rows]
        out["labels"] = _labels(find)

    out = _render(monkeypatch, board=_five(), after=scenario)
    assert out["qids"] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert not any(t.startswith("The top 3 of") for t in out["labels"])

