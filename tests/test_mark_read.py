"""Mark all as read: the top-bar button stamps the CURRENT scope (one feed
or everything). read_at + the forward-only cursor move together, so cards,
pills and /api/feeds agree immediately. hidden/queued posts have no card
and must not be counted."""
from rssgate import db


def _mk(client, url, n, prefix):
    r = client.post("/api/feeds", json={"url": url, "type": "feed",
                                        "categories": ["tech"]})
    fid = r.get_json()["id"]
    conn = client.conn
    for i in range(n):
        aid = db.upsert_article(conn, fid, f"{prefix}{i}", f"{url}/{i}",
                                f"T {prefix}{i}", f"2026-10-{i + 1:02d}T00:00:00Z")
        db.set_article(conn, aid, status="ready", summary="s")
    return fid


def test_mark_read_scoped_to_one_feed(client):
    a = _mk(client, "https://a.test/feed", 5, "a")
    b = _mk(client, "https://b.test/feed", 3, "b")
    r = client.post("/api/mark_read", json={"feed_id": a})
    assert r.status_code == 200
    assert r.get_json()["marked"] == 5
    feeds = {f["id"]: f["unread"] for f in client.get("/api/feeds").get_json()}
    assert feeds[a] == 0, "the chosen feed is read"
    assert feeds[b] == 3, "the other feed is NOT collateral damage"
    assert db.get_feed(client.conn, a)["last_read_ts"], "cursor advanced too"


def test_mark_read_all_feeds(client):
    a = _mk(client, "https://a.test/feed", 5, "a")
    b = _mk(client, "https://b.test/feed", 3, "b")
    r = client.post("/api/mark_read", json={"feed_id": 0})
    assert r.get_json()["marked"] == 8
    assert all(f["unread"] == 0 for f in client.get("/api/feeds").get_json())


def test_mark_read_skips_cardless_posts(client):
    """hidden and pending/processing render no card - they must not inflate
    the confirmed number (the pill-parity rule again)."""
    fid = _mk(client, "https://a.test/feed", 4, "a")
    conn = client.conn
    rows = conn.execute("SELECT id FROM articles WHERE feed_id=?"
                        " ORDER BY id LIMIT 2", (fid,)).fetchall()
    db.set_article(conn, rows[0]["id"], status="hidden")
    db.set_article(conn, rows[1]["id"], status="pending")
    r = client.post("/api/mark_read", json={"feed_id": fid})
    assert r.get_json()["marked"] == 2


def test_mark_read_input_hygiene(client):
    _mk(client, "https://a.test/feed", 2, "a")
    assert client.post("/api/mark_read", data="not json",
                       content_type="text/plain").status_code == 400
    assert client.post("/api/mark_read", json=[1, 2]).status_code == 400
    # junk feed_id coerces to the safe scope (all), never a 500
    assert client.post("/api/mark_read",
                       json={"feed_id": "abc"}).status_code == 200
