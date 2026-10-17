"""The run workflow starts from a final benchmark (page smoke tests, no network)."""

import json
from types import SimpleNamespace

from nicegui import ui

from app.ui import pages
from tests.ui_harness import button, click, labels, render as _render

# The harness fakes ``module._is_logged_in``; the workflow pages use ``_require_login`` (patched in _setup).
_SHIM = SimpleNamespace(_is_logged_in=None)


def render(monkeypatch, _module, path, **kw):
    return _render(monkeypatch, _SHIM, path, **kw)

COMMIT = "ab" * 32
URL = "https://doip.example/doip/retrieve/Q1791000000001/input.parquet?version=" + "ee" * 32
SQL = ""
SPEC = {"benchmark_id": 3, "dataset_qid": "Q1", "data_commit": COMMIT, "input_data_files": [[URL, "input.parquet"]],
        "data_transformation_sql": [SQL], "input_columns": ["datum", "v"], "config": {"prediction_length": 27, "prediction_offset": 0},
        "test_start": "2026-04-01", "test_end": "2026-09-30"}
ROW = {"id": 3, "name": "Flu", "date_column": "datum", "filters": '[["typ", "a"]]'}
DATASET = {"qid": "Q1", "name": "Wastewater", "data_path": "https://doip.example/doip/retrieve/Q1/d.parquet"}
MODEL = {"name": "m", "docker_image": "img", "docker_tag": "1", "additional_properties": [
    {"name": "prediction_length", "value": 4}, {"name": "history_length", "value": 91}]}


def _fresh_state():
    return {"datasets": [], "model": MODEL, "sql": {}, "config": None, "filenames": {}, "benchmark": None}


def _setup(monkeypatch, state, finals=None, spec=SPEC, get=None):
    def setup():
        monkeypatch.setattr(pages, "_require_login", lambda right=None: True)
        monkeypatch.setattr(pages, "_executor", lambda: "episerve_user_1")
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


def _benchmark_state(sql=SQL, path=URL):
    state = _fresh_state()
    state.update(datasets=[{**DATASET, "data_path": path}], sql={"Q1": sql}, filenames={"Q1": "input.parquet"},
                 config=json.dumps({"prediction_length": 27}))
    state["benchmark"] = {"id": 3, "name": "Flu", "dataset_qid": "Q1", "data_commit": COMMIT, "prediction_length": 27,
                          "test_start": "2026-04-01", "test_end": "2026-09-30", "date_column": "datum",
                          "filters": [["typ", "a"]], "input_uri": URL}
    return state


def _step5(monkeypatch, state, after=None):
    return render(monkeypatch, pages, "/ui/run_workflow/5", setup=_setup(monkeypatch, state), after=after or (lambda *a: None))


def test_step5_accepts_an_sql_selection_and_the_run_is_triggered_with_it(monkeypatch):
    seen, triggered = {}, []
    from app.clients import prefect as prefect_client
    monkeypatch.setattr(prefect_client, "trigger_model_run", lambda **kw: triggered.append(kw) or {"prefect_flow_run_id": "r1", "status": "ok"})
    monkeypatch.setattr(pages.settings, "prefect_api_url", "http://prefect")
    selection = 'SELECT "datum", "v" FROM df'

    async def after(out, client, find, drain):
        seen["labels"] = labels(find)
        await click(button(find, "Trigger Run"), client)
        seen["after"] = [(l.text) for l in find(ui.label)]
        seen["links"] = [(l.props.get("href"), l.text) for l in find(ui.link)]

    _step5(monkeypatch, _benchmark_state(sql=selection), after)
    assert "This run would not qualify for the leaderboard:" not in seen["labels"]
    assert triggered and triggered[0]["input_data_files"][0][0] == URL
    assert triggered[0]["executed_by"] == "episerve_user_1"                    # the pseudonym, never name or email
    assert "Add the run there once it has finished." in seen["after"]
    assert ("/ui/benchmarks/3", "Open the leaderboard") in seen["links"]


def test_step4_overlays_the_horizon_for_declared_parameters(monkeypatch):
    state = _benchmark_state()
    state["config"] = None
    seen = {}

    def after(out, client, find, drain):
        seen["config"] = json.loads(state["config"])

    render(monkeypatch, pages, "/ui/run_workflow/4", setup=_setup(monkeypatch, state), after=after)
    assert seen["config"] == {"prediction_length": 27, "history_length": 91}


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
    assert f"?version={'ee' * 32}" in payload and "input.parquet" in payload


def test_step5_warns_when_the_input_is_not_the_generated_file(monkeypatch):
    seen = {}

    def after(out, client, find, drain):
        seen["labels"] = labels(find)

    _step5(monkeypatch, _benchmark_state(path=DATASET["data_path"]), after)
    assert "This run would not qualify for the leaderboard:" in seen["labels"]
    assert any("is not the benchmark's generated input file" in t for t in seen["labels"])


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


def test_the_workflow_pages_show_the_api_box_for_starting_a_run(monkeypatch):
    seen = {}

    def after(out, client, find, drain):
        seen["labels"] = labels(find)

    _step5(monkeypatch, _benchmark_state(), after)
    assert "curl -X POST http://test/model-runs" in seen["labels"]
