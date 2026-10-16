"""The /ui/benchmarks page: list final benchmarks and design, check and finalize drafts."""

import logging
from datetime import date as _date

from nicegui import run, ui, app as _napp
from starlette.requests import Request

from app.auth import _daily_token
from app.benchmarks import actions as bactions
from app.benchmarks.definition import DefinitionError
from app.clients import ckan as ckan_client
from app.leaderboard.loaders import LoadError
from app.ui.pages import _error_label, _header

logger = logging.getLogger(__name__)

_LOGIN_EXPIRED = "Your login has expired; please log in again."
_DOMAIN_ERRORS = (DefinitionError, LoadError, bactions.NotFound, bactions.NotDraft, bactions.Conflict)


def _is_logged_in() -> bool:
    return _napp.storage.user.get("token") == _daily_token()


def _fmt(value) -> str:
    return "" if value is None else str(value)


def _input_range(row: dict) -> str:
    start = _fmt(row.get("history_start")) or "first date"
    end = _fmt(row.get("history_end")) or "last date before the test start"
    return f"{start} → {end}"


def _filter_text(row: dict) -> str:
    return f"{row['filter_column']} = {row.get('filter_value') or ''}" if row.get("filter_column") else "none"


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
    state = {"id": existing["id"] if existing else None, "report": None, "saved": False}
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
        fcol = text_input("Filter column (optional)", row.get("filter_column") or "")
        fval = text_input("Filter value", row.get("filter_value") or "")
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
                filter_column=(fcol.value or "").strip() or None, filter_value=fval.value or None,
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
                    state["id"] = await run.io_bound(lambda: bactions.create_draft(**values))
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
                if not await save():
                    return
                try:
                    report = await run.io_bound(lambda: bactions.check(state["id"]))
                except Exception as exc:
                    fail(exc, "check the benchmark")
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
                        ui.label(f"Observations in the test window: {report.truth_points}").classes("text-sm")
                        ui.label(f"History ends: {report.history_end}").classes("text-sm")
                        ui.label(
                            f"Forecast length: {report.prediction_length} steps every {report.frequency_days} days"
                        ).classes("text-sm")
                        ui.label("Input SQL").classes("text-sm font-semibold")
                        ui.label(report.input_sql or "").classes(
                            "font-mono text-xs whitespace-pre-wrap bg-gray-100 p-2 rounded w-full")
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
                ui.label(f"This freezes the benchmark and pins data commit {report.data_commit}. "
                         "It cannot be changed afterwards.")

                async def freeze():
                    if not _is_logged_in():
                        ui.notify(_LOGIN_EXPIRED, type="warning")
                        return
                    error.set_text("")
                    freeze_btn.disable()
                    spinner.set_visibility(True)
                    try:
                        await run.io_bound(lambda: bactions.finalize(state["id"]))
                    except Exception as exc:
                        fail(exc, "finalize the benchmark")
                        confirm.close()
                        return
                    finally:
                        spinner.set_visibility(False)
                        freeze_btn.enable()
                    confirm.close()
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


def _retire_dialog(benchmark: dict, on_done) -> None:
    with ui.dialog() as dialog, ui.card().classes("gap-2"):
        ui.label(f"Retire {benchmark['name']}? It disappears from the lists; its entries stay stored.")
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
            ui.button("Retire benchmark", on_click=retire).props("color=negative")
    dialog.on("hide", dialog.delete)
    dialog.open()


def register_benchmarks_page() -> None:
    """Register ``/ui/benchmarks``. Page load reads the database only; CKAN only when the designer opens."""

    @ui.page("/ui/benchmarks")
    def benchmarks_page(request: Request):
        _header("/ui/benchmarks")
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
                await run.io_bound(lambda: bactions.copy_as_draft(benchmark_id))
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
            with ui.column().classes("px-8 py-6"):
                _error_label("Could not load benchmarks.")
            return
        drafts, drafts_error = [], False
        if logged_in:
            try:
                drafts = bactions.list_drafts()
            except Exception:
                logger.exception("could not load the drafts")
                drafts_error = True

        with ui.column().classes("px-8 py-6 w-full gap-4"):
            with ui.column().classes("gap-0"):
                ui.label("Benchmarks").classes("text-3xl font-bold text-gray-900")
                ui.label(
                    "A benchmark fixes the series, the test window and the data version that forecasts "
                    "are scored against. Design it as a draft, check it against the data, then finalize it."
                ).classes("text-sm text-gray-500 mt-1")
            if logged_in:
                ui.button("New benchmark", on_click=lambda: guarded(lambda: _designer_dialog(None, reload)))
                ui.label("Drafts").classes("text-xl font-semibold text-gray-900")
                if drafts_error:
                    _error_label("Could not load drafts.")
                elif not drafts:
                    ui.label("No drafts.").classes("text-sm text-gray-500")
                for d in drafts:
                    with ui.row().classes("items-center gap-4 border border-gray-200 rounded-xl px-4 py-2"):
                        ui.label(d["name"]).classes("font-semibold")
                        ui.label(f"{d['dataset_qid']} · {d['target_column']} · test window "
                                 f"{d['test_start']} → {d['test_end']}").classes("text-sm text-gray-600")
                        ui.button("Edit", on_click=lambda d=d: guarded(lambda: _designer_dialog(d, reload))
                                  ).props("flat")
            ui.label("Final benchmarks").classes("text-xl font-semibold text-gray-900")
            if not finals:
                ui.label("No benchmarks yet.").classes("text-sm text-gray-500")
            for b in finals:
                with ui.column().classes("w-full gap-1 border border-gray-200 rounded-xl px-4 py-3"):
                    with ui.row().classes("items-center gap-4"):
                        ui.label(b["name"]).classes("text-lg font-semibold")
                        ui.link("Leaderboard", f"/ui/leaderboard?benchmark={b['id']}").classes("text-sm")
                        if logged_in:
                            ui.button("Copy as draft", on_click=lambda b=b: copy(b["id"])).props("flat")
                            ui.button("Retire", on_click=lambda b=b: guarded(lambda: _retire_dialog(b, reload))
                                      ).props("flat color=negative")
                    with ui.row().classes("gap-x-6 gap-y-0"):
                        for caption, value in (
                            ("Dataset", b["dataset_qid"]),
                            ("Target", b["target_column"]),
                            ("Filter", _filter_text(b)),
                            ("Input range", _input_range(b)),
                            ("Test window", f"{b['test_start']} → {b['test_end']}"),
                            ("Entries", b.get("entry_count", 0)),
                            ("Data commit", (b.get("data_commit") or "")[:12]),
                        ):
                            ui.label(f"{caption}: {value}").classes("text-sm text-gray-700")
