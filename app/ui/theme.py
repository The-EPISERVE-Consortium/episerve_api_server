"""Design tokens and stylesheet loading for application and authentication pages."""

from pathlib import Path

from nicegui import ui

BEIGE = "#f2eee5"
LINE = "#d4cfc5"
ACCENT = "#0772a0"
NAVY = "#1e3a5f"

_STYLES = Path(__file__).resolve().parent.parent / "static" / "css"
TOKENS_CSS = f"""
:root {{
  --epi-bg: {BEIGE};
  --epi-line: {LINE};
  --epi-accent: {ACCENT};
  --epi-navy: {NAVY};
  --epi-surface: #fff;
  --epi-text: #111827;
  --epi-muted: #6b7280;
  --epi-radius: 12px;
  --epi-page-padding: 24px 32px;
}}
"""
THEME_CSS = TOKENS_CSS + (_STYLES / "theme.css").read_text()
AUTH_CSS = TOKENS_CSS + (_STYLES / "auth.css").read_text()


def apply_theme() -> None:
    """Apply shared colors and application-scoped component styles."""
    ui.colors(primary=ACCENT)
    ui.add_css(THEME_CSS)


def apply_auth_theme() -> None:
    """Apply the independent full-screen authentication layout."""
    ui.colors(primary=ACCENT)
    ui.add_css(AUTH_CSS)
