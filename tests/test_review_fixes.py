"""v0.93.3 review fixes: stored-XSS link scheme, config type gate (a bad
value used to save and then kill the scheduler tick silently), scheduler
_due robustness, _Browsed as a context manager."""
import time

from rssgate import config, fetcher, net
from rssgate.scheduler import Scheduler


def test_safe_link_allows_only_http_and_resolves_relative():
    base = "https://site.test/feed.xml"
    assert fetcher.safe_link(base, "https://a.test/x") == "https://a.test/x"
    assert fetcher.safe_link(base, "/post/1") == "https://site.test/post/1"
    for bad in ("javascript:alert(1)", " JavaScript:alert(1)",
                "data:text/html,<script>x</script>", "vbscript:x"):
        assert fetcher.safe_link(base, bad) == base, bad


def test_feed_parse_neutralises_javascript_links(monkeypatch):
    xml = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
           b'<item><title>evil</title><link>javascript:alert(1)</link>'
           b'<guid>g1</guid></item></channel></rss>')
    monkeypatch.setattr(fetcher, "_get", lambda *a, **k: {
        "ok": True, "not_modified": False, "status": 200, "content": xml,
        "content_type": "application/rss+xml", "etag": None,
        "last_modified": None, "challenge": False})
    res = fetcher.fetch_feed("https://site.test/rss")
    e = res["entries"][0]
    assert e["link"] == "https://site.test/rss" and e["guid"] == "g1"


def test_config_put_rejects_wrong_types_with_400(client):
    for body in ({"polling": {"feed_interval_minutes": "x"}},
                 {"polling": {"feed_interval_minutes": 0}},
                 {"summarizer": {"concurrency": "abc"}},
                 {"summarizer": {"system_prompt": 5}},
                 {"llm": None}, {"llm": "x"},
                 {"polling": {"fetch_on_start": "yes"}},
                 {"maintenance": {"retention_months": -1}}):
        r = client.put("/api/config", json=body)
        assert r.status_code == 400, (body, r.status_code)
        assert "error" in r.get_json()
    ok = client.put("/api/config", json={"polling": {"feed_interval_minutes": 15}})
    assert ok.status_code == 200
    assert ok.get_json()["polling"]["feed_interval_minutes"] == 15


def test_check_types_lets_unknown_and_freeform_keys_through():
    assert config.check_types({"llm": {"extra_body": {"anything": [1]}}}) is None
    assert config.check_types({"llm": {"api_key_set": True}}) is None
    assert config.check_types({"newsection": 5}) is None


def test_due_survives_bad_interval_and_uses_utc():
    s = Scheduler(None, {"polling": {"feed_interval_minutes": "x",
                                     "page_interval_minutes": 180}}, None)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    feed = {"type": "feed", "last_fetched_at": stamp}
    assert s._due(feed, time.time()) is True      # falls back to 30 min
    s.cfg["polling"]["feed_interval_minutes"] = 120
    assert s._due(feed, time.time()) is False     # 1 h ago < 2 h, in UTC


def test_browsed_is_a_context_manager():
    b = net._Browsed(200, "<html></html>", "https://h.test/i.jpg")
    with b as r:
        assert r is b
