"""The look of the UI pages: a beige page, the content in a white card with rounded corners, a blue accent.

``apply_theme()`` is called by the page header, so every page that has the top bar gets it; the login pages, which
are full-bleed, do not.
"""

from nicegui import ui

# One place for the colours. The beige is also the footer's.
BEIGE = "#f2eee5"
LINE = "#d4cfc5"
ACCENT = "#0772a0"
NAVY = "#1e3a5f"

THEME_CSS = f"""
:root {{ --epi-bg: {BEIGE}; --epi-line: {LINE}; --epi-accent: {ACCENT}; --epi-navy: {NAVY}; }}
body, .q-layout, .q-page-container {{ background: var(--epi-bg); }}
.q-page {{ padding: 24px 0 32px; min-height: 0 !important; }}
.nicegui-content {{
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 1px 3px rgba(60, 50, 30, 0.08);
  width: min(calc(100% - 48px), 1400px);
  margin: 0 auto;
  padding: 0 !important;
}}
.q-tab--active {{ color: var(--epi-accent) !important; }}
.q-tab__indicator {{ color: var(--epi-accent) !important; }}
"""


def apply_theme() -> None:
    ui.colors(primary=ACCENT)
    ui.add_css(THEME_CSS)
