"""Browser rungs 2 and 3, hermetically: rules-based wall clearing and the
LLM rescue with its hard action allowlist. No real browser: fake pages."""
import re

import pytest

from rssgate import browser, net

GOOD = "<html><body>" + "<p>Real article text. </p>" * 60 + "</body></html>"
STUCK_HTML = "<html><head><title>Just a moment... | Cloudflare</title></head></html>"


class El:
    def __init__(self, role, name, page):
        self.role, self.name, self.page = role, name, page

    def _visible(self):
        return self in self.page.els

    def is_visible(self):
        return self._visible()

    def click(self, timeout=None):
        if not self._visible():
            raise RuntimeError("not visible")
        self.page.clicks.append(self.name)
        self.page.els.remove(self)
        if self.page.after_click:
            self.html_hook = self.page.after_click.pop(0)
            self.page.html = self.html_hook


class Loc:
    def __init__(self, els, page):
        self._els = els
        self._page = page

    @property
    def first(self):
        if self._els:
            return self._els[0]
        hidden = El("hidden", "hidden", self._page)   # never in page.els
        return hidden


class Page:
    def __init__(self, els=None, html=STUCK_HTML, snapshot=None, after_click=None):
        self.els = list(els or [])
        self.html = html
        self.clicks = []
        self.snap = snapshot or {"title": "t", "texts": [], "body": "x" * 2000}
        self.after_click = list(after_click or [])

    def get_by_role(self, role, name=None):
        hits = [e for e in self.els if e.role == role
                and (name is None or name.search(e.name))]
        return Loc(hits, self)

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self.html

    def evaluate(self, js, arg=None):
        return self.snap


class LLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, max_tokens=1200, model=""):
        self.calls += 1
        return (self.replies.pop(0) if self.replies else '{"action":"give_up"}', {})


# --- rung 2: rules ----------------------------------------------------------

def test_clear_walls_clicks_consent_once():
    p = Page(els=[El("button", "Accept All Cookies", None)], html=GOOD)
    p.els[0].page = p
    assert browser.clear_walls(p) == 1
    assert p.clicks == ["Accept All Cookies"]


def test_clear_walls_caps_clicks():
    els = [El("button", "Accept cookies", None) for _ in range(10)]
    p = Page(els=els, html=GOOD)
    for e in p.els:
        e.page = p
    assert browser.clear_walls(p) == browser.WALL_CLICKS


def test_clear_walls_no_match_is_noop():
    p = Page(els=[], html=GOOD)
    assert browser.clear_walls(p) == 0
    assert p.clicks == []


# --- rung 3: LLM rescue -----------------------------------------------------

def test_rescue_clicks_allowlisted_text_and_unsticks():
    el = El("button", "I agree", None)
    p = Page(els=[el], html=STUCK_HTML,
             snapshot={"title": "Just a moment...", "texts": ["I agree"],
                       "body": "checking your browser"},
             after_click=[GOOD])
    el.page = p
    ok = browser.rescue(p, LLM(['{"action":"click","text":"I agree"}']),
                        max_steps=2, budget_s=10)
    assert ok and p.clicks == ["I agree"]


def test_rescue_refuses_hallucinated_text():
    p = Page(html=STUCK_HTML,
             snapshot={"title": "t", "texts": ["Continue"], "body": "robot?"})
    llm = LLM(['{"action":"click","text":"Sign in with Google"}'])
    assert browser.rescue(p, llm, max_steps=2, budget_s=10) is False
    assert p.clicks == [] and llm.calls == 1


def test_rescue_rejects_unknown_action_and_garbage():
    p = Page(html=STUCK_HTML, snapshot={"title": "t", "texts": [], "body": "x"})
    assert browser.rescue(p, LLM(['{"action":"submit_form"}']), 2, 10) is False
    p2 = Page(html=STUCK_HTML, snapshot={"title": "t", "texts": [], "body": "x"})
    assert browser.rescue(p2, LLM(['not json at all']), 2, 10) is False


def test_rescue_step_cap_and_wait_cap():
    p = Page(html=STUCK_HTML, snapshot={"title": "t", "texts": [], "body": "x"})
    llm = LLM(['{"action":"wait","seconds":9999}'] * 10)
    assert browser.rescue(p, llm, max_steps=3, budget_s=10) is False
    assert llm.calls == 3                      # never more than max_steps


def test_rescue_llm_error_falls_back_to_page_state():
    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("llm down")
    p = Page(html=GOOD, snapshot={"title": "t", "texts": [], "body": "x"})
    assert browser.rescue(p, Boom(), max_steps=2, budget_s=10) is True


def test_is_stuck_regex():
    assert browser.is_stuck(STUCK_HTML)
    assert browser.is_stuck("<html>short</html>")          # tiny page = stuck
    assert not browser.is_stuck(GOOD)


# --- wiring -----------------------------------------------------------------

def test_fetch_none_without_playwright(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert browser.fetch("https://x.test/1", "x.test", {}) is None


def test_net_delegates_to_browser_fetch(monkeypatch):
    seen = {}

    def fake_fetch(url, host, fcfg, factory, timeout_s=30):
        seen.update(url=url, host=host, fcfg=fcfg, factory=factory)
        return net._Browsed(200, "<html>ok</html>", url)

    class CFR:                      # a minimal Cloudflare challenge response
        status_code, ok = 403, False
        text = "<html>Just a moment... cloudflare</html>"
        headers = {"cf-mitigated": "challenge"}
        cookies = {}

    from rssgate import browser as br
    monkeypatch.setattr(br, "fetch", fake_fetch)
    monkeypatch.setattr("requests.get", lambda url, **kw: CFR())
    net.configure({"fetch": {"per_host_interval": 0, "browser_fallback": True,
                             "challenge_retry_seconds": 0.01,
                             "llm_rescue": True}})
    net._jar.clear(); net._browser_last.clear()
    r = net.get("https://cf.test/1")
    assert r.ok and seen["host"] == "cf.test"
    assert seen["fcfg"].get("llm_rescue") is True


def test_rescue_reports_every_llm_call_for_accounting():
    """Rescue tokens are real spend: each model turn reaches on_usage."""
    el = El("button", "I agree", None)
    p = Page(els=[el], html=STUCK_HTML,
             snapshot={"title": "Just a moment...", "texts": ["I agree"],
                       "body": "checking your browser"},
             after_click=[GOOD])
    el.page = p
    got = []
    browser.rescue(p, LLM(['{"action":"click","text":"I agree"}']),
                   max_steps=2, budget_s=10,
                   on_usage=lambda llm, usage, ms: got.append(usage))
    assert len(got) == 1 and isinstance(got[0], dict)
