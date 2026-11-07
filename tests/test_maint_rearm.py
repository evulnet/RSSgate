"""Re-arm pass: posts whose upgrade ladder expired WHILE a site was
bot-checking its pages get one more page attempt. A teaser-length summary
on an excerpt post means the feed had nothing and the block never lifted
in time; the block may have lifted now (gizmodo 2026-11: feed is
title+teaser only, pages intermittently bot-checked, 23 posts stuck as
teasers)."""
from rssgate import db, maint


def _mk(client, url, guid, ts, summary, digest_source):
    r = client.post("/api/feeds", json={"url": url, "type": "feed",
                                        "categories": ["tech"]})
    fid = r.get_json()["id"]
    conn = client.conn
    aid = db.upsert_article(conn, fid, guid, f"{url}/{guid}", "T", ts)
    db.set_article(conn, aid, status="ready", summary=summary,
                   digest_source=digest_source, upgrade_at=None)
    return aid


def test_rearm_gives_blocked_teasers_one_more_attempt(client):
    aid = _mk(client, "https://g.test/feed", "g1",
              "2026-11-06T00:00:00Z", "A bare teaser line.", "excerpt")
    assert maint.rearm_blocked_excerpts(client.conn) == 1
    row = client.conn.execute("SELECT upgrade_at, upgrade_tries FROM articles"
                              " WHERE id=?", (aid,)).fetchone()
    assert row["upgrade_at"], "back on the ladder"
    assert row["upgrade_tries"] == 0, "a FULL fresh ladder (block may be over)"
    # idempotent: the touched row left the selection
    assert maint.rearm_blocked_excerpts(client.conn) == 0


def test_rearm_leaves_everything_else_alone(client):
    c = client.conn
    real = _mk(client, "https://r.test/feed",
               "r1", "2026-11-06T00:00:00Z", "x" * 400, "excerpt")   # real digest
    old = _mk(client, "https://o.test/feed", "o1", "2026-01-01T00:00:00Z",
              "Ancient teaser.", "excerpt")                            # outside window
    db.set_article(client.conn, old, fetched_at="2026-01-01T00:00:00Z")
    page = _mk(client, "https://p.test/feed", "p1", "2026-11-06T00:00:00Z",
               "Short but from a real page.", "")                       # not an excerpt
    assert maint.rearm_blocked_excerpts(c) == 0
    for aid in (real, old, page):
        row = c.execute("SELECT upgrade_at FROM articles WHERE id=?",
                        (aid,)).fetchone()
        assert row["upgrade_at"] is None, "untouched"


def test_run_all_reports_the_pass(client):
    _mk(client, "https://g.test/feed", "g1", "2026-11-06T00:00:00Z",
        "Teaser.", "excerpt")
    report = maint.run_all(client.conn, {})
    assert report["excerpts_rearmed"] == 1
    # second run: nothing left to do (the idempotency assertion)
    assert maint.run_all(client.conn, {})["excerpts_rearmed"] == 0
