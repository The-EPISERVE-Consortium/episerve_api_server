"""The footer of the landing page, shared by every UI page (one source for the CSS and the markup)."""

from datetime import date
from pathlib import Path

from nicegui import ui, app as _napp

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_URL = "/epi-static"
_napp.add_static_files(STATIC_URL, STATIC_DIR)

FOOTER_CSS = """
.site-footer {
  background: #ede8da;
  font-size: 0.85rem;
  color: #555;
}
.footer-main {
  display: flex;
  gap: 0;
  padding: 2rem 3rem;
  flex-wrap: wrap;
}
.footer-col {
  flex: 1 1 220px;
  padding: 0 2rem;
}
.footer-col:not(:last-child) {
  border-right: 1px solid rgba(0,0,0,0.15);
}
.footer-col:first-child { padding-left: 0; }
.footer-col:last-child { padding-right: 0; }
.footer-brand-row {
  display: flex;
  align-items: flex-start;
  gap: 1rem;
}
.footer-logo-img {
  width: 120px;
  flex-shrink: 0;
}
.footer-desc {
  font-size: 0.82rem;
  line-height: 1.55;
  color: #206b82;
  margin: 0;
}
.footer-links {
  list-style: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  height: 100%;
  gap: 0.75rem;
  text-align: center;
}
.footer-links a {
  color: #206b82;
  text-decoration: none;
  font-size: 0.88rem;
}
.footer-links a:hover { text-decoration: underline; }
.footer-funding {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 0.5rem;
  text-align: center;
}
.footer-funding-label {
  font-size: 0.8rem;
  color: #777;
}
.footer-funding-img {
  max-width: 160px;
}
.footer-bottom {
  border-top: 1px solid rgba(0,0,0,0.12);
  text-align: center;
  padding: 0.75rem 2rem;
  font-size: 0.8rem;
  color: #888;
}

@media (max-width: 600px) {
  .footer-main { flex-direction: column; padding: 1.5rem; }
  .footer-col { border-right: none !important; border-bottom: 1px solid rgba(0,0,0,0.12); padding: 1rem 0; }
  .footer-col:last-child { border-bottom: none; }
}
"""

_FOOTER_HTML = """<footer class="site-footer">
  <div class="footer-main">
    <div class="footer-col">
      <div class="footer-brand-row">
        <img src="@@STATIC@@/img/logo_episerve_bw.png" alt="EPISERVE" class="footer-logo-img">
        <p class="footer-desc">Open simulation platform and data service for infectious disease research and pandemic preparedness in Germany. Provided by the EPISERVE Consortium and hosted at Zuse Institute Berlin.</p>
      </div>
    </div>
    <div class="footer-col">
      <ul class="footer-links">
        <li><a href="https://zib.de/impressum">Legal Notice and Data Protection</a></li>
      </ul>
    </div>
    <div class="footer-col">
      <div class="footer-funding">
        <img src="@@STATIC@@/img/logo-bmftr.png" alt="Federal Ministry of Research, Technology and Space" class="footer-funding-img">
      </div>
    </div>
  </div>
  <div class="footer-bottom">
    &copy; @@YEAR@@ EPISERVE Consortium. All rights reserved.
  </div>
</footer>
"""


def footer_html() -> str:
    """The footer markup with the current year."""
    return _FOOTER_HTML.replace("@@STATIC@@", STATIC_URL).replace("@@YEAR@@", str(date.today().year))


def footer() -> None:
    """Add the footer at the end of a UI page; it scrolls with the content. Call it once per page."""
    ui.add_css(FOOTER_CSS)
    with ui.footer(fixed=False).classes("p-0 bg-transparent"):
        ui.html(footer_html(), sanitize=False).classes("w-full")
