import pytest
import requests

from app.clients import doip


class _Resp:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _patch(monkeypatch, resp):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return resp

    monkeypatch.setattr(doip.requests, "get", fake_get)
    return calls


def test_asks_the_gateway_for_the_first_bytes_only(monkeypatch):
    calls = _patch(monkeypatch, _Resp(b"a,b\n1,2\n"))

    doip.fetch_text_head("https://doip/doip/retrieve/Q1/x.csv", max_bytes=1000)

    assert calls[0]["params"] == {"size": 1000}


def test_cut_response_drops_the_incomplete_last_line(monkeypatch):
    _patch(monkeypatch, _Resp(b"a,b\n1,2\n3,", status_code=206))

    assert doip.fetch_text_head("u") == "a,b\n1,2\n"


def test_cut_response_without_any_newline_is_kept(monkeypatch):
    _patch(monkeypatch, _Resp(b"a,b,c", status_code=206))

    assert doip.fetch_text_head("u") == "a,b,c"


def test_complete_response_is_returned_untouched(monkeypatch):
    # 200: the whole file (small file, or a gateway that ignores ``size``)
    _patch(monkeypatch, _Resp(b"a,b\n1,2\n3,4"))

    assert doip.fetch_text_head("u") == "a,b\n1,2\n3,4"


def test_cut_inside_a_multibyte_character_does_not_fail(monkeypatch):
    _patch(monkeypatch, _Resp("a\nä".encode()[:-1], status_code=206))  # last char cut in half

    assert doip.fetch_text_head("u") == "a\n"


def test_error_status_raises(monkeypatch):
    _patch(monkeypatch, _Resp(b"", status_code=502))

    with pytest.raises(requests.HTTPError):
        doip.fetch_text_head("u")
