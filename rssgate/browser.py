"""Headless-Chromium page fetch: the last rung when plain HTTP meets a JS
challenge or an interactive wall.

Rung 1 (deterministic): a real browser with a PERSISTENT per-host profile
(Cloudflare trusts returning visitors), the system Chrome channel when
present, and automation signals scrubbed. Rung 2 (rules): click known
consent/modal buttons - no model involved. Rung 3 (LLM rescue): when the
page is still stuck, the model may choose ONE action per step from a hard
allowlist (click a text that verifiably exists on the page, wait, give up).
Fetched pages are untrusted input: the gate is the allowlist and the
absence of any typing/submitting action, not the model's judgment.
"""
from __future__ import annotations

import json
import os
import re
import time
import logging

log = logging.getLogger("rssgate.browser")
_warned = False

STUCK_RE = re.compile(
    r"just a moment|verify you are human|attention required|"
    r"are you a (really )?robot|enable javascript|checking your browser",
    re.I)

# Rung 2: first visible button/link whose text matches - clicked at most
# WALL_CLICKS times per fetch, then we re-read the page.
WALL_RULES = [
    r"accept (all )?(cookies|cookies and continue)?", r"^i agree$", r"^agree$",
    r"got it", r"continue as guest", r"^no thanks$", r"^close$", r"dismiss",
]
WALL_CLICKS = 3

RESCUE_SYSTEM = """You help a local RSS reader render public web pages for a single \
reader. From the page snapshot, choose ONE next action toward seeing the article.
Reply with ONLY one JSON object, no prose:
{"action": "click", "text": "<exact button/link text from the snapshot>"}
{"action": "wait", "seconds": 3}
{"action": "give_up"}
Only click a text that appears verbatim in the snapshot. Never type or submit
anything. Prefer dismissing consent/cookie walls. If nothing plausible, give_up."""

RESCUE_STEPS = 4            # model turns per fetch
RESCUE_BUDGET_S = 45.0      # wall-clock ceiling for the rescue loop
WAIT_CAP_S = 10


def is_stuck(html: str) -> bool:
    return bool(STUCK_RE.search(html or "")) or len(html or "") < 1500


def _locator_click(page, pattern: str) -> bool:
    """Click the first visible button/link matching pattern; True if clicked."""
    rx = re.compile(pattern, re.I)
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role, name=rx).first
            if loc.is_visible():
                loc.click(timeout=2000)
                return True
        except Exception:  # noqa: BLE001 - missing/unclickable: next rule
            continue
    return False


def clear_walls(page) -> int:
    """Rung 2: dismiss consent/modal buttons by rules. Returns clicks made."""
    clicks = 0
    while clicks < WALL_CLICKS:
        for pattern in WALL_RULES:
            if _locator_click(page, pattern):
                clicks += 1
                page.wait_for_timeout(500)
                break
        else:
            break
    return clicks


def snapshot(page, body_chars: int = 1200) -> dict:
    """Compact, model-safe view: title, control texts, a text excerpt only."""
    try:
        data = page.evaluate(
            """(n) => {
                const texts = [];
                document.querySelectorAll(
                  'button, a[role=button], a[href^="#"], [role=button]')
                  .forEach(e => { const t = (e.innerText || '').trim();
                                   if (t && t.length < 60) texts.push(t); });
                return { title: document.title,
                         texts: [...new Set(texts)].slice(0, 40),
                         body: (document.body ? document.body.innerText : '')
                               .slice(0, n) };
            }""", body_chars)
        return data or {"title": "", "texts": [], "body": ""}
    except Exception:  # noqa: BLE001
        return {"title": "", "texts": [], "body": ""}


def _parse_action(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"action": "give_up"}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {"action": "give_up"}
    except (ValueError, TypeError):
        return {"action": "give_up"}


def rescue(page, llm, max_steps: int = RESCUE_STEPS,
           budget_s: float = RESCUE_BUDGET_S) -> bool:
    """Rung 3: LLM picks actions from the allowlist until un-stuck or spent.
    Returns True when the page stopped looking stuck."""
    t0 = time.monotonic()
    for _ in range(max(0, max_steps)):
        if time.monotonic() - t0 > budget_s:
            break
        if not is_stuck(page.content()):
            return True             # the snapshot is capped < the size floor
        snap = snapshot(page)
        asked = json.dumps({"title": snap["title"], "controls": snap["texts"],
                            "excerpt": snap["body"]})
        try:
            reply, _usage = llm.chat(
                [{"role": "system", "content": RESCUE_SYSTEM},
                 {"role": "user", "content": asked}], max_tokens=150)
        except Exception:  # noqa: BLE001 - LLM down: deterministic outcome
            return not is_stuck(page.content())
        act = _parse_action(reply)
        kind = str(act.get("action", "")).lower()
        if kind == "click":
            want = str(act.get("text", "")).strip()
            # Allowlist: the text must verifiably exist on the page.
            if not want or want not in snap["texts"]:
                return False
            if not _locator_click(page, re.escape(want)):
                return False
            page.wait_for_timeout(1000)
            if not is_stuck(page.content()):
                return True
        elif kind == "wait":
            try:
                secs = min(max(float(act.get("seconds", 3)), 0.0), WAIT_CAP_S)
            except (TypeError, ValueError):
                secs = 1.0
            page.wait_for_timeout(int(secs * 1000))
            if not is_stuck(page.content()):
                return True
        else:                                   # give_up or anything unknown
            return False
    return not is_stuck(page.content())


def _clean_ua(ua: str) -> str:
    return (ua or "").replace(" rssgate", "").strip()


def fetch(url: str, host: str, fcfg: dict, llm_factory=None,
          timeout_s: int = 30):
    """Open url in a persistent headless browser; returns net._Browsed or None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        global _warned
        if not _warned:          # default-on feature, optional dependency:
            _warned = True       # say once why the rung never runs
            log.warning("fetch.browser_fallback is on but playwright is not "
                        "installed: pip install playwright && python -m "
                        "playwright install chromium")
        return None
    from . import net
    profile = os.path.join(str(fcfg.get("browser_profile_dir")
                               or "data/browser-profiles"), host or "default")
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        return None
    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    ua = _clean_ua(net.user_agent())
    try:
        with sync_playwright() as p:
            ctx = None
            for channel in ("chrome", None):     # real Chrome if installed
                try:
                    ctx = p.chromium.launch_persistent_context(
                        profile, headless=True, channel=channel, args=args,
                        locale="en-CA", user_agent=ua,
                        viewport={"width": 1366, "height": 900})
                    break
                except Exception:  # noqa: BLE001 - channel missing: try next
                    continue
            if ctx is None:
                return None
            try:
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',"
                    "{get:()=>undefined});")
                seeded = net._cookies_for(host)
                if seeded:
                    ctx.add_cookies([{"name": k, "value": v,
                                      "domain": host, "path": "/"}
                                     for k, v in seeded.items()])
                page = ctx.new_page()
                # The FINAL main-frame navigation decides the status: a
                # Cloudflare challenge answers 403 and only then redirects to
                # the real page, so goto()'s response (the 403) reported a
                # solved page as a site block and paused the feed.
                navs = []
                page.on("response", lambda resp: navs.append(resp.status)
                        if resp.request.is_navigation_request()
                        and resp.frame == page.main_frame else None)
                r = page.goto(url, wait_until="domcontentloaded",
                              timeout=timeout_s * 1000)
                page.wait_for_timeout(3000)      # let CF redirect chains settle
                clear_walls(page)
                if is_stuck(page.content()):
                    factory = llm_factory
                    llm = None
                    if fcfg.get("llm_rescue", True) and factory:
                        try:
                            llm = factory()
                        except Exception:  # noqa: BLE001
                            llm = None
                    if llm is not None:
                        steps = fcfg.get("rescue_max_steps", RESCUE_STEPS)
                        rescue(page, llm, max_steps=int(steps))
                html = page.content()
                status = navs[-1] if navs else (
                    getattr(r, "status", 200) if r is not None else 200)
                if status in (403, 503) and not is_stuck(html):
                    status = 200      # challenge solved in place (no new nav)
                # this host's cookies only: ctx.cookies() is every domain the
                # page touched, and ad cookies evicted cf_clearance (25 cap)
                for ck in ctx.cookies(url):
                    net._store_cookie(host, ck.get("name", ""), ck.get("value", ""))
            finally:
                ctx.close()
    except Exception:  # noqa: BLE001 - any browser failure: caller falls back
        return None
    return net._Browsed(status, html, url)
