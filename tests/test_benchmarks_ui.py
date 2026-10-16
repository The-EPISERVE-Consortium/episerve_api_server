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


best_log: list = []


def default_best(ids):
    best_log.append(ids)
    return {5: {"model_name": "timesfm", "metric": "interval_score", "value": 4.5}} if 5 in ids else {}


def _render(monkeypatch, *, finals=(FINAL,), drafts=(DRAFT,), logged_in=False, after=None, draft_log=None, best=None):
    def list_drafts():
        if draft_log is not None:
            draft_log.append(1)
        return list(drafts)

    def setup():
        monkeypatch.setattr(benchmarks_page.bactions, "list_final", lambda: list(finals))
        monkeypatch.setattr(benchmarks_page.bactions, "list_drafts", list_drafts)
        monkeypatch.setattr(benchmarks_page.lboard_actions, "best_entries", best or default_best)

    return render(monkeypatch, benchmarks_page, "/ui/benchmarks", logged_in=logged_in, setup=setup, after=after)


def _retire_buttons(find):
    return [b for b in find(ui.button) if b.props.get("label") == "Retire"]


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
    assert "Dataset: Q1" in text and "Target: v" in text
    assert "Filter: region = DE" in text and "Test window: 2022-01-01 → 2022-01-31" in text
    assert "Entries: 3" in text and f"Data commit: {COMMIT[:12]}" in text
    assert COMMIT not in " ".join(text)
    assert any(t.startswith("Input range: first date") for t in text)
    links = {e.props.get("href"): e.text for e in out["find"](ui.link)}
    hrefs = {e.text: e.props.get("href") for e in out["find"](ui.link) if "/ui/benchmarks/5" in (e.props.get("href") or "")}
    assert hrefs == {"Flu 2022": "/ui/benchmarks/5", "Leaderboard": "/ui/benchmarks/5?tab=leaderboard"}
    assert "Best: timesfm (interval score 4.50)" in text


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
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: called.append("check"))

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "New benchmark"), client)
        _fill(find, **{"Test start (C)": "not-a-date"})
        await click(button(find, "Save"), client)
        await drain()
        out["bad"] = labels(find)
        _fill(find, **{"Test start (C)": "2022-01-01", "History end (blank = last date before C)": "31.12.2021"})
        await click(button(find, "Check against data"), client)
        await drain()
        out["bad2"] = labels(find)

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert called == []
    assert "Enter test start as YYYY-MM-DD." in out["bad"]
    assert "Enter history end as YYYY-MM-DD." in out["bad2"]


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
    monkeypatch.setattr(benchmarks_page.bactions, "finalize", lambda i, expected_commit=None: done.append((i, expected_commit)))
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
    assert (f"Freeze this benchmark? It will be pinned to data commit {COMMIT[:12]} "
            "(the commit just checked) and can no longer be changed." in out["confirm"])
    assert done == [(7, COMMIT)] and navigated == ["/ui/benchmarks"]


def test_finalize_and_retire_and_copy_call_the_actions_and_recheck_the_login(monkeypatch):
    calls = []
    after_expiry = []
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())
    monkeypatch.setattr(benchmarks_page.bactions, "finalize", lambda i, expected_commit=None: calls.append(("finalize", i)))
    monkeypatch.setattr(benchmarks_page.bactions, "retire", lambda i: calls.append(("retire", i)))
    monkeypatch.setattr(benchmarks_page.bactions, "copy_as_draft", lambda i: calls.append(("copy", i)) or 11)
    notes = _notes(monkeypatch)
    monkeypatch.setattr(benchmarks_page.ui.navigate, "to", lambda url: None)
    monkeypatch.setattr(benchmarks_page.ui.navigate, "reload", lambda: None)

    def expire():
        monkeypatch.setattr(benchmarks_page, "_is_logged_in", lambda: False)
        monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: after_expiry.append("update"))
        monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: after_expiry.append("check"))
        monkeypatch.setattr(benchmarks_page.bactions, "create_draft", lambda **kw: after_expiry.append("create"))

    def expired_note():
        assert notes and notes[-1][1]["type"] == "warning" and "expired" in notes[-1][0]
        notes.clear()

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        # copy and retire work while logged in
        await click(button(find, "Copy as draft"), client)
        await drain()
        await click(_retire_buttons(find)[-1], client)           # the final benchmark's (drafts come first)
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
    assert calls == [("copy", 5), ("retire", 5)] and after_expiry == []


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


def _io_hook(monkeypatch, hook):
    """Make run.io_bound run the function inline, then call ``hook`` (simulates edits during the call)."""
    async def io_bound(fn, *args, **kw):
        result = fn(*args, **kw)
        hook()
        return result

    monkeypatch.setattr(benchmarks_page.run, "io_bound", io_bound)


def test_an_edit_during_a_pending_check_keeps_finalize_disabled(monkeypatch):
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        calls = {"n": 0}

        def edit_on_check_result():
            calls["n"] += 1
            if calls["n"] == 2:                      # save is call 1, check is call 2
                _inputs(find)["Name"].value = "edited meanwhile"

        _io_hook(monkeypatch, edit_on_check_result)
        await click(button(find, "Check against data"), client)
        await drain()
        out["enabled"] = button(find, "Finalize").enabled
        out["labels"] = labels(find)

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert out["enabled"] is False
    assert "The form changed during the check; check again." in out["labels"]
    assert f"Data commit: {COMMIT}" not in out["labels"]


def test_a_failing_second_check_leaves_finalize_disabled(monkeypatch):
    results = [_report(), RuntimeError("db down")]

    def check(i):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", check)

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        await click(button(find, "Check against data"), client)
        await drain()
        out["first"] = button(find, "Finalize").enabled
        await click(button(find, "Check against data"), client)
        await drain()
        out["second"] = button(find, "Finalize").enabled

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert (out["first"], out["second"]) == (True, False)


def test_finalize_conflict_shows_its_message_and_the_flow_stays_usable(monkeypatch):
    from app.benchmarks.errors import Conflict
    outcomes = [Conflict("The data changed since the check (checked a, now b); check again."), None]
    seen = []

    def finalize(i, expected_commit=None):
        seen.append(expected_commit)
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())
    monkeypatch.setattr(benchmarks_page.bactions, "finalize", finalize)
    navigated = []
    monkeypatch.setattr(benchmarks_page.ui.navigate, "to", lambda url: navigated.append(url))

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        await click(button(find, "Check against data"), client)
        await drain()
        await click(button(find, "Finalize"), client)
        await click(button(find, "Freeze benchmark"), client)
        await drain()
        out["after_conflict"] = labels(find)
        out["enabled"] = button(find, "Check against data").enabled
        await click(button(find, "Check against data"), client)
        await drain()
        await click(button(find, "Finalize"), client)
        await click(button(find, "Freeze benchmark"), client)
        await drain()

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert "The data changed since the check (checked a, now b); check again." in out["after_conflict"]
    assert out["enabled"] and seen == [COMMIT, COMMIT] and navigated == ["/ui/benchmarks"]


def test_finalize_navigates_without_a_competing_reload(monkeypatch):
    reloads = []
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())
    monkeypatch.setattr(benchmarks_page.bactions, "finalize", lambda i, expected_commit=None: None)
    monkeypatch.setattr(benchmarks_page.ui.navigate, "to", lambda url: None)
    monkeypatch.setattr(benchmarks_page.ui.navigate, "reload", lambda: reloads.append(1))

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        await click(button(find, "Check against data"), client)
        await drain()
        await click(button(find, "Finalize"), client)
        await click(button(find, "Freeze benchmark"), client)
        await drain()

    _render(monkeypatch, logged_in=True, after=scenario)
    assert reloads == []


def test_conflict_and_notdraft_on_save_show_their_message(monkeypatch):
    from app.benchmarks.errors import Conflict, NotDraft
    errors = [NotDraft("Benchmark 7 is final or retired and cannot be changed."), Conflict("changed")]

    def update(i, **kw):
        raise errors.pop(0)

    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", update)

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        await click(button(find, "Save"), client)
        await drain()
        out["first"] = labels(find)
        await click(button(find, "Save"), client)
        await drain()
        out["second"] = labels(find)

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert "Benchmark 7 is final or retired and cannot be changed." in out["first"]
    assert "changed" in out["second"]


def test_notdraft_on_finalize_shows_its_message(monkeypatch):
    from app.benchmarks.errors import NotDraft

    def finalize(i, expected_commit=None):
        raise NotDraft("Benchmark 7 is final or retired and cannot be changed.")

    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())
    monkeypatch.setattr(benchmarks_page.bactions, "finalize", finalize)

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        await click(button(find, "Check against data"), client)
        await drain()
        await click(button(find, "Finalize"), client)
        await click(button(find, "Freeze benchmark"), client)
        await drain()
        out["labels"] = labels(find)

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert "Benchmark 7 is final or retired and cannot be changed." in out["labels"]


def _boom(*a, **kw):
    raise RuntimeError("Can't connect to MySQL server on 'db.internal'")


def test_retire_and_copy_error_branches(monkeypatch, caplog):
    notes = _notes(monkeypatch)
    monkeypatch.setattr(benchmarks_page.ui.navigate, "reload", lambda: None)
    steps = {}

    async def scenario(out, client, find, drain):
        # NotFound on retire shows its message in the dialog
        monkeypatch.setattr(benchmarks_page.bactions, "retire", lambda i: (_ for _ in ()).throw(NotFound("gone")))
        await click(_retire_buttons(find)[-1], client)
        await click(button(find, "Retire benchmark"), client)
        await drain()
        steps["nf"] = labels(find)
        monkeypatch.setattr(benchmarks_page.bactions, "retire", _boom)
        await click(button(find, "Retire benchmark"), client)
        await drain()
        steps["generic"] = labels(find)
        # copy: NotFound then generic
        monkeypatch.setattr(benchmarks_page.bactions, "copy_as_draft",
                            lambda i: (_ for _ in ()).throw(NotFound("gone too")))
        await click(button(find, "Copy as draft"), client)
        await drain()
        steps["copy_nf"] = list(notes)
        notes.clear()
        monkeypatch.setattr(benchmarks_page.bactions, "copy_as_draft", _boom)
        await click(button(find, "Copy as draft"), client)
        await drain()
        steps["copy_generic"] = list(notes)

    with caplog.at_level("ERROR"):
        _render(monkeypatch, logged_in=True, after=scenario)
    assert "gone" in steps["nf"]
    assert "Could not retire the benchmark." in steps["generic"]
    assert steps["copy_nf"] == [("gone too", {"type": "negative"})]
    assert steps["copy_generic"] == [("Could not copy the benchmark.", {"type": "negative"})]
    assert not any("db.internal" in t for t in steps["generic"])
    assert sum("db.internal" in (r.exc_text or "") for r in caplog.records) == 2


def test_a_draft_can_be_retired_after_a_confirmation_and_the_login_is_rechecked(monkeypatch):
    calls = []
    monkeypatch.setattr(benchmarks_page.bactions, "retire", lambda i: calls.append(i))
    monkeypatch.setattr(benchmarks_page.ui.navigate, "reload", lambda: None)
    notes = _notes(monkeypatch)

    async def scenario(out, client, find, drain):
        draft_retire = _retire_buttons(find)[0]                  # drafts are listed before final benchmarks
        await click(draft_retire, client)
        await drain()
        out["confirm_text"] = labels(find)
        assert calls == []
        monkeypatch.setattr(benchmarks_page, "_is_logged_in", lambda: False)
        await click(button(find, "Retire draft"), client)
        await drain()
        out["blocked"] = list(calls)
        monkeypatch.setattr(benchmarks_page, "_is_logged_in", lambda: True)
        await click(button(find, "Retire draft"), client)
        await drain()

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert "Retire this draft? It disappears from the list." in out["confirm_text"]
    assert out["blocked"] == [] and notes[-1][1]["type"] == "warning"
    assert calls == [7]


def test_an_edit_during_the_save_of_a_check_keeps_finalize_disabled(monkeypatch):
    monkeypatch.setattr(benchmarks_page.bactions, "update_draft", lambda i, **kw: None)
    monkeypatch.setattr(benchmarks_page.bactions, "check", lambda i: _report())

    async def scenario(out, client, find, drain):
        _datasets(monkeypatch)
        await click(button(find, "Edit"), client)
        calls = {"n": 0}

        def edit_during_save():
            calls["n"] += 1
            if calls["n"] == 1:                      # the save is the first io_bound call
                _inputs(find)["Name"].value = "typed while saving"

        _io_hook(monkeypatch, edit_during_save)
        await click(button(find, "Check against data"), client)
        await drain()
        out["enabled"] = button(find, "Finalize").enabled
        out["labels"] = labels(find)

    out = _render(monkeypatch, logged_in=True, after=scenario)
    assert out["enabled"] is False
    assert f"Data commit: {COMMIT}" not in out["labels"]


def test_benchmark_without_entries_shows_no_best_and_skips_the_query(monkeypatch):
    best_log.clear()
    out = _render(monkeypatch, finals=({**FINAL, "entry_count": 0},))
    assert "Best: —" in out["labels"] and best_log == [[]]


def test_best_query_failure_does_not_break_the_list(monkeypatch):
    def boom(ids):
        raise RuntimeError("db")

    out = _render(monkeypatch, best=boom)
    assert out["status"] == 200 and "Best: —" in out["labels"]
    assert "Flu 2022" in [e.text for e in out["find"](ui.link)]
