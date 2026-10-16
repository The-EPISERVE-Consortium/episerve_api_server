"""The footer of the landing page, shared by every UI page: one source, local logos, on every page."""

import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from nicegui import ui

from app.main import app
from app.ui import footer as footer_module
from app.ui import benchmarks_page, pages
from tests.test_benchmarks_ui import _render
from tests.ui_harness import render


def _get(path):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            return await http.get(path)

    return asyncio.run(go())


def _footers(out):
    return out["find"](ui.footer), [e.content for e in out["find"](ui.html)]


def test_ui_pages_show_the_footer_markup_of_the_landing_page(monkeypatch):
    out = _render(monkeypatch)
    footers, contents = _footers(out)
    assert len(footers) == 1
    assert footer_module.footer_html() in contents


def test_footer_has_the_description_the_legal_link_the_funding_logo_and_the_copyright():
    html = footer_module.footer_html()
    assert "Open simulation platform and data service for infectious disease research and pandemic preparedness in Germany." in html
    assert "Provided by the EPISERVE Consortium and hosted at Zuse Institute Berlin." in html
    assert '<a href="https://zib.de/impressum">Legal Notice and Data Protection</a>' in html
    assert 'alt="Federal Ministry of Research, Technology and Space"' in html
    assert f"&copy; {date.today().year} EPISERVE Consortium. All rights reserved." in html
    assert html.count("<li>") == 1                        # the landing page lists the legal notice only


def test_footer_logos_come_from_this_app_not_from_another_host():
    html = footer_module.footer_html()
    assert 'src="/epi-static/img/logo_episerve_bw.png"' in html and 'src="/epi-static/img/logo-bmftr.png"' in html
    assert "https://episerve.zib.de" not in html


def test_footer_scrolls_with_the_page_instead_of_being_fixed(monkeypatch):
    out = _render(monkeypatch)
    assert out["client"].layout.props["view"][9] == "f"            # lower case = not fixed


def test_landing_page_uses_the_same_footer_source():
    response = _get("/")
    assert response.status_code == 200
    assert footer_module.footer_html() in response.text
    assert footer_module.FOOTER_CSS in response.text
    assert "/*FOOTER_CSS*/" not in response.text and "<!--FOOTER_HTML-->" not in response.text
    assert "https://episerve.zib.de/img/logo" not in response.text


def test_login_page_has_the_footer_too(monkeypatch):
    monkeypatch.setattr(pages, "_napp", SimpleNamespace(storage=SimpleNamespace(user={})))
    out = render(monkeypatch, benchmarks_page, "/login")      # the module only supplies _is_logged_in
    assert out["status"] == 200
    footers, contents = _footers(out)
    assert len(footers) == 1 and footer_module.footer_html() in contents


@pytest.mark.parametrize("name", ["logo_episerve_bw.png", "logo-bmftr.png"])
def test_the_logos_are_served_by_the_app(name):
    response = _get(f"/epi-static/img/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_logo_files_are_part_of_the_package():
    for name in ("logo_episerve_bw.png", "logo-bmftr.png"):
        assert (footer_module.STATIC_DIR / "img" / name).is_file()
