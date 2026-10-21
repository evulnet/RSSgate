"""Feed ordering: sort_order drives BOTH the admin table and the viewer
sidebar (they share db.list_feeds), and the reorder endpoint is the only
writer. New feeds append to the end; a migration backfills from id so an
existing library keeps the order its reader already knows."""
from rssgate import db


def _mk(client, url, title=""):
    r = client.post("/api/feeds", json={"url": url, "type": "feed",
                                        "categories": ["tech"], "title": title})
    assert r.status_code == 201
    return r.get_json()["id"]


def test_list_feeds_uses_sort_order(client):
    a = _mk(client, "https://a.com/feed")
    b = _mk(client, "https://b.com/feed")
    c = _mk(client, "https://c.com/feed")
    assert [f["id"] for f in db.list_feeds(client.conn)] == [a, b, c]  # id order kept
    db.reorder_feeds(client.conn, [c, a, b])
    assert [f["id"] for f in db.list_feeds(client.conn)] == [c, a, b]
    assert [f["id"] for f in client.get("/api/feeds").get_json()] == [c, a, b]


def test_new_feed_appends_to_the_end(client):
    a = _mk(client, "https://a.com/feed")
    b = _mk(client, "https://b.com/feed")
    db.reorder_feeds(client.conn, [b, a])
    c = _mk(client, "https://c.com/feed")
    assert [f["id"] for f in db.list_feeds(client.conn)] == [b, a, c]


def test_reorder_endpoint_roundtrip(client):
    a = _mk(client, "https://a.com/feed")
    b = _mk(client, "https://b.com/feed")
    c = _mk(client, "https://c.com/feed")
    r = client.post("/api/feeds/reorder", json={"ids": [b, c, a]})
    assert r.status_code == 200
    assert [f["id"] for f in client.get("/api/feeds").get_json()] == [b, c, a]
    # persists across a fresh connection (it is DB state, not request state)
    fresh = db.connect(db.path_of(client.conn))
    assert [f["id"] for f in db.list_feeds(fresh)] == [b, c, a]
    fresh.close()


def test_reorder_rejects_garbage(client):
    _mk(client, "https://a.com/feed")
    assert client.post("/api/feeds/reorder", json={}).status_code == 400
    assert client.post("/api/feeds/reorder", json={"ids": "x"}).status_code == 400
    assert client.post("/api/feeds/reorder", json={"ids": []}).status_code == 400
    assert client.post("/api/feeds/reorder", json={"ids": [1, 2, 99999]}
                       ).status_code == 400   # unknown id
    assert client.post("/api/feeds/reorder", json={"ids": [1, 1]}).status_code == 400
    assert client.post("/api/feeds/reorder", json={"ids": [None]}).status_code == 400
    # nothing was damaged: the list still serves every feed
    assert len(client.get("/api/feeds").get_json()) == 1


def test_zero_sort_order_falls_back_to_id_order(conn):
    # rows whose sort_order never moved off the default (0) must come out
    # in id order - the tiebreaker is what an un-reordered library runs on
    conn.execute("INSERT INTO feeds(id, url, type, added_at, sort_order)"
                 " VALUES (7, 'https://x7', 'feed', '', 0),"
                 "        (3, 'https://x3', 'feed', '', 0)")
    conn.commit()
    db._migrate(conn)
    assert [r["id"] for r in db.list_feeds(conn)] == [3, 7]
