"""Bot-check resilience: per-host cookie echo (__cf_bm), ONE delayed retry
with browser headers after a JS challenge, and an optional headless-Chromium
last resort that also harvests challenge-grade cookies. Retry and fallback
are OFF until configure() supplies them, so the rest of the hermetic suite
keeps its single-request semantics (and this file resets them on teardown)."""
import time

import pytest

from rssgate import net


class Resp:
    def __init__(self, status=200, text="<html><body><p>Real article "
                 "text here for the extractor to chew on.</p></body></html>",
                 cookies=None):
        self.status_code, self.ok, self.text = status, status < 400, text
        self.headers = {"content-type": "text/html"}
        self.cookies = cookies or {}


def CF():
    return Resp(403, "<html><head>Just a moment... cloudflare</head></html>",
                {"cf": "x"})


@pytest.fixture(autouse=True)
def _reset_net():
    net._jar.clear()
    net._browser_last.clear()
    yield
    net.configure({"fetch": {"per_host_interval": 0,
                             "challenge_retry_seconds": 0,
                             "browser_fallback": False}})
    net._jar.clear()
    net._browser_last.clear()


def test_cookies_echoed_to_same_host(monkeypatch):
    net.configure({"fetch": {"per_host_interval": 0}})
    seen = []

    def fake(url, headers=None, **kw):
        seen.append(kw.get("cookies"))
        return Resp(cookies={"__cf_bm": "abc123"})

    monkeypatch.setattr("requests.get", fake)
    net.get("https://cf.test/1")
    net.get("https://cf.test/2")
    assert not seen[0]
    assert seen[1] == {"__cf_bm": "abc123"}


def test_cookies_are_per_host(monkeypatch):
    net.configure({"fetch": {"per_host_interval": 0}})
    seen = {}

    def fake(url, headers=None, **kw):
        seen[url] = kw.get("cookies")
        return Resp(cookies={"x": "1"} if "a.test" in url else {})

    monkeypatch.setattr("requests.get", fake)
    net.get("https://a.test/1")
    net.get("https://b.test/1")
    assert seen["https://a.test/1"] in (None, {})
    assert seen["https://b.test/1"] in (None, {})   # a.test's cookie not leaked


def test_no_retry_until_configured(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.get", lambda url, **kw: calls.append(1) or CF())
    net.configure({"fetch": {"per_host_interval": 0, "challenge_retry_seconds": 0}})
    r = net.get("https://cf.test/1")
    assert net.is_challenge(r)
    assert len(calls) == 1


def test_retry_with_browser_headers_recovers(monkeypatch):
    calls = []

    def fake(url, headers=None, **kw):
        calls.append(headers)
        return CF() if len(calls) == 1 else Resp()

    monkeypatch.setattr("requests.get", fake)
    net.configure({"fetch": {"per_host_interval": 0,
                             "challenge_retry_seconds": 0.01,
                             "browser_fallback": False}})
    r = net.get("https://cf.test/1")
    assert r.ok
    assert len(calls) == 2
    assert "accept-language" in calls[1]          # fuller profile on retry
    assert "accept-language" not in (calls[0] or {})


def test_browser_fallback_used_and_returned(monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, **kw: CF())
    browsed = net._Browsed(200, "<html>real content</html>", "https://cf.test/1")
    got = []
    monkeypatch.setattr(net, "browser_get", lambda url, **kw: got.append(url) or browsed)
    net.configure({"fetch": {"per_host_interval": 0,
                             "challenge_retry_seconds": 0.01,
                             "browser_fallback": True}})
    r = net.get("https://cf.test/1")
    assert r is browsed
    assert got == ["https://cf.test/1"]


def test_all_failures_return_challenge_as_one_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr("requests.get", lambda url, **kw: calls.append(1) or CF())
    monkeypatch.setattr(net, "browser_get", lambda url, **kw: None)
    net.configure({"fetch": {"per_host_interval": 0,
                             "challenge_retry_seconds": 0.01,
                             "browser_fallback": True}})
    r = net.get("https://cf.test/1")
    assert net.is_challenge(r)
    assert len(calls) == 2          # plain + retry; caller counts ONE challenge


def test_browser_get_none_without_playwright(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert net.browser_get("https://cf.test/1") is None


def test_browser_get_cooldown_blocks_second_launch(monkeypatch):
    launches = []
    monkeypatch.setattr(net, "_browser_fetch",
                        lambda url, host, timeout_s=30: launches.append(url) or None)
    assert net.browser_get("https://cf.test/1") is None   # first: allowed, records time
    assert launches == ["https://cf.test/1"]
    assert net.browser_get("https://cf.test/1") is None   # inside cooldown: no launch
    assert launches == ["https://cf.test/1"]


def test_browsed_response_shape():
    b = net._Browsed(200, "<html>x</html>", "https://cf.test/1")
    assert b.ok and b.status_code == 200 and b.text == "<html>x</html>"
    assert list(b.iter_content(4))
    b5 = net._Browsed(403, "Just a moment... cloudflare", "u")
    assert not b5.ok and net.is_challenge(b5)
