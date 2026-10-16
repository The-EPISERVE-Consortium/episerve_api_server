"""The benchmark designer page: database reads on load, CKAN only when the designer opens."""

from datetime import date

from nicegui import ui

from app.benchmarks.actions import CheckReport
from app.benchmarks.errors import NotFound
from app.ui import benchmarks_page
from tests.ui_harness import button, click, labels, render

COMMIT = "0123456789abcdef" * 4
FINAL = {
    "id": 5, "name": "Flu 2022", "dataset_qid": "Q1", "target_column": "v", "date_column": "datum",
    "filter_column": "region", "filter_value": "DE", "history_start": None, "history_end": None,
    "test_start": date(2022, 1, 1), "test_end": date(2022, 1, 31), "entry_count": 3, "data_commit": COMMIT,
}
DRAFT = {
    "id": 7, "name": "Draft A", "description": "d", "dataset_qid": "Q1", "target_column": "v",
    "date_column": "datum", "filter_column": None, "filter_value": None, "history_start": None,
    "history_end": None, "test_start": date(2022, 2, 1), "test_end": date(2022, 2, 28),
    "input_filename": "input.parquet", "entry_count": 0, "data_commit": None,
}
DATASETS = [{"qid": "Q1", "name": "Wastewater"}]


def _render(monkeypatch, *, finals=(FINAL,), drafts=(DRAFT,), logged_in=False, after=None, draft_log=None):
    def list_drafts():
        if draft_log is not None:
            draft_log.append(1)
        return list(drafts)

    def setup():
        monkeypatch.setattr(benchmarks_page.bactions, "list_final", lambda: list(finals))
        monkeypatch.setattr(benchmarks_page.bactions, "list_drafts", list_drafts)

    return render(monkeypatch, benchmarks_page, "/ui/benchmarks", logged_in=logged_in, setup=setup, after=after)


def _inputs(find):
    return {i.props.get("label"): i for i in find(ui.input)}


def _notes(monkeypatch):
    notes = []
    monkeypatch.setattr(benchmarks_page.ui, "notify", lambda message, **kw: notes.append((message, kw)))
    return notes


def _datasets(monkeypatch):
    monkeypatch.setattr(benchmarks_page.ckan_client, "list_processed_datasets", lambda: DATASETS)


def _fill(find, **extra):
    inputs = _inputs(find)
    values = {"Name": "Flu", "Target column": "v", "Test start (C)": "2022-01-01", "Test end (D)": "2022-01-31"}
    for label, value in {**values, **extra}.items():
        inputs[label].value = value


def _report(**kw):
    base = dict(ok=True, data_commit=COMMIT, truth_points=5, history_end=date(2021, 12, 31),
                prediction_length=5, frequency_days=7, input_sql="SELECT * FROM t")
    return CheckReport(**{**base, **kw})


def test_page_lists_final_benchmarks_from_the_database_only(monkeypatch):
    out = _render(monkeypatch)
    assert out["status"] == 200
    text = out["labels"]
    assert "Flu 2022" in text and "Dataset: Q1" in text and "Target: v" in text
    assert "Filter: region = DE" in text and "Test window: 2022-01-01 → 2022-01-31" in text
    assert "Entries: 3" in text and f"Data commit: {COMMIT[:12]}" in text
    assert COMMIT not in " ".join(text)
    assert any(t.startswith("Input range: first date") for t in text)
    links = {e.props.get("href"): e.text for e in out["find"](ui.link)}
    assert links["/ui/leaderboard?benchmark=5"] == "Leaderboard"


def test_anonymous_users_see_no_management_controls(monkeypatch):
    log = []
    out = _render(monkeypatch, draft_log=log)
    shown = {b.props.get("label") for b in out["find"](ui.button)}
    assert not shown & {"New benchmark", "Edit", "Copy as draft", "Retire"}
    assert "Drafts" not in out["labels"] and "Draft A" not in out["labels"]
    assert log == []


def test_logged_in_users_see_drafts_and_controls(monkeypatch):
    log = []
    out = _render(monkeypatch, logged_in=True, draft_log=log)
    shown = {b.props.get("label") for b in out["find"](ui.button)}
    assert {"New benchmark", "Edit", "Copy as draft", "Retire"} <= shown
    assert "Drafts" in out["labels"] and "Draft A" in out["labels"] and log == [1]


def test_empty_state(monkeypatch):
    out = _render(monkeypatch, finals=(), drafts=(), logged_in=True)
    assert "No benchmarks yet." in out["labels"] and "No drafts." in out["labels"]


def test_designer_saves_a_new_draft_with_parsed_fields(monkeypatch):
    created = []
    monkeypatch.setattr(benchmarks_page.bactions, "create_draft", lambda **kw: created.append(kw) or 9)

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "New benchmark"), client)
        select = next(e for e in find(ui.select) if e.props.get("label") == "Dataset")
        assert list(select.options.values()) == ["Wastewater (Q1)"]
        _fill(find, **{"History start (optional)": "2021-06-01"})
        await click(button(find, "Save"), client)
        await drain()

    _render(monkeypatch, logged_in=True, after=scenario)
    assert created == [dict(
        name="Flu", description=None, dataset_qid="Q1", target_column="v", date_column="datum",
        test_start=date(2022, 1, 1), test_end=date(2022, 1, 31), filter_column=None, filter_value=None,
        history_start=date(2021, 6, 1), history_end=None, input_filename="input.parquet")]


def test_editing_a_draft_updates_it_and_a_second_save_keeps_the_id(monkeypatch):
    calls = []
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: calls.append((i, kw)))

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        inputs = _inputs(find)
        assert inputs["Name"].value == "Draft A" and inputs["Test start (C)"].value == "2022-02-01"
        inputs["Name"].value = "Renamed"
        await click(button(find, "Save"), client)
        await drain()

    _render(monkeypatch, logged_in=True, after=scenario)
    assert calls[0][0] == 7 and calls[0][1]["name"] == "Renamed" and calls[0][1]["test_end"] == date(2022, 2, 28)


def test_designer_rejects_bad_dates_without_calling_the_action(monkeypatch):
    called = []
    monkeypatch.setattr(benchmarks_page.bactions, "create_draft", lambda **kw: called.append(kw) or 9)

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "New benchmark"), client)
        _fill(find, **{"Test start (C)": "not-a-date"})
        await click(button(find, "Save"), client)
        await drain()
        out["bad"] = labels(find)
        _fill(find, **{"History end (blank = last date before C)": "31.12.2021"})
        await click(button(find, "Check against data"), client)
        await drain()
        out["bad2"] = labels(find)

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert called == []
    assert "Enter test start as YYYY-MM-DD." in out["bad"]


def test_check_shows_problems_warnings_and_the_generated_sql(monkeypatch):
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    reports = [_report(ok=False, problems=["column v is missing"], warnings=["uneven dates"], input_sql=None),
               _report()]
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: reports.pop(0))

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        await click(button(find, "Check against data"), client)
        await drain()
        out["first"] = labels(find)
        out["red"] = [e.text for e in find(ui.label) if "text-red-600" in e.classes]
        out["orange"] = [e.text for e in find(ui.label) if "text-orange-600" in e.classes]
        out["finalize_disabled_1"] = not button(find, "Finalize").enabled
        await click(button(find, "Check against data"), client)
        await drain()
        out["second"] = labels(find)

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert "column v is missing" in out["red"] and "uneven dates" in out["orange"]
    assert out["finalize_disabled_1"] is True
    assert "SELECT * FROM t" not in out["first"]
    second = out["second"]
    assert f"Data commit: {COMMIT}" in second and "Observations in the test window: 5" in second
    assert "Forecast length: 5 steps every 7 days" in second and "SELECT * FROM t" in second


def test_finalize_is_disabled_until_a_successful_check_and_confirms_with_the_commit(monkeypatch):
    done = []
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())
    monkeypatch.setattr(benchmarks_page.bactions, "finalize", lambda i: done.append(i))
    navigated = []
    monkeypatch.setattr(benchmarks_page.ui.navigate, "to", lambda url: navigated.append(url))

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        out["before"] = button(find, "Finalize").enabled
        await click(button(find, "Check against data"), client)
        await drain()
        out["after"] = button(find, "Finalize").enabled
        _inputs(find)["Name"].value = "changed"        # editing invalidates the check
        out["edited"] = button(find, "Finalize").enabled
        await click(button(find, "Check against data"), client)
        await drain()
        await click(button(find, "Finalize"), client)
        out["confirm"] = labels(find)
        assert done == []
        await click(button(find, "Freeze benchmark"), client)
        await drain()

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert (out["before"], out["after"], out["edited"]) == (False, True, False)
    assert (f"This freezes the benchmark and pins data commit {COMMIT}. It cannot be changed afterwards."
            in out["confirm"])
    assert done == [7] and navigated == ["/ui/benchmarks"]


def test_finalize_and_retire_and_copy_call_the_actions_and_recheck_the_login(monkeypatch):
    calls = []
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())
    monkeypatch.setattr(benchmarks_page.bactions, "finalize", lambda i: calls.append(("finalize", i)))
    monkeypatch.setattr(benchmarks_page.bactions, "retire", lambda i: calls.append(("retire", i)))
    monkeypatch.setattr(benchmarks_page.bactions, "copy_as_draft", lambda i: calls.append(("copy", i)) or 11)
    notes = _notes(monkeypatch)
    monkeypatch.setattr(benchmarks_page.ui.navigate, "to", lambda url: None)
    monkeypatch.setattr(benchmarks_page.ui.navigate, "reload", lambda: None)

    def expire():
        monkeypatch.setattr(benchmarks_page, "_is_logged_in", lambda: False)

    def expired_note():
        assert notes and notes[-1][1]["type"] == "warning" and "expired" in notes[-1][0]
        notes.clear()

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        # copy and retire work while logged in
        await click(button(find, "Copy as draft"), client)
        await drain()
        await click(button(find, "Retire"), client)
        await click(button(find, "Retire benchmark"), client)
        await drain()
        assert calls == [("copy", 5), ("retire", 5)]
        # finalize works, then fails the re-check once the login expires
        await click(button(find, "Edit"), client)
        await click(button(find, "Check against data"), client)
        await drain()
        await click(button(find, "Finalize"), client)
        expire_after_open = button(find, "Freeze benchmark")
        expire()
        await click(expire_after_open, client)
        await drain()
        expired_note()
        for label in ("Finalize", "Save", "Check against data", "Edit", "New benchmark", "Copy as draft", "Retire",
                      "Retire benchmark"):
            btn = button(find, label)
            if btn is not None:
                await click(btn, client)
                await drain()
                expired_note()

    _render(monkeypatch, logged_in=True, after=scenario)
    assert calls == [("copy", 5), ("retire", 5)]


def test_domain_errors_show_their_message_and_unexpected_ones_are_generic_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: (_ for _ in ()).throw(NotFound("gone")))

    def boom(i):
        raise RuntimeError("Can't connect to MySQL server on 'db.internal'")

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        await click(button(find, "Check against data"), client)
        await drain()
        out["domain"] = labels(find)
        monkeypatch.setattr(benchmarks_page.bactions, "check", boom)
        await click(button(find, "Check against data"), client)
        await drain()
        out["generic"] = labels(find)

    with caplog.at_level("ERROR"):
        out = _render(monkeypatch, logged_in=True, after=scenario)
    assert "gone" in out["domain"]
    assert "Could not check the benchmark." in out["generic"]
    assert not any("db.internal" in t for t in out["generic"])
    assert any("db.internal" in (r.exc_text or "") for r in caplog.records)


def test_database_error_on_load_shows_a_generic_label_and_logs(monkeypatch, caplog):
    def boom():
        raise RuntimeError("db down")

    with caplog.at_level("ERROR"):
        out = render(monkeypatch, benchmarks_page, "/ui/benchmarks",
                     setup=lambda: monkeypatch.setattr(benchmarks_page.bactions, "list_final", boom))
    assert out["status"] == 200
    assert "Could not load benchmarks." in out["text"] and "db down" not in out["text"]
    assert any("db down" in (r.exc_text or "") for r in caplog.records)
