"""AI Blackboard tasks, routing rules, and traces."""

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from nicegui import ui, app as _napp
from starlette.requests import Request
from app.clients import blackboard as blackboard_client
from app.config import settings
from app.ui import session
from app.ui.layout import header
from app.ui.components.messages import error_label
from app.ui.session import require_login


def register_blackboard_page():
    """Register the blackboard page route."""
    @ui.page("/ui/blackboard")
    def blackboard(request: Request):
        """Render task monitoring and routing controls for authorized users.

        Args:
            request: Incoming HTTP request used by the page.
        """
        from datetime import datetime

        if not require_login("blackboard"):
            return
        header("/ui/blackboard")

        def _fmt_date(dt) -> tuple:
            """Format a catalog timestamp for display.

            Args:
                dt: Timestamp from task metadata.

            Returns:
                The formatted timestamp and a relative age, with a fallback for invalid values.
            """
            if not dt:
                return "—", ""
            try:
                s = str(dt).strip()
                d = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        d = datetime.strptime(s[:19], fmt)
                        break
                    except ValueError:
                        pass
                if d is None:
                    d = datetime.strptime(s[:10], "%Y-%m-%d")
                # Calendar-day difference, not elapsed 24h periods -- otherwise
                # anything <24h old (e.g. yesterday evening) shows as "Today".
                days = (datetime.now().date() - d.date()).days
                if days <= 0:
                    rel = "Today"
                elif days == 1:
                    rel = "Yesterday"
                else:
                    rel = f"{days} days ago"
                return d.strftime("%b %-d, %Y  %H:%M"), rel
            except Exception:
                return str(dt), ""

        def _state_cls(state: str) -> str:
            """Return the display classes for a Blackboard task state.

            Args:
                state: Current workflow selections or task state.

            Returns:
                Utility classes for the background and text color of the task state.
            """
            s = (state or "").lower()
            if s == "waiting":                       return "bg-blue-100 text-blue-700"
            if s == "dispatching_run":               return "bg-orange-100 text-orange-700"
            if s == "running":                       return "bg-amber-100 text-amber-700"
            if s == "waiting_for_next_periodic_run": return "bg-purple-100 text-purple-700"
            if s == "resolved":                      return "bg-green-100 text-green-700"
            if s == "failed":                        return "bg-red-100 text-red-700"
            return "bg-gray-100 text-gray-600"

        try:
            raw_rows = blackboard_client.list_task_runs()
        except Exception as e:
            with ui.column().classes("epi-page-padding"):
                error_label(f"Could not load blackboard: {e}")
            return

        all_rows = []
        for r in raw_rows:
            date_display, rel_time = _fmt_date(r.get("created_at"))
            state_change_display, state_change_rel = _fmt_date(r.get("last_state_change"))
            periodic_last_display, periodic_last_rel = _fmt_date(r.get("periodic_last_triggered_at"))
            interval = r.get("periodic_interval_minutes")
            if interval:
                schedule_display = f"periodic · every {interval}min"
            elif r.get("post_type") == "run_me":
                schedule_display = "once"
            else:
                schedule_display = "—"
            fid = r.get("triggered_flow_run_id")
            prefect_url = (
                f"{settings.prefect_ui_url}/runs/flow-run/{fid}"
                if fid and settings.prefect_ui_url else ""
            )
            all_rows.append({**r,
                "_date_display": date_display, "_rel_time": rel_time,
                "_state_change_display": state_change_display, "_state_change_rel": state_change_rel,
                "_periodic_last_display": periodic_last_display, "_periodic_last_rel": periodic_last_rel,
                "_schedule_display": schedule_display,
                "_state_cls": _state_cls(r.get("state", "")),
                "_has_trace": bool(r.get("has_trace")),
                "_prefect_url": prefect_url,
            })

        filtered_rows: list = list(all_rows)
        current_search = [""]
        current_state = ["All States"]
        current_sort   = ["Newest First"]

        def apply_filters():
            """Apply the current search and sort controls, then refresh the results."""
            s = current_search[0].lower()
            st = current_state[0]
            rs = [
                r for r in all_rows
                if (not s
                    or s in (r.get("topic") or "").lower()
                    or s in (r.get("finding") or "").lower()
                    or s in (r.get("prompt") or "").lower())
                and (st == "All States" or r.get("state") == st)
            ]
            if current_sort[0] == "Newest First":
                rs.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
            else:
                rs.sort(key=lambda r: (r.get("topic") or "").lower())
            filtered_rows.clear()
            filtered_rows.extend(rs)
            count_lbl.refresh()
            bb_table.refresh()

        @ui.refreshable
        def count_lbl():
            """Render the number of currently visible catalog entries."""
            n = len(filtered_rows)
            ui.label(f"Showing {n} row{'s' if n != 1 else ''}").classes("text-sm text-gray-500")

        detail_dialog = ui.dialog()
        detail_title = [None]
        detail_finding_raw = [None]
        detail_finding_md = [None]
        detail_trace_row = [None]
        detail_trace_iframe = [None]
        current_finding_text = [""]

        def toggle_finding_view(e):
            """Switch between rendered and source finding content.

            Args:
                e: UI event containing the selected row or changed value.
            """
            if e.value:
                detail_finding_md[0].set_content(current_finding_text[0])
                detail_finding_raw[0].set_visibility(False)
                detail_finding_md[0].set_visibility(True)
            else:
                detail_finding_raw[0].set_visibility(True)
                detail_finding_md[0].set_visibility(False)

        detail_prompt_section = [None]
        detail_prompt_label = [None]
        detail_finding_section = [None]

        with detail_dialog, ui.card().classes("w-full max-w-5xl p-6 gap-3"):
            detail_title[0] = ui.label("").classes("text-lg font-bold text-gray-900")

            # Each section opens independently -- see open_detail's `focus`
            # arg and the table's promptClick/findingClick/traceClick handlers.
            detail_prompt_section[0] = ui.column().classes("w-full gap-1")
            with detail_prompt_section[0]:
                ui.label("Prompt").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mt-2")
                with ui.element("div").classes("w-full max-h-64 overflow-auto bg-gray-50 border border-gray-200 rounded-lg p-3"):
                    detail_prompt_label[0] = ui.label("").classes("whitespace-pre-wrap font-mono text-xs text-gray-800")

            detail_finding_section[0] = ui.column().classes("w-full gap-1")
            with detail_finding_section[0]:
                with ui.row().classes("w-full items-center justify-between mt-2"):
                    ui.label("Finding").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide")
                    ui.switch("Render as Markdown", on_change=toggle_finding_view).props("dense").classes("text-xs")
                with ui.element("div").classes("w-full max-h-64 overflow-auto bg-gray-50 border border-gray-200 rounded-lg p-3"):
                    detail_finding_raw[0] = ui.label("").classes("whitespace-pre-wrap font-mono text-xs text-gray-800")
                    # sanitize=True (default) runs the rendered HTML through
                    # client-side DOMPurify before insertion -- safe even though
                    # `finding` is agent-produced, not hand-authored, content.
                    detail_finding_md[0] = ui.markdown("").classes("text-sm")
                    detail_finding_md[0].set_visibility(False)

            detail_trace_row[0] = ui.column().classes("w-full gap-1")
            with detail_trace_row[0]:
                ui.label("Trace").classes("text-xs font-semibold text-gray-400 uppercase tracking-wide mt-2")
                # Rendered, not dumped as raw markup -- trace.html is a
                # self-contained interactive viewer (pi-trace-extension), so
                # showing its source as text would defeat the point of it.
                # Sandboxed with scripts allowed but same-origin/top-navigation
                # denied: the viewer's own JS (collapsible tree, tabs) still
                # works, but it can't reach this app's cookies/storage or
                # navigate the parent page -- this is agent-produced content,
                # not fully trusted.
                detail_trace_iframe[0] = ui.element("iframe").props('sandbox="allow-scripts"').classes(
                    "w-full border border-gray-200 rounded-lg"
                ).style("height: 70vh;")
            ui.button("Close", on_click=detail_dialog.close).classes("mt-2 self-end").props("flat")

        def open_detail(row: dict, focus: str = "finding"):
            """Open the detail dialog showing only the section that was clicked.

            Args:
                row: The clicked row.
                focus: 'prompt', 'finding', or 'trace' -- shows only that
                    section, hides the other two. Does nothing for
                    focus='trace' if the row has no trace.
            """
            if focus == "trace" and not row.get("_has_trace"):
                return

            title_type = row.get("topic") or row.get("post_type") or ""
            detail_title[0].set_text(f"#{row.get('id')} · {title_type}")

            detail_prompt_section[0].set_visibility(focus == "prompt")
            detail_finding_section[0].set_visibility(focus == "finding")
            detail_trace_row[0].set_visibility(focus == "trace")

            if focus == "prompt":
                detail_prompt_label[0].set_text(row.get("prompt") or "(empty)")
                detail_trace_iframe[0].props('src="about:blank"')
            elif focus == "finding":
                current_finding_text[0] = row.get("finding") or "(empty)"
                detail_finding_raw[0].set_text(current_finding_text[0])
                if detail_finding_md[0].visible:
                    detail_finding_md[0].set_content(current_finding_text[0])
                detail_trace_iframe[0].props('src="about:blank"')
            else:
                # A normal iframe `src` navigation -- the browser fetches this
                # over plain HTTP, independent of NiceGUI's websocket RPC
                # channel, so a multi-megabyte trace.html doesn't hit the
                # websocket's per-message size limit (see get_trace_html).
                detail_trace_iframe[0].props(f'src="/ui/blackboard/trace/{row.get("id")}"')

            detail_dialog.open()

        add_task_dialog = ui.dialog()
        add_task_error = [None]

        def toggle_interval_visibility(e):
            """Show scheduling controls for the selected task type.

            Args:
                e: UI event containing the selected row or changed value.
            """
            add_task_interval[0].set_visibility(e.value == "periodic")

        with add_task_dialog, ui.card().classes("w-full max-w-xl p-6 gap-3"):
            ui.label("Add AI Task to Blackboard").classes("text-lg font-bold text-gray-900")
            ui.label(
                "Inserts a post_type='run_me' row -- a task that isn't a follow-up to "
                "any finding. The orchestrator picks it up on its next poll."
            ).classes("text-xs text-gray-500")
            add_topic = ui.input(
                label="Topic (optional)",
                placeholder="e.g. code-analysis-report",
            ).classes("w-full").props("outlined dense")
            add_task_prompt = ui.textarea(
                label="Prompt",
                placeholder='e.g. Clone <repo-url>, analyse it and write a report to /output/report.pdf.',
            ).classes("w-full").props("outlined dense")
            add_task_schedule = ui.select(
                options=["once", "periodic"],
                value="once",
                label="Schedule",
                on_change=toggle_interval_visibility,
            ).classes("w-full").props("outlined dense options-dense")
            add_task_interval = [ui.number(
                label="Interval (minutes)",
                min=1,
                value=1440,
            ).classes("w-full").props("outlined dense")]
            add_task_interval[0].set_visibility(False)
            add_task_error[0] = ui.label("").classes("text-xs text-red-600")

            def submit_add_task():
                """Validate the task form and create a Blackboard task."""
                prompt_text = (add_task_prompt.value or "").strip()
                if not prompt_text:
                    add_task_error[0].set_text("Prompt is required.")
                    return
                interval = int(add_task_interval[0].value) if add_task_schedule.value == "periodic" else None
                topic = (add_topic.value or "").strip() or None
                try:
                    new_id = blackboard_client.insert_initial_task(prompt_text, interval, topic)
                except Exception as e:
                    add_task_error[0].set_text(f"Could not add task: {e}")
                    return
                ui.notify(f"Added task #{new_id}", type="positive")
                add_task_dialog.close()
                ui.navigate.to("/ui/blackboard")

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Cancel", on_click=add_task_dialog.close).props("flat")
                ui.button("Add Task", on_click=submit_add_task).props("unelevated color=primary")

        @ui.refreshable
        def bb_table():
            """Render filtered Blackboard tasks and row actions."""
            if not filtered_rows:
                ui.label("No blackboard rows match your search.").classes("text-sm text-gray-400 py-8 text-center w-full")
                return

            tbl = ui.table(
                columns=[
                    {"name": "actions",     "label": "",                    "field": "id",                         "align": "right"},
                    {"name": "id",          "label": "ID",                  "field": "id",                         "align": "left"},
                    {"name": "post_type",   "label": "Post Type",           "field": "post_type",                  "align": "left", "sortable": True},
                    {"name": "state",       "label": "State",               "field": "state",                      "align": "left"},
                    {"name": "topic",   "label": "Topic",               "field": "topic",                  "align": "left", "sortable": True},
                    {"name": "schedule",    "label": "Schedule",            "field": "_schedule_display",          "align": "left"},
                    {"name": "trace",       "label": "Trace",               "field": "_has_trace",                 "align": "left"},
                    {"name": "created",     "label": "Created",             "field": "created_at",                 "align": "left", "sortable": True},
                    {"name": "changed",     "label": "Last State Change",   "field": "last_state_change",          "align": "left"},
                    {"name": "periodic_last", "label": "Periodic Last Run", "field": "periodic_last_triggered_at", "align": "left"},
                ],
                rows=filtered_rows,
                row_key="id",
                pagination={"rowsPerPage": 15, "sortBy": "created_at", "descending": True},
            ).classes("w-full")

            tbl.add_slot("body-cell-post_type", r'''
                <q-td :props="props">
                    <span class="text-xs px-2 py-0.5 rounded-full font-medium"
                          :class="props.row.post_type === 'run_me' ? 'bg-indigo-100 text-indigo-700' : 'bg-teal-100 text-teal-700'">
                        {{ props.row.post_type }}
                    </span>
                </q-td>''')

            tbl.add_slot("body-cell-state", r'''
                <q-td :props="props">
                    <span :class="'px-2 py-0.5 rounded-full text-xs font-medium ' + props.row._state_cls">{{ props.row.state }}</span>
                </q-td>''')

            tbl.add_slot("body-cell-trace", r'''
                <q-td :props="props">
                    <span v-if="props.row._has_trace"
                          class="bg-blue-100 text-blue-700 text-xs px-2 py-0.5 rounded-full font-medium cursor-pointer hover:bg-blue-200"
                          @click="$parent.$emit('traceClick', props.row)">available</span>
                    <span v-else class="text-xs text-gray-400">—</span>
                </q-td>''')

            tbl.add_slot("body-cell-created", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._date_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._rel_time }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-changed", r'''
                <q-td :props="props">
                    <div class="text-sm text-gray-700">{{ props.row._state_change_display }}</div>
                    <div class="text-xs text-gray-400">{{ props.row._state_change_rel }}</div>
                </q-td>''')

            tbl.add_slot("body-cell-periodic_last", r'''
                <q-td :props="props">
                    <div v-if="props.row.periodic_last_triggered_at" class="text-sm text-gray-700">{{ props.row._periodic_last_display }}</div>
                    <div v-if="props.row.periodic_last_triggered_at" class="text-xs text-gray-400">{{ props.row._periodic_last_rel }}</div>
                    <span v-else class="text-xs text-gray-400">—</span>
                </q-td>''')

            tbl.add_slot("body-cell-actions", r'''
                <q-td :props="props">
                    <q-btn flat round dense icon="more_vert" size="sm" class="text-gray-400">
                        <q-menu>
                            <q-list dense>
                                <q-item clickable v-close-popup @click="$parent.$emit('promptClick', props.row)">
                                    <q-item-section>Show prompt</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup @click="$parent.$emit('findingClick', props.row)">
                                    <q-item-section>Show finding</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup
                                        :href="props.row._prefect_url || undefined"
                                        :target="props.row._prefect_url ? '_blank' : undefined"
                                        @click="!props.row._prefect_url && $parent.$emit('prefectClick', props.row)">
                                    <q-item-section>Open Prefect run</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup @click="$parent.$emit('setWaitingClick', props.row)">
                                    <q-item-section>Set to waiting (new)</q-item-section>
                                </q-item>
                                <q-item clickable v-close-popup @click="$parent.$emit('setDismissedClick', props.row)">
                                    <q-item-section>Dismiss</q-item-section>
                                </q-item>
                            </q-list>
                        </q-menu>
                    </q-btn>
                </q-td>''')

            def on_prompt_click(e):
                """Open the selected task prompt.

                Args:
                    e: UI event containing the selected row or changed value.
                """
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    open_detail(row, focus="prompt")

            def on_finding_click(e):
                """Open the selected task finding.

                Args:
                    e: UI event containing the selected row or changed value.
                """
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    open_detail(row, focus="finding")

            def on_trace_click(e):
                """Open the selected task execution trace.

                Args:
                    e: UI event containing the selected row or changed value.
                """
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    open_detail(row, focus="trace")

            def on_prefect_click(e):
                # Only fires for rows with no triggered run -- rows that have
                # one open the Prefect UI directly via the menu item's own
                # href (see the body-cell-actions slot).
                """Open the selected task in Prefect.

                Args:
                    e: UI event containing the selected row or changed value.
                """
                ui.notify("No Prefect run recorded for this row yet.", type="info")

            def _set_state(row: dict, state: str):
                """Update a task state and refresh the task list.

                Args:
                    row: Selected catalog or task record.
                    state: Current workflow selections or task state.
                """
                try:
                    blackboard_client.set_state(row["id"], state)
                except Exception as e:
                    ui.notify(f"Could not update task_runs.id={row['id']}: {e}", type="negative")
                    return
                ui.notify(f"task_runs.id={row['id']} set to '{state}'", type="positive")
                ui.navigate.to("/ui/blackboard")

            def on_set_waiting_click(e):
                """Mark the selected task as waiting.

                Args:
                    e: UI event containing the selected row or changed value.
                """
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    _set_state(row, "waiting")

            def on_set_dismissed_click(e):
                """Dismiss the selected task.

                Args:
                    e: UI event containing the selected row or changed value.
                """
                row = e.args[0] if isinstance(e.args, list) else e.args
                if row:
                    _set_state(row, "dismissed")

            tbl.on("promptClick", on_prompt_click)
            tbl.on("findingClick", on_finding_click)
            tbl.on("traceClick", on_trace_click)
            tbl.on("prefectClick", on_prefect_click)
            tbl.on("setWaitingClick", on_set_waiting_click)
            tbl.on("setDismissedClick", on_set_dismissed_click)

        with ui.column().classes("epi-page"):
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("AI Blackboard").classes("epi-page-title")
                    ui.label("Seeded/recurring tasks and results published by one-shot agent runs, and the follow-up runs triggered from them.").classes("epi-page-description")
                with ui.row().classes("items-center gap-2"):
                    def run_orchestrator_now():
                        """Trigger an immediate Blackboard orchestrator run."""
                        from app.clients import prefect as prefect_client
                        try:
                            result = prefect_client.trigger_orchestrator_run()
                        except Exception as e:
                            ui.notify(f"Could not trigger orchestrator run: {e}", type="negative")
                            return
                        ui.notify(f"Orchestrator run triggered (flow run {result['prefect_flow_run_id']})", type="positive")

                    ui.button(
                        "Run Orchestrator",
                        icon="bolt",
                        on_click=run_orchestrator_now,
                    ).props(
                        f"unelevated color=primary no-caps {'disabled' if not settings.prefect_api_url else ''}"
                    ).tooltip("Trigger an out-of-cycle blackboard-orchestrator run instead of waiting for the hourly poll.")
                    ui.button("Add AI task to Blackboard", icon="add", on_click=add_task_dialog.open).props("unelevated color=primary no-caps")

            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes("epi-search"):
                    ui.icon("search").classes("text-gray-400 shrink-0")
                    ui.input(
                        placeholder="Search by topic, prompt, or finding content...",
                        on_change=lambda e: (current_search.__setitem__(0, e.value), apply_filters()),
                    ).props("borderless dense").classes("flex-1 text-sm")
                ui.select(
                    options=["All States", "waiting", "dispatching_run", "running",
                             "waiting_for_next_periodic_run", "resolved", "failed", "dismissed"],
                    value="All States",
                    on_change=lambda e: (current_state.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")
                ui.select(
                    options=["Newest First", "Topic A–Z"],
                    value="Newest First",
                    on_change=lambda e: (current_sort.__setitem__(0, e.value), apply_filters()),
                ).props("outlined dense options-dense").classes("text-sm")

            count_lbl()

            with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-x-auto"):
                bb_table()

            # ---- Routing Rules: topic -> follow-up prompt template ----
            # Read by workflow-prefect__ai-blackboard-orchestrator's
            # orchestrator to chain a published finding into a new run. The
            # `blackboard` DB user has no DELETE here, so a rule is retired
            # by toggling `enabled`, never removed.
            try:
                rr_rows = blackboard_client.list_routing_rules()
                rr_load_error = None
            except Exception as e:
                rr_rows = []
                rr_load_error = str(e)

            rr_dialog = ui.dialog()
            rr_error = [None]

            with rr_dialog, ui.card().classes("w-full max-w-2xl p-6 gap-3"):
                rr_dialog_title = ui.label("").classes("text-lg font-bold text-gray-900")
                ui.label(
                    "Maps the topic of a published finding to the prompt the orchestrator "
                    "triggers next. Placeholders: $finding, $prompt, $id, $topic "
                    "($$ for a literal $). No other logic -- the whole finding is substituted as-is."
                ).classes("text-xs text-gray-500")
                rr_topic = ui.input(
                    label="Topic",
                    placeholder="e.g. code-analysis-report",
                ).classes("w-full").props("outlined dense")
                rr_template = ui.textarea(
                    label="Prompt template",
                    placeholder="A report (task_runs.id=$id) found:\n\n$finding\n\nClone the repo, verify each finding, fix real bugs, open a PR.",
                ).classes("w-full").props("outlined dense autogrow")
                rr_enabled = ui.switch("Enabled", value=True)
                rr_error[0] = ui.label("").classes("text-xs text-red-600")

                def submit_rr():
                    """Save the routing rule form and refresh the rule table."""
                    tt = (rr_topic.value or "").strip()
                    tmpl = (rr_template.value or "").strip()
                    if not tt or not tmpl:
                        rr_error[0].set_text("Topic and Prompt template are both required.")
                        return
                    try:
                        blackboard_client.upsert_routing_rule(tt, tmpl, bool(rr_enabled.value))
                    except Exception as e:
                        rr_error[0].set_text(f"Could not save: {e}")
                        return
                    ui.notify(f"Saved routing rule '{tt}'", type="positive")
                    rr_dialog.close()
                    ui.navigate.to("/ui/blackboard")

                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("Cancel", on_click=rr_dialog.close).props("flat")
                    ui.button("Save", on_click=submit_rr).props("unelevated color=primary")

            def open_rr_dialog(rule: dict | None):
                """Open the routing rule editor with the selected rule values.

                Args:
                    rule: Routing rule to edit, or None to create one.
                """
                rr_error[0].set_text("")
                if rule:
                    rr_dialog_title.set_text(f"Edit routing rule · {rule['topic']}")
                    rr_topic.set_value(rule["topic"])
                    rr_topic.props(add="readonly")
                    rr_template.set_value(rule["prompt_template"])
                    rr_enabled.set_value(bool(rule["enabled"]))
                else:
                    rr_dialog_title.set_text("Add routing rule")
                    rr_topic.set_value("")
                    rr_topic.props(remove="readonly")
                    rr_template.set_value("")
                    rr_enabled.set_value(True)
                rr_dialog.open()

            ui.separator().classes("mt-4")
            with ui.row().classes("w-full items-start justify-between gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label("Routing Rules").classes("text-2xl font-bold text-gray-900")
                    ui.label(
                        "topic → the follow-up prompt the orchestrator triggers when a "
                        "someone_take_over row with that topic is published."
                    ).classes("epi-page-description")
                ui.button("Add routing rule", icon="add", on_click=lambda: open_rr_dialog(None)).props("unelevated color=primary no-caps")

            if rr_load_error:
                if "routing_rules" in rr_load_error and ("doesn't exist" in rr_load_error or "does not exist" in rr_load_error or "1146" in rr_load_error):
                    error_label("The routing_rules table doesn't exist yet — create it "
                                 "on the blackboard database as the MariaDB root user "
                                 "(id, topic UNIQUE, prompt_template, enabled, created_at, updated_at).")
                else:
                    error_label(f"Could not load routing rules: {rr_load_error}")
            elif not rr_rows:
                ui.label("No routing rules yet. Add one to chain a finding into a follow-up run.").classes("text-sm text-gray-400 py-6 text-center w-full")
            else:
                with ui.element("div").classes("w-full border border-gray-200 rounded-xl bg-white overflow-x-auto"):
                    rr_tbl = ui.table(
                        columns=[
                            {"name": "actions",         "label": "",                "field": "id",              "align": "right"},
                            {"name": "topic",       "label": "Topic",           "field": "topic",       "align": "left"},
                            {"name": "enabled",         "label": "Enabled",         "field": "enabled",         "align": "left"},
                            {"name": "prompt_template", "label": "Prompt Template", "field": "prompt_template", "align": "left"},
                            {"name": "updated_at",      "label": "Updated",         "field": "updated_at",      "align": "left"},
                        ],
                        rows=list(rr_rows),
                        row_key="id",
                        pagination={"rowsPerPage": 10},
                    ).classes("w-full")

                    rr_tbl.add_slot("body-cell-enabled", r'''
                        <q-td :props="props">
                            <span class="text-xs px-2 py-0.5 rounded-full font-medium"
                                  :class="props.row.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'">
                                {{ props.row.enabled ? 'enabled' : 'disabled' }}
                            </span>
                        </q-td>''')

                    rr_tbl.add_slot("body-cell-prompt_template", r'''
                        <q-td :props="props">
                            <div class="max-w-xl whitespace-pre-wrap font-mono text-xs text-gray-600"
                                 style="display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;">{{ props.row.prompt_template }}</div>
                        </q-td>''')

                    rr_tbl.add_slot("body-cell-actions", r'''
                        <q-td :props="props">
                            <q-btn flat round dense icon="more_vert" size="sm" class="text-gray-400">
                                <q-menu>
                                    <q-list dense>
                                        <q-item clickable v-close-popup @click="$parent.$emit('rrEdit', props.row)">
                                            <q-item-section>Edit</q-item-section>
                                        </q-item>
                                        <q-item clickable v-close-popup @click="$parent.$emit('rrToggle', props.row)">
                                            <q-item-section>{{ props.row.enabled ? 'Disable' : 'Enable' }}</q-item-section>
                                        </q-item>
                                    </q-list>
                                </q-menu>
                            </q-btn>
                        </q-td>''')

                    def on_rr_edit(e):
                        """Open the selected routing rule for editing.

                        Args:
                            e: UI event containing the selected row or changed value.
                        """
                        row = e.args[0] if isinstance(e.args, list) else e.args
                        if row:
                            open_rr_dialog(row)

                    def on_rr_toggle(e):
                        """Toggle the selected routing rule and refresh the table.

                        Args:
                            e: UI event containing the selected row or changed value.
                        """
                        row = e.args[0] if isinstance(e.args, list) else e.args
                        if not row:
                            return
                        try:
                            blackboard_client.set_routing_rule_enabled(row["id"], not row["enabled"])
                        except Exception as ex:
                            ui.notify(f"Could not update rule: {ex}", type="negative")
                            return
                        ui.notify("Routing rule updated", type="positive")
                        ui.navigate.to("/ui/blackboard")

                    rr_tbl.on("rrEdit", on_rr_edit)
                    rr_tbl.on("rrToggle", on_rr_toggle)


    @_napp.get("/ui/blackboard/trace/{row_id}")
    def blackboard_trace(row_id: int):
        # Plain HTTP GET, not a @ui.page -- this is what the trace iframe's
        # `src` points at (see open_detail above). Session-cookie auth still
        # applies: NiceGUI's storage middleware is installed on this same
        # app regardless of route type, so app.storage.user is available
        # here exactly as it is inside a @ui.page.
        """Serve an authorized task trace as HTML for the trace iframe.

        Args:
            row_id: Identifier of the task whose trace is requested.
        """
        if not session.can("blackboard"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        trace_html = blackboard_client.get_trace_html(row_id)
        if trace_html is None:
            raise HTTPException(status_code=404, detail="No trace for this row")
        return HTMLResponse(content=trace_html)
