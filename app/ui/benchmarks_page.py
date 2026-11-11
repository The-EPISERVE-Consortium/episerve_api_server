"""The /ui/benchmarks page: list final benchmarks and design, check and finalize drafts."""

import logging
from datetime import date as _date

from nicegui import run, ui
from starlette.requests import Request

from app.ui import session
from app.benchmarks import actions as bactions
from app.benchmarks.definition import DefinitionError, covariates_from_row, filters_from_row, materialized_from_row
from app.clients import ckan as ckan_client
from app.leaderboard import actions as lboard_actions
from app.leaderboard.about import description_text
from app.leaderboard.loaders import LoadError
from app.leaderboard.present import LABELS, model_label
from app.ui.components.messages import error_label
from app.ui.layout import header
from app.ui.theme import BENCHMARK_LIST_CSS

logger = logging.getLogger(__name__)

_LOGIN_EXPIRED = "Your login has expired; please log in again."
MAX_FORM_FILTERS = 5     # only the designer form is limited; the API takes any number
MAX_FORM_COVARIATES = 6
_DOMAIN_ERRORS = (DefinitionError, LoadError, bactions.NotFound, bactions.NotDraft, bactions.Conflict,
                  bactions.Forbidden)


def _is_logged_in() -> bool:
    return session.can("benchmarks")


def _current_user():
    return session.current_user()


def _fmt(value) -> str:
    return "" if value is None else str(value)


def _best_text(best: dict | None) -> str:
    """Format a compact model name and its best score.

    Args:
        best: Best stored entry, or None when no entry has been scored.

    Returns:
        Model and metric summary, or a dash when no score is available.
    """
    if not best:
        return "—"
    metric = "Interval score" if best["metric"] == "interval_score" else LABELS[best["metric"]]
    return f"{model_label(best['model_name'])} · {metric} {best['value']:.2f}"


def _input_range(row: dict) -> str:
    """Format the input window, using generated-file dates for open boundaries.

    Args:
        row: Benchmark definition and optional generated-file manifest.

    Returns:
        Readable input window with explicit fallbacks for unknown dates.
    """
    generated = (materialized_from_row(row) or {}).get("input") or {}
    start = row.get("history_start") or generated.get("first_date")
    end = row.get("history_end") or generated.get("last_date")
    return f"{_pretty_date(start) if start else 'First available date'} – " + (
        _pretty_date(end) if end else "Last date before the test start"
    )


def _pretty_date(value) -> str:
    """Format a stored date as a compact, unambiguous calendar date.

    Args:
        value: Date object or ISO-formatted stored value.

    Returns:
        Date such as '16 Oct 2026', or a dash when no date is recorded.
    """
    try:
        day = value if isinstance(value, _date) else _date.fromisoformat(str(value)[:10])
    except ValueError:
        return "—"
    return f"{day:%d %b %Y}"


def _runs_text(count) -> str:
    count = count or 0
    return "No runs yet" if count == 0 else f"{count} run" + ("" if count == 1 else "s")


def _parse_date(text, label: str, *, required: bool) -> _date | None:
    text = (text or "").strip()
    if not text:
        if required:
            raise ValueError(f"Enter {label} as YYYY-MM-DD.")
        return None
    try:
        return _date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Enter {label} as YYYY-MM-DD.") from None


def _designer_dialog(existing: dict | None, on_done) -> None:
    """Form to create or edit a draft; loads the dataset choices from CKAN when it opens."""
    state = {"id": existing["id"] if existing else None, "report": None, "saved": False, "version": 0}
    row = existing or {}
    with ui.dialog() as dialog, ui.card().classes("w-[44rem] max-w-full gap-2"):
        ui.label("Edit benchmark" if existing else "New benchmark").classes("text-lg font-semibold")
        error = ui.label("").classes("text-red-600 text-sm")
        try:
            options = {d["qid"]: f"{d['name']} ({d['qid']})" for d in ckan_client.list_processed_datasets()}
        except Exception:
            logger.exception("could not load the dataset choices")
            options = {}
            error.set_text("Could not load datasets.")
        if row.get("dataset_qid") and row["dataset_qid"] not in options:
            options[row["dataset_qid"]] = row["dataset_qid"]

        def invalidate(_=None):
            state["version"] += 1
            state["report"] = None
            finalize_btn.disable()

        def text_input(label, value="", **props):
            return ui.input(label, value=value, placeholder=props.get("placeholder"), on_change=invalidate
                            ).props("outlined dense").classes("w-full")

        name = text_input("Name", row.get("name") or "")
        description = text_input("Description", row.get("description") or "")
        dataset = ui.select(options, label="Dataset", on_change=invalidate,
                            value=row.get("dataset_qid") or next(iter(options), None)
                            ).props("outlined dense").classes("w-full")
        target = text_input("Target column", row.get("target_column") or "")
        date_col = text_input("Date column", row.get("date_column") or "datum")
        t_start = text_input("Test start (C)", _fmt(row.get("test_start")), placeholder="YYYY-MM-DD")
        t_end = text_input("Test end (D)", _fmt(row.get("test_end")), placeholder="YYYY-MM-DD")
        ui.label("Filters (optional): each one is column = value; together they select one series"
                 ).classes("text-sm text-gray-600 mt-1")
        filter_box = ui.column().classes("w-full gap-1")
        filter_inputs: list = []     # (column input, value input, row); the form allows MAX_FORM_FILTERS of them

        def update_add_button():
            add_filter_btn.set_enabled(len(filter_inputs) < MAX_FORM_FILTERS)

        def add_filter(column: str = "", value: str = ""):
            with filter_box:
                with ui.row().classes("w-full items-center gap-2 no-wrap") as line:
                    entry = (text_input("Filter column", column), text_input("Filter value", value), line)

                    def remove_filter(_=None, entry=entry):
                        filter_inputs.remove(entry)
                        filter_box.remove(entry[2])
                        update_add_button()
                        invalidate()

                    ui.button(icon="close", on_click=remove_filter).props("flat round dense aria-label='Remove filter'")
            filter_inputs.append(entry)
            update_add_button()

        add_filter_btn = ui.button("Add filter", icon="add", on_click=lambda: (add_filter(), invalidate())
                                   ).props("flat no-caps dense").classes("self-start")
        for column, value in filters_from_row(row):
            add_filter(column, value)
        ui.label("Covariates (optional): further datasets whose columns are added to the model input, aligned to the "
                 "target series").classes("text-sm text-gray-600 mt-2")
        covariate_box = ui.column().classes("w-full gap-2")
        covariate_blocks: list = []      # dicts with the inputs of one covariate

        def update_covariate_button():
            add_covariate_btn.set_enabled(len(covariate_blocks) < MAX_FORM_COVARIATES)

        def add_covariate(source=None):
            block = {"filters": []}
            with covariate_box:
                with ui.column().classes("w-full gap-1 border border-gray-200 rounded-lg p-3") as card:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        block["name"] = text_input("Covariate name", source.name if source else "")
                        block["dataset"] = ui.select(
                            options, label="Covariate dataset", on_change=invalidate,
                            value=source.dataset_qid if source else None).props("outlined dense").classes("w-full")

                        def remove_covariate(_=None, block=block, card=card):
                            covariate_blocks.remove(block)
                            covariate_box.remove(card)
                            update_covariate_button()
                            invalidate()

                        ui.button(icon="delete", on_click=remove_covariate).props(
                            "flat round dense aria-label='Remove covariate'")
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        block["date"] = text_input("Covariate date column", source.date_column if source else "")
                        block["columns"] = text_input("Covariate columns (comma separated)",
                                                      ", ".join(source.columns) if source else "")
                        block["aggregation"] = ui.select(
                            ["mean", "sum", "last"], label="Per time step", on_change=invalidate,
                            value=source.aggregation if source else "mean").props("outlined dense").classes("w-40")
                    block["filter_box"] = ui.column().classes("w-full gap-1")

                    def add_cov_filter(column="", value="", block=block):
                        with block["filter_box"]:
                            with ui.row().classes("w-full items-center gap-2 no-wrap") as line:
                                entry = (text_input("Covariate filter column", column),
                                         text_input("Covariate filter value", value), line)

                                def remove_cov_filter(_=None, entry=entry, block=block):
                                    block["filters"].remove(entry)
                                    block["filter_box"].remove(entry[2])
                                    block["add_filter"].set_enabled(len(block["filters"]) < MAX_FORM_FILTERS)
                                    invalidate()

                                ui.button(icon="close", on_click=remove_cov_filter).props(
                                    "flat round dense aria-label='Remove covariate filter'")
                        block["filters"].append(entry)
                        block["add_filter"].set_enabled(len(block["filters"]) < MAX_FORM_FILTERS)

                    block["add_filter"] = ui.button(
                        "Add covariate filter", icon="add", on_click=lambda _=None: (add_cov_filter(), invalidate())
                    ).props("flat no-caps dense").classes("self-start")
                    for column, value in (source.filters if source else ()):
                        add_cov_filter(column, value)
            covariate_blocks.append(block)
            update_covariate_button()

        add_covariate_btn = ui.button("Add covariate", icon="add", on_click=lambda: (add_covariate(), invalidate())
                                      ).props("flat no-caps dense").classes("self-start")
        for source in covariates_from_row(row):
            add_covariate(source)
        h_start = text_input("History start (optional)", _fmt(row.get("history_start")), placeholder="YYYY-MM-DD")
        h_end = text_input("History end (blank = last date before C)", _fmt(row.get("history_end")),
                           placeholder="YYYY-MM-DD")
        filename = text_input("Input file name", row.get("input_filename") or "input.parquet")
        result = ui.column().classes("w-full gap-1")
        spinner = ui.spinner(size="md")
        spinner.set_visibility(False)

        def busy(on: bool):
            spinner.set_visibility(on)
            for b in (save_btn, check_btn):
                b.disable() if on else b.enable()
            if on:
                finalize_btn.disable()
            elif state["report"] is not None and state["report"].ok:
                finalize_btn.enable()

        def fields() -> dict:
            return dict(
                name=name.value or "", description=(description.value or "").strip() or None,
                dataset_qid=dataset.value or "", target_column=target.value or "",
                date_column=date_col.value or "",
                test_start=_parse_date(t_start.value, "test start", required=True),
                test_end=_parse_date(t_end.value, "test end", required=True),
                filters=tuple(((c.value or "").strip(), v.value or "") for c, v, _ in filter_inputs
                              if (c.value or "").strip() or (v.value or "")),
                covariates=[
                    {"name": (b["name"].value or "").strip(), "dataset_qid": b["dataset"].value or "",
                     "date_column": (b["date"].value or "").strip(), "columns": b["columns"].value or "",
                     "aggregation": b["aggregation"].value or "mean",
                     "filters": [((c.value or "").strip(), v.value or "") for c, v, _ in b["filters"]
                                 if (c.value or "").strip() or (v.value or "")]}
                    for b in covariate_blocks],
                history_start=_parse_date(h_start.value, "history start", required=False),
                history_end=_parse_date(h_end.value, "history end", required=False),
                input_filename=(filename.value or "").strip() or "input.parquet",
            )

        def fail(exc: Exception, what: str):
            if isinstance(exc, _DOMAIN_ERRORS):
                error.set_text(str(exc))
            else:
                logger.exception("could not %s", what)
                error.set_text(f"Could not {what}.")

        async def save() -> bool:
            """Store the form as a draft (create or update); True when stored."""
            if not _is_logged_in():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return False
            error.set_text("")
            try:
                values = fields()
            except ValueError as exc:
                error.set_text(str(exc))
                return False
            try:
                if state["id"] is None:
                    user = _current_user()
                    state["id"] = await run.io_bound(lambda: bactions.create_draft(**values, created_by=user.id))
                else:
                    await run.io_bound(lambda: bactions.update_draft(state["id"], **values))
            except Exception as exc:
                fail(exc, "save the draft")
                return False
            state["saved"] = True
            return True

        async def do_save():
            busy(True)
            try:
                if await save():
                    ui.notify("Draft saved.", type="positive")
            finally:
                busy(False)

        async def do_check():
            busy(True)
            try:
                version = state["version"]          # before the save, so an edit typed during it is caught
                if not await save():
                    return
                state["report"] = None
                finalize_btn.disable()
                try:
                    report = await run.io_bound(lambda: bactions.check(state["id"]))
                except Exception as exc:
                    fail(exc, "check the benchmark")
                    return
                if state["version"] != version:
                    error.set_text("The form changed during the check; check again.")
                    return
                state["report"] = report
                result.clear()
                with result:
                    for problem in report.problems:
                        ui.label(problem).classes("text-red-600 text-sm")
                    for warning in report.warnings:
                        ui.label(warning).classes("text-orange-600 text-sm")
                    if report.ok:
                        ui.label(f"Data commit: {report.data_commit}").classes("text-sm")
                        if report.columns:
                            ui.label(f"Generated model input: {report.input_rows} rows, {len(report.columns)} columns"
                                     ).classes("text-sm")
                            for column in report.columns:
                                share = (report.missing or {}).get(column)
                                ui.label(f"{column}" + (f" ({share:.0%} empty)" if share else "")).classes(
                                    "text-xs text-gray-600 pl-3")
                        ui.label(f"Observations in the test window: {report.truth_points}").classes("text-sm")
                        ui.label(f"History ends: {report.history_end}").classes("text-sm")
                        ui.label(
                            f"Forecast length: {report.prediction_length} steps every {report.frequency_days} days"
                        ).classes("text-sm")
            finally:
                busy(False)

        def open_finalize():
            if not _is_logged_in():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return
            report = state["report"]
            if report is None or not report.ok:
                error.set_text("Check the benchmark against the data first.")
                return
            with ui.dialog() as confirm, ui.card().classes("gap-2"):
                ui.label(f"Freeze this benchmark? It will be pinned to data commit {report.data_commit[:12]} "
                         "(the commit just checked) and can no longer be changed.")

                async def freeze():
                    if not _is_logged_in():
                        ui.notify(_LOGIN_EXPIRED, type="warning")
                        return
                    error.set_text("")
                    freeze_btn.disable()
                    spinner.set_visibility(True)
                    try:
                        user = _current_user()
                        await run.io_bound(
                            lambda: bactions.finalize(state["id"], expected_commit=report.data_commit, by=user))
                    except Exception as exc:
                        fail(exc, "finalize the benchmark")
                        confirm.close()
                        return
                    finally:
                        spinner.set_visibility(False)
                        freeze_btn.enable()
                    confirm.close()
                    state["saved"] = False          # only the navigation below, not a reload
                    dialog.close()
                    ui.navigate.to("/ui/benchmarks")

                with ui.row().classes("justify-end w-full"):
                    ui.button("Cancel", on_click=confirm.close).props("flat")
                    freeze_btn = ui.button("Freeze benchmark", on_click=freeze).props("color=negative")
            confirm.on("hide", confirm.delete)
            confirm.open()

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            save_btn = ui.button("Save", on_click=do_save)
            check_btn = ui.button("Check against data", on_click=do_check)
            finalize_btn = ui.button("Finalize", on_click=open_finalize).props("color=primary")
            finalize_btn.disable()

    def hidden():
        dialog.delete()
        if state["saved"]:
            on_done()

    dialog.on("hide", hidden)
    dialog.open()


def _retire_dialog(benchmark: dict, on_done, *, draft: bool = False) -> None:
    with ui.dialog() as dialog, ui.card().classes("gap-2"):
        ui.label(
            "Retire this draft? It disappears from the list." if draft
            else f"Retire {benchmark['name']}? It disappears from the lists; its entries stay stored."
        )
        error = ui.label("").classes("text-red-600 text-sm")

        async def retire():
            if not _is_logged_in():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return
            try:
                await run.io_bound(lambda: bactions.retire(benchmark["id"]))
            except bactions.NotFound as exc:
                error.set_text(str(exc))
                return
            except Exception:
                logger.exception("could not retire the benchmark")
                error.set_text("Could not retire the benchmark.")
                return
            dialog.close()
            on_done()

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Retire draft" if draft else "Retire benchmark", on_click=retire).props("color=negative")
    dialog.on("hide", dialog.delete)
    dialog.open()


def register_benchmarks_page() -> None:
    """Register ``/ui/benchmarks``. Page load reads the database only; CKAN only when the designer opens."""

    @ui.page("/ui/benchmarks")
    def benchmarks_page(request: Request):
        """Render compact benchmark rows and authorized draft management controls.

        Args:
            request: Browser request handled by the benchmark list route.
        """
        header("/ui/benchmarks", api="benchmarks")
        ui.add_css(BENCHMARK_LIST_CSS)
        logged_in = _is_logged_in()
        reload = ui.navigate.reload

        def guarded(action):
            """Run ``action`` only while the login is still valid."""
            if not _is_logged_in():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return
            action()

        async def copy(benchmark_id: int):
            if not _is_logged_in():
                ui.notify(_LOGIN_EXPIRED, type="warning")
                return
            try:
                user = _current_user()
                await run.io_bound(lambda: bactions.copy_as_draft(benchmark_id, user.id))
            except bactions.NotFound as exc:
                ui.notify(str(exc), type="negative")
                return
            except Exception:
                logger.exception("could not copy the benchmark")
                ui.notify("Could not copy the benchmark.", type="negative")
                return
            reload()

        try:
            finals = bactions.list_final()
        except Exception:
            logger.exception("could not load the benchmarks")
            with ui.column().classes("epi-page-padding"):
                error_label("Could not load benchmarks.")
            return
        drafts, drafts_error = [], False
        if logged_in:
            try:
                drafts = bactions.list_drafts()
            except Exception:
                logger.exception("could not load the drafts")
                drafts_error = True

        try:
            best = lboard_actions.best_entries([b["id"] for b in finals if b.get("entry_count")])
        except Exception:
            logger.exception("could not load the best entries")
            best = {}

        with ui.column().classes("epi-page epi-benchmark-list-page"):
            with ui.row().classes("w-full items-start justify-between"):
                with ui.column().classes("gap-0"):
                    ui.label("Benchmarks").classes("epi-page-title")
                    ui.label("Fixed data, test windows and scoring for comparing forecast models."
                             ).classes("epi-page-description")
                if logged_in:
                    ui.button("New benchmark", on_click=lambda: guarded(lambda: _designer_dialog(None, reload)))
            if logged_in:
                ui.label("Drafts").classes("text-xl font-semibold text-gray-900")
                if drafts_error:
                    error_label("Could not load drafts.")
                elif not drafts:
                    ui.label("No drafts.").classes("text-sm text-gray-500")
                for d in drafts:
                    with ui.row().classes("items-center gap-4 border border-gray-200 rounded-xl px-4 py-2"):
                        ui.label(d["name"]).classes("font-semibold")
                        ui.label(f"{d['dataset_qid']} · {d['target_column']} · test window "
                                 f"{d['test_start']} → {d['test_end']}").classes("text-sm text-gray-600")
                        ui.button("Edit", on_click=lambda d=d: guarded(lambda: _designer_dialog(d, reload))
                                  ).props("flat")
                        ui.button("Retire", on_click=lambda d=d: guarded(
                            lambda: _retire_dialog(d, reload, draft=True))).props("flat color=negative")
            with ui.row().classes("epi-benchmark-list-heading"):
                ui.label("Final benchmarks").classes("text-xl font-semibold text-gray-900")
                count = len(finals)
                ui.label(f"{count} benchmark{'s' if count != 1 else ''}").classes("epi-benchmark-list-muted")
            if not finals:
                ui.label("No benchmarks yet.").classes("text-sm text-gray-500")
            with ui.element("div").classes("epi-benchmark-list"):
                for b in finals:
                    with ui.element("article").classes("epi-benchmark-list-row"):
                        with ui.column().classes("epi-benchmark-list-content"):
                            ui.link(b["name"], f"/ui/benchmarks/{b['id']}").classes("epi-benchmark-list-title")
                            ui.label(description_text(b)).classes("epi-benchmark-list-description")
                            with ui.row().classes("epi-benchmark-list-facts"):
                                ui.label(f"Test window {_pretty_date(b['test_start'])} – {_pretty_date(b['test_end'])}")
                                ui.label(f"Input {_input_range(b)}")
                            with ui.row().classes("epi-benchmark-list-facts epi-benchmark-list-secondary"):
                                ui.label(f"Best: {_best_text(best.get(b['id']))}")
                                ui.label(f"Finalized {_pretty_date(b.get('finalized_at'))}"
                                         ).classes("epi-benchmark-list-muted")
                        with ui.column().classes("epi-benchmark-list-actions"):
                            ui.label(_runs_text(b.get("entry_count"))).classes("font-semibold")
                            ui.link("Leaderboard →", f"/ui/benchmarks/{b['id']}?tab=leaderboard")
                        if logged_in:
                            with ui.row().classes("epi-benchmark-list-management"):
                                ui.button("Copy as draft", on_click=lambda b=b: copy(b["id"])).props("flat dense size=sm")
                                ui.button("Retire", on_click=lambda b=b: guarded(lambda: _retire_dialog(b, reload))
                                          ).props("flat dense size=sm color=negative")
