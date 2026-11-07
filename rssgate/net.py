"""Outbound HTTP for everything that talks to feed/article/image hosts.

One place for: the User-Agent (config fetch.user_agent, default below),
per-host politeness pacing (fetch.per_host_interval seconds between requests
to the same host, shared by every thread), a per-host COOKIE JAR (Cloudflare
plants __cf_bm on clean responses; never echoing it back is what makes a
fetcher look like a bot), and a two-step recovery when a page still hits a
JS challenge: one delayed retry with browser-like headers, then an optional
headless-Chromium fetch that also harvests challenge-grade cookies.

Retry and browser fallback are OFF until configure() runs (the app entry
point does it), so the hermetic suite keeps its single-request semantics.

We read public pages for one local reader; we do not republish or scrape at
scale. Cloudflare's challenge here is a machine problem (JS + TLS + cookies),
so the answer is cookie persistence and a real browser - not an LLM.

Sites ban bursts: adding a feed used to fetch ~100 article pages back-to-back
and got an IP blocked. Calls requests.get at call time, so tests that
monkeypatch requests.get or fetcher._get keep working.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 rssgate")

# Sent on the challenge retry: a challenge response usually means our
# minimal request profile tripped the bot rules, so echo a fuller set.
BROWSER_HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "accept-language": "en-CA,en;q=0.9",
    "accept-encoding": "gzip, deflate, br",
    "upgrade-insecure-requests": "1",
}

BLOCK_STATUSES = (403, 429)      # site says "go away": back the feed off
BROWSER_COOLDOWN = 600.0         # one Chromium launch per host per 10 min

_lock = threading.Lock()
_next_ok: dict[str, float] = {}  # host -> monotonic time of next allowed hit
_cfg = {"ua": "", "interval": 0.0, "challenge_retry": 0.0,
        "browser_fallback": False}

_jar_lock = threading.Lock()
_jar: dict[str, dict[str, str]] = {}      # host -> {cookie name: value}
_browser_last: dict[str, float] = {}      # host -> monotonic of last launch


def _store_cookie(host: str, name: str, value: str) -> None:
    if not host or not name or len(value) > 4096:
        return
    with _jar_lock:
        jar = _jar.setdefault(host, {})
        jar[name] = value
        if len(jar) > 25:                   # hostile/pathological sets
            for k in list(jar)[:-25]:
                del jar[k]


def _store_cookies(host: str, resp) -> None:
    src = getattr(resp, "cookies", None)
    if not host or src is None:
        return
    try:
        if hasattr(src, "items"):
            items = [(str(k), str(v)) for k, v in src.items()]
        else:
            items = [(c.name, c.value) for c in src]
    except Exception:  # noqa: BLE001 - fake responses in tests
        return
    for name, value in items:
        _store_cookie(host, name, value)


def _cookies_for(host: str) -> dict[str, str]:
    with _jar_lock:
        return dict(_jar.get(host, {}))


def _send(url: str, headers: dict | None, kw: dict) -> requests.Response:
    h = {"user-agent": user_agent()}
    h.update(headers or {})
    ck = dict(_cookies_for(host_of(url)))
    ck.update(kw.pop("cookies", None) or {})
    if ck:
        kw["cookies"] = ck
    return requests.get(url, headers=h, **kw)


def is_challenge(resp) -> bool:
    """A JavaScript bot check (Cloudflare "Just a moment..."): only a real
    browser passes, waiting does not help - so it is NOT a block to back off
    from. Detected by Cloudflare's explicit header or the interstitial page."""
    if getattr(resp, "status_code", 200) not in (403, 503):
        return False
    h = getattr(resp, "headers", {}) or {}
    if str(h.get("cf-mitigated", "")).lower() == "challenge":
        return True
    try:
        head = (resp.text or "")[:4000].lower()
    except Exception:  # noqa: BLE001
        return False
    return ("just a moment" in head and "cloudflare" in head) \
        or "challenge-platform" in head


def configure(cfg: dict) -> None:
    f = (cfg or {}).get("fetch", {}) or {}
    _cfg["ua"] = str(f.get("user_agent") or "").strip()
    try:
        _cfg["interval"] = max(0.0, float(f.get("per_host_interval", 3)))
    except (TypeError, ValueError):
        _cfg["interval"] = 3.0
    # Keys absent => OFF, not the config.py defaults: only the app's full
    # merged config turns recovery on, so a test that configures just pacing
    # keeps strict single-request semantics.
    try:
        _cfg["challenge_retry"] = max(0.0, float(f.get("challenge_retry_seconds", 0)))
    except (TypeError, ValueError):
        _cfg["challenge_retry"] = 0.0
    _cfg["browser_fallback"] = bool(f.get("browser_fallback", False))


def user_agent() -> str:
    return _cfg["ua"] or DEFAULT_UA


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def pace(url: str) -> float:
    """Block until this host may be hit again; returns seconds waited."""
    gap = _cfg["interval"]
    host = host_of(url)
    if gap <= 0 or not host:
        return 0.0
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_ok.get(host, 0.0))
        _next_ok[host] = slot + gap
    wait = slot - now
    if wait > 0:
        time.sleep(wait)
    return wait


def get(url: str, headers: dict | None = None, **kw) -> requests.Response:
    """GET with pacing, cookie echo, and (once configured) challenge recovery:
    one delayed retry with browser headers, then optional headless Chromium.
    A still-challenged response is returned as-is so callers keep their
    existing streak/excerpt logic - and it counts as ONE attempt, not three."""
    pace(url)
    resp = _send(url, headers, dict(kw))
    _store_cookies(host_of(url), resp)
    if not is_challenge(resp):
        return resp
    if _cfg["challenge_retry"] <= 0:
        return resp                        # unconfigured (hermetic tests): 1 request
    time.sleep(min(_cfg["challenge_retry"], 120.0))
    pace(url)
    retry_headers = dict(BROWSER_HEADERS)
    retry_headers.update(headers or {})
    resp2 = _send(url, retry_headers, dict(kw))
    _store_cookies(host_of(url), resp2)
    if not is_challenge(resp2):
        return resp2
    if _cfg["browser_fallback"]:
        browsed = browser_get(url)
        if browsed is not None and not is_challenge(browsed):
            return browsed
    return resp2


class _Browsed:
    """Minimal requests.Response surface for a headless-Chromium fetch."""

    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 8192):
        data = self.text.encode("utf-8", "replace")
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]


def browser_get(url: str, timeout_s: int = 30):
    """Last resort for a JS challenge: a real headless Chromium runs the
    Cloudflare script and we keep its cookies. Returns _Browsed or None
    (playwright missing, cooldown active, or any error)."""
    host = host_of(url)
    now = time.monotonic()
    with _lock:
        if not host or now - _browser_last.get(host, 0.0) < BROWSER_COOLDOWN:
            return None
        _browser_last[host] = now
    return _browser_fetch(url, host, timeout_s)


def _browser_fetch(url: str, host: str, timeout_s: int = 30):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                ctx = browser.new_context(
                    user_agent=user_agent().replace(" rssgate", "").strip(),
                    locale="en-CA", viewport={"width": 1366, "height": 900})
                seeded = _cookies_for(host)
                if seeded:
                    ctx.add_cookies([{"name": k, "value": v,
                                      "domain": host, "path": "/"}
                                     for k, v in seeded.items()])
                page = ctx.new_page()
                r = page.goto(url, wait_until="domcontentloaded",
                              timeout=timeout_s * 1000)
                page.wait_for_timeout(3000)   # let a CF redirect chain settle
                status = getattr(r, "status", 200) if r is not None else 200
                html = page.content()
                for ck in ctx.cookies():
                    _store_cookie(host, ck.get("name", ""), ck.get("value", ""))
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 - any browser failure: caller falls back
        return None
    return _Browsed(status, html, url)
