"""Stepper, run summary, and footer for the model-run workflow."""

import json
from nicegui import ui


STEPS = [
    (1, "Datasets",      "Choose input datasets"),
    (2, "Transforms",    "SQL per dataset"),
    (3, "Model",         "Select model"),
    (4, "Configuration", "JSON parameters"),
    (5, "Review & Run",  "Validate and run"),
]


def stepper(current_step: int):
    """Render navigation between the five workflow steps.

    Args:
        current_step: One-based index of the active workflow step.
    """
    with ui.row().classes("epi-stepper"):
        for i, (n, title, desc) in enumerate(STEPS):
            active = n == current_step
            done   = n < current_step
            clickable = not active
            row_cls = "items-center gap-3 shrink-0 " + ("cursor-pointer" if clickable else "")
            with ui.row().classes(row_cls).on("click", lambda _, step=n: ui.navigate.to(f"/ui/run_workflow/{step}") if step != current_step else None):
                circle_cls = (
                    "w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold shrink-0 "
                    + ("epi-primary" if (active or done) else "border-2 border-gray-300 text-gray-400")
                )
                with ui.element("div").classes(circle_cls):
                    if done:
                        ui.icon("check").classes("text-sm")
                    else:
                        ui.label(str(n))
                with ui.column().classes("gap-0"):
                    ui.label(title).classes(
                        "text-sm font-semibold "
                        + ("text-primary" if active else "text-gray-700 hover:text-primary" if done else "text-gray-400 hover:text-blue-400")
                    )
                    ui.label(desc).classes("text-xs text-gray-400")
            if i < len(STEPS) - 1:
                ui.element("div").classes(
                    "flex-1 h-px mx-4 " + ("bg-blue-300" if done else "bg-gray-200")
                )


def summary_panel(state: dict, hint: str = ""):
    """Render selected datasets, model, and configuration beside a step.

    Args:
        state: Current workflow selections or task state.
        hint: Optional guidance shown below the summary.
    """
    DOT = ["bg-blue-500", "bg-green-500", "bg-purple-500", "bg-orange-500", "bg-red-500", "bg-teal-500"]
    with ui.element("div").classes("epi-run-summary"):
        ui.label("Run Summary").classes("text-base font-bold text-gray-900")
        ui.label("Your selections will appear here.").classes("text-xs text-gray-400 mb-3")

        with ui.row().classes("w-full items-start gap-3 py-3 border-b border-gray-100"):
            ui.icon("storage").classes("text-primary mt-0.5 shrink-0")
            with ui.column().classes("flex-1 gap-1 min-w-0"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Datasets").classes("text-sm font-semibold text-gray-800")
                    n_ds = len(state.get("datasets", []))
                    if n_ds:
                        with ui.element("div").classes("bg-green-100 text-green-700 text-xs px-2 py-0.5 rounded-full font-medium shrink-0"):
                            ui.label(f"{n_ds} selected")
                for i, ds in enumerate(state.get("datasets", [])):
                    with ui.row().classes("items-center gap-2"):
                        ui.element("div").classes(f"w-2 h-2 rounded-full shrink-0 {DOT[i % len(DOT)]}")
                        ui.label(ds["name"]).classes("text-xs text-gray-600 truncate")
                if not state.get("datasets"):
                    ui.label("None selected").classes("text-xs text-gray-400")

        bench = state.get("benchmark")
        if bench:
            with ui.row().classes("w-full items-start gap-3 py-3 border-b border-gray-100"):
                ui.icon("flag").classes("text-primary mt-0.5 shrink-0")
                ui.label(f"Benchmark: {bench['name']} (data commit {bench['data_commit'][:12]})").classes(
                    "text-xs text-gray-600 flex-1 min-w-0")

        with ui.row().classes("w-full items-start gap-3 py-3 border-b border-gray-100"):
            ui.icon("view_in_ar").classes("text-primary mt-0.5 shrink-0")
            with ui.column().classes("flex-1 gap-0"):
                ui.label("Model").classes("text-sm font-semibold text-gray-800")
                m = state.get("model")
                if m:
                    ui.label(f"{m['name']} ({m['docker_tag']})").classes("text-xs text-gray-600")
                else:
                    ui.label("Not selected yet").classes("text-xs text-gray-400")

        with ui.row().classes("w-full items-start gap-3 py-3"):
            ui.icon("tune").classes("text-primary mt-0.5 shrink-0")
            with ui.column().classes("flex-1 gap-0"):
                ui.label("Configuration").classes("text-sm font-semibold text-gray-800")
                cfg = (state.get("config") or "").strip()
                try:
                    parsed = json.loads(cfg)
                    keys = list(parsed.keys())
                    summary = ", ".join(f"{k}: {parsed[k]}" for k in keys[:2]) if keys else "Configured"
                    ui.label(summary).classes("text-xs text-gray-600")
                except Exception:
                    ui.label("Not configured yet").classes("text-xs text-gray-400")

    if hint:
        with ui.row().classes("epi-run-hint"):
            ui.icon("info_outline").classes("text-blue-500 shrink-0")
            ui.label(hint).classes("text-sm text-primary")


def workflow_footer():
    """Render the reproducibility notice below a workflow step."""
    with ui.row().classes("w-full justify-center items-center gap-2 py-6 mt-4 border-t border-gray-100"):
        ui.icon("lock").classes("text-gray-400 text-sm")
        ui.label("All runs are logged and reproducible.").classes("text-xs text-gray-400")
