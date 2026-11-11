"""Readable benchmark summaries, generated-file layouts, and provenance details."""

from collections.abc import Callable
from datetime import date

from nicegui import ui

from app.benchmarks import recipe
from app.benchmarks.definition import filters_from_row, materialized_from_row, target_preview_from_row
from app.leaderboard.about import description_text, overview_facts


def _date(value) -> date | None:
    """Parse a stored date without inventing a missing boundary.

    Args:
        value: Date object or ISO-formatted stored value.

    Returns:
        Parsed date, or None when no usable date is recorded.
    """
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_range(start, end) -> str:
    """Format a date range with explicit labels for unknown boundaries.

    Args:
        start: First recorded date or an unknown boundary.
        end: Last recorded date or an unknown boundary.

    Returns:
        Human-readable inclusive date range.
    """
    first, last = _date(start), _date(end)
    return (f"{first:%d %b %Y}" if first else "First available date") + " – " + (
        f"{last:%d %b %Y}" if last else "Last available date"
    )


def _file_windows(row: dict, manifest: dict, windows: list[dict]) -> list[dict]:
    """Combine generated-file metadata with stored window boundaries.

    Args:
        row: Benchmark definition from the database.
        manifest: Stored generated-file manifest.
        windows: Time windows from the stored or reconstructed recipe.

    Returns:
        File layouts with dates, formatted ranges, and inclusive calendar durations.
    """
    by_file = {window["file"]: window for window in windows}
    result = []
    for layout in recipe.file_layouts(row, manifest):
        window = by_file.get(layout["title"], {})
        reference = layout["kind"] == "reference"
        first = _date(layout["first_date"]) or _date(window.get("from")) or _date(
            row.get("test_start" if reference else "history_start")
        )
        last = _date(layout["last_date"]) or _date(window.get("to")) or _date(
            row.get("test_end" if reference else "history_end")
        )
        days = (last - first).days + 1 if first and last and last >= first else None
        result.append({**layout, "first": first, "last": last, "days": days, "range": _date_range(first, last)})
    return result


def overview(row: dict, *, can_edit: bool, on_edit: Callable[[], None]) -> None:
    """Render the benchmark description, target, metadata, and forecast window.

    Args:
        row: Stored benchmark definition and summary counts.
        can_edit: Whether the current user may edit the description.
        on_edit: Callback opening the existing description editor.
    """
    with ui.row().classes("epi-benchmark-description-row"):
        ui.label(description_text(row)).classes("epi-benchmark-description")
        if can_edit:
            ui.button(icon="edit", on_click=on_edit).props(
                "flat round dense size=sm aria-label='Edit description'"
            ).classes("text-gray-500")
    with ui.element("section").classes("epi-benchmark-summary"):
        with ui.column().classes("epi-benchmark-target"):
            ui.label("Target series").classes("epi-benchmark-heading").props("role=heading aria-level=2")
            ui.label(row["target_column"]).classes("epi-benchmark-identifier")
            with ui.column().classes("epi-benchmark-filters"):
                for column, value in filters_from_row(row):
                    ui.label(f"{column} = {value}").classes("epi-benchmark-filter")
        with ui.element("dl").classes("epi-benchmark-facts"):
            for caption, value in overview_facts(row):
                if caption == "Series":
                    continue
                with ui.element("div").classes("epi-benchmark-fact"):
                    with ui.element("dt"):
                        ui.label(caption)
                    with ui.element("dd"):
                        ui.label(value)
    manifest = materialized_from_row(row)
    if manifest:
        windows = _file_windows(row, manifest, recipe.display(row, manifest)["windows"])
        with ui.element("section").classes("epi-benchmark-window"):
            ui.label("Forecast window").classes("epi-benchmark-heading").props("role=heading aria-level=2")
            ui.label("The model sees the input period. Forecasts are evaluated over the reference period.").classes(
                "epi-benchmark-body"
            )
            preview = target_preview_from_row(row)
            if preview:
                with ui.element("picture").classes("epi-benchmark-target-preview"):
                    source = ui.element("source").props('media="(max-width: 700px)"')
                    source.props["srcset"] = preview.get("mobile_image") or preview["image"]
                    image = ui.element("img").props(
                        'alt="Observed target series: blue model input and teal held-out scoring reference"'
                    )
                    image.props["src"] = preview["image"]
                ui.label("Observed target values · Shaded period: scoring reference").classes("epi-benchmark-note")
            elif all(window["days"] for window in windows):
                with ui.element("div").classes("epi-benchmark-timeline").props("aria-hidden=true"):
                    for window in windows:
                        ui.element("div").classes(f"epi-benchmark-period-{window['kind']}").style(
                            f"flex-grow: {window['days']}"
                        )
            with ui.row().classes("epi-benchmark-window-legend"):
                for window in windows:
                    title = "Model input" if window["kind"] == "input" else "Scoring reference"
                    duration = f" · {window['days']} day{'s' if window['days'] != 1 else ''}" if window["days"] else ""
                    with ui.row().classes("epi-benchmark-window-label"):
                        ui.element("span").classes(f"epi-benchmark-swatch epi-benchmark-period-{window['kind']}")
                        with ui.column().classes("gap-0"):
                            ui.label(title + duration).classes("font-semibold")
                            ui.label(window["range"]).classes("epi-benchmark-body")
    with ui.row().classes("epi-benchmark-explore"):
        ui.link("Explore the data →", f"/ui/benchmarks/{row['id']}?tab=data")


def _file_panel(layout: dict, *, covariates: bool) -> None:
    """Render the purpose, size, date range, and links of one generated file.

    Args:
        layout: File layout including window metadata.
        covariates: Whether the model input contains covariate columns.
    """
    reference = layout["kind"] == "reference"
    with ui.element("article").classes("epi-benchmark-file"):
        ui.label("Scoring reference" if reference else "Model input").classes("epi-benchmark-heading").props(
            "role=heading aria-level=2"
        )
        ui.label(layout["title"]).classes("epi-benchmark-identifier")
        ui.label("Held-out target values used for scoring" if reference else
                 "Historical target and covariates" if covariates else "Historical target values").classes(
            "epi-benchmark-body"
        )
        ui.label(f"{layout['rows']:,} rows").classes("epi-benchmark-file-count")
        ui.label(layout["range"]).classes("epi-benchmark-body")
        if layout["url"]:
            with ui.row().classes("epi-benchmark-file-links"):
                ui.link("Download Parquet ↗", layout["url"], new_tab=True).props(
                    f"aria-label='Download {layout['title']}'"
                )
                if layout["catalog"]:
                    ui.link("Data hub ↗", layout["catalog"], new_tab=True)
        else:
            ui.label("Stored in internal lakeFS; download unavailable.").classes("epi-benchmark-note")


def _sources(sources: list[dict]) -> None:
    """Render source links, filters, and columns in responsive source rows.

    Args:
        sources: Frozen source descriptions from the benchmark recipe.
    """
    with ui.element("section").classes("epi-benchmark-section"):
        ui.label("Sources and filters").classes("epi-benchmark-heading").props("role=heading aria-level=2")
        with ui.column().classes("epi-benchmark-sources"):
            for source in sources:
                with ui.element("article").classes("epi-benchmark-source"):
                    with ui.column().classes("epi-benchmark-source-identity"):
                        ui.label("Source dataset" if source["role"] == "target" else
                                 f"Covariate · {source['role']}").classes("font-semibold")
                        if source.get("catalog"):
                            ui.link(source["qid"] + " ↗", source["catalog"], new_tab=True)
                        else:
                            ui.label(source["qid"])
                        ui.label("Benchmark-pinned version" if source.get("commit") else
                                 "Latest version · benchmark version not recorded").classes("epi-benchmark-note")
                    with ui.column().classes("epi-benchmark-source-field epi-benchmark-source-file"):
                        ui.label("Source file").classes("epi-benchmark-note")
                        if source.get("url"):
                            ui.link(source.get("file") or "Source file", source["url"], new_tab=True)
                        else:
                            ui.label(source.get("file") or "Not recorded")
                    with ui.column().classes("epi-benchmark-source-field"):
                        ui.label("Filters").classes("epi-benchmark-note")
                        for value in source["filters"]:
                            ui.label("No filters" if value == "none" else value)
                    with ui.column().classes("epi-benchmark-source-field"):
                        ui.label("Date column").classes("epi-benchmark-note")
                        ui.label(source["date_column"]).classes("epi-benchmark-mono")
                    with ui.column().classes("epi-benchmark-source-field"):
                        ui.label("Selected columns").classes("epi-benchmark-note")
                        for column in source["columns"]:
                            ui.label(column).classes("epi-benchmark-mono")


def _generation_details(info: dict) -> None:
    """Keep the full time windows and generation rules behind a disclosure.

    Args:
        info: Stored or reconstructed benchmark recipe.
    """
    ui.label("Time windows").classes("epi-benchmark-subheading")
    ui.table(
        columns=[{"name": name, "label": label, "field": name, "align": "left"}
                 for name, label in (("file", "File"), ("from", "From"), ("to", "To"), ("rows", "Rows"),
                                     ("content", "Content"))],
        rows=info["windows"], row_key="file",
    ).props("flat dense hide-bottom").classes("epi-benchmark-table w-full")
    ui.label("Rules").classes("epi-benchmark-subheading")
    with ui.element("ul").classes("epi-benchmark-rules"):
        for rule in info["rules"]:
            with ui.element("li"):
                ui.label(rule)
    if info["merge"]:
        ui.label("Merge").classes("epi-benchmark-subheading")
        merge = ui.table(
            columns=[{"name": "covariate", "label": "Covariate", "field": "covariate", "align": "left"},
                     {"name": "joined_to", "label": "Joined to", "field": "joined_to", "align": "left"},
                     {"name": "rule", "label": "Rule", "field": "rule", "align": "left",
                      "style": "white-space: normal; min-width: 18rem"},
                     {"name": "columns", "label": "Output columns", "field": "columns", "align": "left"}],
            rows=info["merge"], row_key="covariate",
        ).props("flat dense hide-bottom").classes("epi-benchmark-table w-full")
        merge.add_slot("body-cell-columns", '''
            <q-td :props="props"><div v-for="column in props.value" :key="column">{{ column }}</div></q-td>
        ''')


def generated_data(row: dict, manifest: dict) -> None:
    """Render generated files, schema comparison, sources, and reproducibility tools.

    Args:
        row: Benchmark definition from the database.
        manifest: Stored generated-file manifest, including older lakeFS-only layouts.
    """
    info = recipe.display(row, manifest)
    layouts = _file_windows(row, manifest, info["windows"])
    covariates = any(column["role"].startswith("covariate") for column in layouts[0]["columns"])
    with ui.element("div").classes("epi-benchmark-files"):
        for layout in layouts:
            _file_panel(layout, covariates=covariates)
    ui.label("Only the input file is passed to the model.").classes("epi-benchmark-note")
    with ui.element("section").classes("epi-benchmark-section"):
        ui.label("Columns in each file").classes("epi-benchmark-heading").props("role=heading aria-level=2")
        columns = ui.table(
            columns=[{"name": "column", "label": "Column", "field": "column", "align": "left"},
                     {"name": "role", "label": "Role", "field": "role", "align": "left"},
                     {"name": "input", "label": "Input", "field": "input", "align": "center"},
                     {"name": "reference", "label": "Reference", "field": "reference", "align": "center"}],
            rows=recipe.column_rows(layouts), row_key="column",
        ).props("flat bordered dense hide-bottom").classes("epi-benchmark-table epi-benchmark-columns w-full")
        columns.add_slot("body-cell-column", '''
            <q-td :props="props" class="epi-benchmark-mono">{{ props.value }}</q-td>
        ''')
        for kind in ("input", "reference"):
            columns.add_slot(f"body-cell-{kind}", '''
                <q-td :props="props" class="text-center">
                    <span :aria-label="props.value === 'yes' ? 'Included' : 'Not included'">
                        {{ props.value === 'yes' ? '✓' : '—' }}
                    </span>
                </q-td>
            ''')
    _sources(info["sources"])
    with ui.row().classes("epi-benchmark-provenance"):
        ui.icon("lock_outline", size="16px")
        ui.label("Sources pinned at finalization · commit " + str(info["commit"])[:12] + "…").tooltip(
            str(info["commit"])
        )
    with ui.column().classes("epi-benchmark-disclosures"):
        with ui.expansion("Generation rules and merge details").props(
            "switch-toggle-side expand-icon=chevron_right expanded-icon=expand_more"
        ).classes("epi-benchmark-disclosure"):
            _generation_details(info)
        with ui.expansion("File checksums").props(
            "switch-toggle-side expand-icon=chevron_right expanded-icon=expand_more"
        ).classes("epi-benchmark-disclosure"):
            ui.label("SHA-256 identifies the exact generated file contents.").classes("epi-benchmark-note")
            for layout in layouts:
                ui.label(layout["title"]).classes("font-semibold mt-3")
                ui.label(layout["sha256"]).classes("epi-benchmark-checksum")
        with ui.expansion("Reproduce both files", icon="code").props(
            "switch-toggle-side expand-icon=chevron_right expanded-icon=expand_more"
        ).classes("epi-benchmark-disclosure"):
            ui.code(info["code"], language="python").classes("w-full")
