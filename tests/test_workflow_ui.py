"""The run workflow starts from a final benchmark (page smoke tests, no network)."""

import json
from types import SimpleNamespace

from nicegui import ui

from app.benchmarks.errors import NotFound
from app.ui import pages
from tests.ui_harness import button, click, labels, render as _render

# The harness fakes ``module._is_logged_in``; the workflow pages use ``_require_login`` (patched in _setup).
_SHIM = SimpleNamespace(_is_logged_in=None)


def render(monkeypatch, _module, path, **kw):
    return _render(monkeypatch, _SHIM, path, **kw)

COMMIT = "ab" * 32
URL = f"https://doip.example/doip/retrieve/Q1/d.parquet?version={COMMIT}"
SQL = ("SELECT CAST(\"datum\" AS DATE) AS \"datum\", \"v\" FROM df WHERE \"typ\" = 'a' "
       "AND CAST(\"datum\" AS DATE) <= DATE '2026-03-25' ORDER BY \"datum\"")
SPEC = {"benchmark_id": 3, "dataset_qid": "Q1", "data_commit": COMMIT, "input_data_files": [[URL, "input.parquet"]],
        "data_transformation_sql": [SQL], "config": {"prediction_length": 27, "prediction_offset": 0},
        "test_start": "2026-04-01", "test_end": "2026-09-30"}
ROW = {"id": 3, "name": "Flu", "date_column": "datum", "filter_column": "typ", "filter_value": "a"}
DATASET = {"qid": "Q1", "name": "Wastewater", "data_path": "https://doip.example/doip/retrieve/Q1/d.parquet"}
MODEL = {"name": "m", "docker_image": "img", "docker_tag": "1", "additional_properties": [
    {"name": "prediction_length", "value": 4}, {"name": "history_length", "value": 91}]}


def _fresh_state():
    return {"datasets": [], "model": MODEL, "sql": {}, "config": None, "filenames": {}, "benchmark": None}


def _setup(monkeypatch, state, finals=None, spec=SPEC, get=None):
    def setup():
        monkeypatch.setattr(pages, "_require_login", lambda: True)
        monkeypatch.setattr(pages, "_tv2_state", lambda: state)
        # the handlers persist the state in the per-user storage, which needs a browser session
        monkeypatch.setattr(pages, "_napp", SimpleNamespace(storage=SimpleNamespace(user={})))
        monkeypatch.setattr(pages.ckan_client, "list_processed_datasets", lambda: [dict(DATASET), {"qid": "Q2", "name": "Other"}])
        monkeypatch.setattr(pages.ckan_client, "list_models", lambda *a, **k: [MODEL])
        monkeypatch.setattr(pages.bactions, "list_final", finals or (lambda: [{"id": 3, "name": "Flu"}]))
        monkeypatch.setattr(pages.bactions, "get", get or (lambda i: ROW))
        monkeypatch.setattr(pages.bactions, "run_spec", lambda i: spec if not isinstance(spec, Exception) else (_ for _ in ()).throw(spec))
    return setup


def _notes(monkeypatch):
    notes = []
    monkeypatch.setattr(pages.ui, "notify", lambda message, **kw: notes.append(message))
    return notes


def _use(find, client, benchmark_id=3):
    select = next(s for s in find(ui.select) if s.props.get("label") == "Start from a benchmark")
    select.value = benchmark_id
    return click(button(find, "Use benchmark"), client)


def test_step1_shows_the_benchmark_card_and_use_applies_it(monkeypatch):
    state, notes, seen = _fresh_state(), _notes(monkeypatch), {}

    async def after(out, client, find, drain):
        assert "Start from a benchmark" in labels(find)
        assert not any(t.startswith("Benchmark:") for t in labels(find))
        await _use(find, client)
        await drain()
        seen["labels"] = labels(find)

    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, state), after=after)
    assert state["benchmark"]["id"] == 3
    assert state["datasets"][0]["data_path"] == URL and state["sql"] == {"Q1": SQL}
    assert "Benchmark Flu applied" in notes
    assert f"Benchmark: Flu (data commit {COMMIT[:12]})" in seen["labels"]


def test_step1_has_no_card_when_listing_fails(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    out = render(monkeypatch, pages, "/ui/run_workflow/1",
                 setup=_setup(monkeypatch, _fresh_state(), finals=boom), after=lambda *a: None)
    assert "Start from a benchmark" not in out["labels"]
    assert "Run a Forecast Model" in out["labels"]


def test_step1_shows_not_found_message_and_generic_failure(monkeypatch):
    notes = _notes(monkeypatch)
    state = _fresh_state()

    async def after(out, client, find, drain):
        await _use(find, client)

    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, state, spec=NotFound("Benchmark 3 is not final")), after=after)
    assert notes == ["Benchmark 3 is not final"] and state["benchmark"] is None
    notes.clear()
    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, state, spec=RuntimeError("secret detail")), after=after)
    assert notes == ["Could not apply the benchmark"]


def test_choosing_a_dataset_by_hand_clears_the_benchmark(monkeypatch):
    state = _fresh_state()
    state["benchmark"] = _benchmark_state()["benchmark"]

    async def after(out, client, find, drain):
        card = next(e for e in find(ui.element) if any(
            l.text == "Other" for l in e.descendants() if isinstance(l, ui.label)) and "cursor-pointer" in e.classes)
        await click(card, client)

    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, state), after=after)
    assert state["benchmark"] is None and [d["qid"] for d in state["datasets"]] == ["Q2"]


def _benchmark_state(sql=SQL, path=URL):
    state = _fresh_state()
    state.update(datasets=[{**DATASET, "data_path": path}], sql={"Q1": sql}, filenames={"Q1": "input.parquet"},
                 config=json.dumps({"prediction_length": 27}))
    state["benchmark"] = {"id": 3, "name": "Flu", "dataset_qid": "Q1", "data_commit": COMMIT, "prediction_length": 27,
                          "test_start": "2026-04-01", "test_end": "2026-09-30", "date_column": "datum",
                          "filter_column": "typ", "filter_value": "a"}
    return state


def _step5(monkeypatch, state, after=None):
    return render(monkeypatch, pages, "/ui/run_workflow/5", setup=_setup(monkeypatch, state), after=after or (lambda *a: None))


def test_step5_shows_banner_payload_and_no_warning(monkeypatch):
    seen = {}

    def after(out, client, find, drain):
        seen["labels"] = labels(find)

    _step5(monkeypatch, _benchmark_state(), after)
    texts = seen["labels"]
    assert f"Benchmark: Flu (data commit {COMMIT[:12]})" in texts
    assert not any("would not qualify" in t for t in texts)
    assert any(f"?version={COMMIT}" in t and SQL.replace('"', '\\"') in t for t in texts)
    assert not any(t.endswith(f"d.parquet?version={COMMIT}") for t in texts)
    assert "d.parquet" in texts


def test_step5_warns_when_the_sql_leaks_and_the_run_can_still_be_triggered(monkeypatch):
    seen, triggered = {}, []
    from app.clients import prefect as prefect_client
    monkeypatch.setattr(prefect_client, "trigger_model_run", lambda **kw: triggered.append(kw) or {"prefect_flow_run_id": "r1", "status": "ok"})
    monkeypatch.setattr(pages.settings, "prefect_api_url", "http://prefect")
    leaky = SQL.replace("2026-03-25", "2026-04-10")

    async def after(out, client, find, drain):
        seen["labels"] = labels(find)
        await click(button(find, "Trigger Run"), client)
        seen["after"] = [(l.text) for l in find(ui.label)]
        seen["links"] = [(l.props.get("href"), l.text) for l in find(ui.link)]

    _step5(monkeypatch, _benchmark_state(sql=leaky), after)
    assert "This run would not qualify for the leaderboard:" in seen["labels"]
    assert any("test start" in t for t in seen["labels"])
    assert triggered and triggered[0]["input_data_files"][0][0] == URL
    assert "Add the run there once it has finished." in seen["after"]
    assert ("/ui/benchmarks/3", "Open the leaderboard") in seen["links"]


def test_step5_without_benchmark_is_unchanged(monkeypatch):
    state = _fresh_state()
    state.update(datasets=[{**DATASET, "data_path": DATASET["data_path"]}], config="{}")
    seen = {}

    def after(out, client, find, drain):
        seen["labels"] = labels(find)

    _step5(monkeypatch, state, after)
    assert not any(t.startswith("Benchmark:") or "would not qualify" in t for t in seen["labels"])


def test_step4_overlays_the_horizon_for_declared_parameters(monkeypatch):
    state = _benchmark_state()
    state["config"] = None
    seen = {}

    def after(out, client, find, drain):
        seen["config"] = json.loads(state["config"])

    render(monkeypatch, pages, "/ui/run_workflow/4", setup=_setup(monkeypatch, state), after=after)
    assert seen["config"] == {"prediction_length": 27, "history_length": 91}


def test_summary_shows_benchmark_only_while_active(monkeypatch):
    seen = {}

    def after(out, client, find, drain):
        seen["labels"] = labels(find)

    state = _benchmark_state()
    render(monkeypatch, pages, "/ui/run_workflow/4", setup=_setup(monkeypatch, state), after=after)
    assert f"Benchmark: Flu (data commit {COMMIT[:12]})" in seen["labels"]
    state["benchmark"] = None
    render(monkeypatch, pages, "/ui/run_workflow/4", setup=_setup(monkeypatch, state), after=after)
    assert not any(t.startswith("Benchmark:") for t in seen["labels"])


def test_step1_select_is_seeded_from_the_active_benchmark(monkeypatch):
    seen = {}

    def after(out, client, find, drain):
        seen["values"] = [s.value for s in find(ui.select) if s.props.get("label") == "Start from a benchmark"]

    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, _benchmark_state()), after=after)
    assert seen["values"] == [3]
    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, _fresh_state()), after=after)
    assert seen["values"] == [None]


def test_use_benchmark_then_step5_end_to_end(monkeypatch):
    state, seen = _fresh_state(), {}

    async def use(out, client, find, drain):
        await _use(find, client)

    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, state), after=use)

    def review(out, client, find, drain):
        seen["labels"] = labels(find)

    render(monkeypatch, pages, "/ui/run_workflow/4", setup=_setup(monkeypatch, state),
           after=lambda *a: seen.update(config=json.loads(state["config"])))
    assert seen["config"] == {"prediction_length": 27, "history_length": 91}
    render(monkeypatch, pages, "/ui/run_workflow/5", setup=_setup(monkeypatch, state), after=review)
    texts = seen["labels"]
    assert not any("would not qualify" in t for t in texts)
    payload = next(t for t in texts if "input_data_files" in t)
    assert f"?version={COMMIT}" in payload and "input.parquet" in payload and "CAST" in payload


def test_step5_warns_when_the_input_is_not_at_the_data_commit(monkeypatch):
    seen = {}

    def after(out, client, find, drain):
        seen["labels"] = labels(find)

    _step5(monkeypatch, _benchmark_state(path=DATASET["data_path"]), after)
    assert "This run would not qualify for the leaderboard:" in seen["labels"]
    assert any("is not read at the benchmark's data commit" in t for t in seen["labels"])


def test_clear_all_clears_the_benchmark(monkeypatch):
    state = _benchmark_state()

    async def after(out, client, find, drain):
        assert any(t.startswith("Benchmark:") for t in labels(find))
        await click(button(find, "Clear selection"), client)
        await drain()
        seen["labels"] = labels(find)

    seen = {}
    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, state), after=after)
    assert state["benchmark"] is None and state["datasets"] == []
    assert not any(t.startswith("Benchmark:") for t in seen["labels"])


def test_benchmark_whose_dataset_is_missing_changes_nothing(monkeypatch):
    notes, state = _notes(monkeypatch), _fresh_state()
    spec = {**SPEC, "dataset_qid": "Q404"}

    async def after(out, client, find, drain):
        await _use(find, client)

    render(monkeypatch, pages, "/ui/run_workflow/1", setup=_setup(monkeypatch, state, spec=spec), after=after)
    assert notes == ["The benchmark's dataset is not available"]
    assert state == _fresh_state()


def test_legacy_session_state_gets_a_benchmark_key(monkeypatch):
    legacy = {"datasets": [], "model": None, "sql": {}, "config": None, "filenames": {}}
    monkeypatch.setattr(pages, "_napp", SimpleNamespace(storage=SimpleNamespace(user={"tv2": legacy})))
    assert pages._tv2_state()["benchmark"] is None


def test_step5_blocks_a_config_without_a_required_parameter(monkeypatch):
    triggered = []
    from app.clients import prefect as prefect_client
    monkeypatch.setattr(prefect_client, "trigger_model_run", lambda **kw: triggered.append(kw) or {"prefect_flow_run_id": "r1", "status": "ok"})
    monkeypatch.setattr(pages.settings, "prefect_api_url", "http://prefect")
    notes = _notes(monkeypatch)
    state = _benchmark_state()
    state["model"] = {**MODEL, "additional_properties": [
        {"name": "history_length", "valueRequired": True, "minValue": 3},
        {"name": "prediction_length", "valueRequired": True, "minValue": 1}]}
    state["config"] = json.dumps({"prediction_length": 27})

    async def after(out, client, find, drain):
        await click(button(find, "Trigger Run"), client)

    _step5(monkeypatch, state, after)
    assert triggered == []
    assert any("missing required parameter 'history_length'" in n for n in notes)
