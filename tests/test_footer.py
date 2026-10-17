"""The footer of the landing page, shared by every UI page: one source, local logos, on every page."""

import asyncio

import httpx
import pytest
from nicegui import ui

from app.main import app
from app.ui import footer as footer_module
from tests.test_benchmarks_ui import _render


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


def test_landing_page_uses_the_same_footer_source():
    response = _get("/")
    assert response.status_code == 200
    assert footer_module.footer_html() in response.text
    assert footer_module.FOOTER_CSS in response.text
    assert "/*FOOTER_CSS*/" not in response.text and "<!--FOOTER_HTML-->" not in response.text
    assert "https://episerve.zib.de/img/logo" not in response.text


@pytest.mark.parametrize("name", ["logo_episerve_bw.png", "logo-bmftr.png"])
def test_the_logos_are_served_by_the_app(name):
    response = _get(f"/epi-static/img/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
