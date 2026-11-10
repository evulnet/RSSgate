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


def test_scheduler_thread_opens_its_own_connection(tmp_path, monkeypatch):
    """Invariant 6: serve.py passes the request connection; the scheduler
    thread must not keep using it."""
    import threading
    from rssgate import db
    path = str(tmp_path / "s.sqlite")
    shared = db.connect(path)
    db.init_db(shared)
    s = Scheduler(shared, {}, lambda: None, tick_seconds=3600, db_path=path)
    seen = threading.Event()
    used = []
    monkeypatch.setattr(s, "poll_due", lambda: used.append(s.conn) or seen.set())
    s.start()
    assert seen.wait(5)
    s.stop()
    assert used[0] is not shared


def test_garbage_cursor_cannot_freeze_a_feed(client):
    from rssgate import db
    fid = db.add_feed(client.conn, "https://cur.test/f", type_="feed")["id"]
    r = client.post("/api/position", json={"ts": "2026-01-01T00:00:00Z",
                                           "id": 1, "reads": {str(fid): "zzz"}})
    assert r.status_code < 500
    assert db.get_feed(client.conn, fid)["last_read_ts"] is None
    client.post("/api/position", json={"ts": "zzz", "id": 1, "global": True})
    assert db.get_state(client.conn, "resume_ts", "") != "zzz"
    db.mark_feed_read(client.conn, fid, "2026-01-01T00:00:00+00:00")
    assert db.get_feed(client.conn, fid)["last_read_ts"] == "2026-01-01T00:00:00Z"


def test_superscript_digit_read_id_is_not_a_500(client):
    r = client.post("/api/position", json={"read_ids": ["²", "3"]})
    assert r.status_code == 200


def test_bounded_mark_all_leaves_later_arrivals_unread(client):
    """A queued ✓ replays with max_id: posts that arrived after the tap
    stay unread."""
    from rssgate import db
    c = client.conn
    fid = db.add_feed(c, "https://mr.test/f", type_="feed")["id"]
    a1 = db.upsert_article(c, fid, "m1", "https://mr.test/1", "t",
                           "2026-11-01T00:00:00Z")
    a2 = db.upsert_article(c, fid, "m2", "https://mr.test/2", "t",
                           "2026-11-02T00:00:00Z")
    for a in (a1, a2):
        db.set_article(c, a, status="ready", summary="s")
    r = client.post("/api/mark_read", json={"feed_id": fid, "max_id": a1})
    assert r.get_json()["marked"] == 1
    rows = {x["id"]: x["read_at"] for x in
            c.execute("SELECT id, read_at FROM articles WHERE feed_id=?", (fid,))}
    assert rows[a1] and not rows[a2]
    assert db.get_feed(c, fid)["last_read_ts"] == "2026-11-01T00:00:00Z"
    # unbounded (the online tap) still marks everything
    assert client.post("/api/mark_read", json={"feed_id": fid}).get_json()["marked"] == 1


def test_models_endpoint_degrades_when_backend_is_down(tmp_path, monkeypatch):
    """A down LLM must not break the admin panel: JSON 502, never a 500."""
    import requests
    from rssgate import llm as L

    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", boom)
    c = L.LLMClient({"llm": {"provider": "local", "base_url": "http://127.0.0.1:9/v1"}})
    try:
        c.list_models()
        assert False, "should raise"
    except L.LLMError:
        pass
    from rssgate.refresh import llm_unavailable
    try:
        c.list_models()
    except L.LLMError as e:
        assert llm_unavailable(e), "a connection failure must HOLD the queue"


def test_concurrent_requests_do_not_share_a_connection(client):
    """Werkzeug serves each request on its own thread; one shared sqlite
    handle interleaved cursors and COUNT(*) came back rowless (/api/feeds
    500 - the tier's pillagree 'flake'). Hammer it from threads."""
    import threading
    from rssgate import db
    for i in range(5):
        fid = db.add_feed(client.conn, f"https://cc{i}.test/f", type_="feed")["id"]
        for j in range(5):
            aid = db.upsert_article(client.conn, fid, f"g{j}", f"https://cc{i}.test/{j}",
                                    "t", f"2026-11-0{j + 1}T00:00:00Z")
            db.set_article(client.conn, aid, status="ready", summary="s")
    bad = []

    def worker():
        for _ in range(25):
            for u in ("/api/feeds", "/api/status", "/api/pulse"):
                try:
                    r = client.application.test_client().get(u)
                    if r.status_code != 200:
                        bad.append((u, r.status_code))
                except Exception as e:  # noqa: BLE001
                    bad.append((u, repr(e)[:80]))
    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert bad == [], bad[:5]
