"""Offline layer contract: /sw.js is a template route (no-store) whose bytes
carry the CURRENT asset hash (a byte change is what makes iOS re-register),
it never intercepts writes, and the asset version is a content hash, not a
hand-bumped string (filmNotes scar: stale statics in installed PWAs)."""
import re

from rssgate import web


def _html(client):
    return client.get("/").get_data(as_text=True)


def test_sw_route_is_no_store_and_carries_current_hash(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert "javascript" in r.headers.get("Content-Type", "")
    m = re.search(r"viewer\.js\?v=([0-9a-f]{12})", _html(client))
    assert m, "viewer.js must be cache-busted with the 12-hex content hash"
    assert m.group(1) in r.get_data(as_text=True), \
        "sw.js must bake the CURRENT asset hash (cache name + precache URLs)"


def test_sw_never_intercepts_writes_or_foreign_origins(client):
    body = client.get("/sw.js").get_data(as_text=True)
    assert 'req.method !== "GET"' in body, "non-GET must pass through to the page"
    assert "self.location.origin" in body, "cross-origin must pass through"
    assert "/api/" in body and "/image/" in body, "API + image lanes exist"
    assert "skipWaiting" in body and "clients.claim" in body


def test_asset_version_is_a_content_hash(client):
    import pathlib

    import rssgate
    v1 = web._asset_version()
    assert re.fullmatch(r"[0-9a-f]{12}", v1)
    p = pathlib.Path(rssgate.__file__).parent / "static" / "viewer.js"
    orig = p.read_bytes()
    try:
        p.write_bytes(orig + b"\n// canary\n")
        assert web._asset_version() != v1, "a byte change must change the hash"
    finally:
        p.write_bytes(orig)


def test_viewer_registers_the_sw_and_shows_the_badge(client):
    js = (client.application.static_folder + "/viewer.js")
    src = open(js, encoding="utf-8").read()
    assert "/sw.js" in src and "serviceWorker" in src
    html = _html(client)
    assert 'id="offline-badge"' in html
    assert "offline-badge" in src, "the UI contract wants every id used"


def test_queue_schedules_its_own_replay(client):
    """The netOk-stuck incident: replay that lives only on online/boot/
    visibility events can sit for hours in a focused tab. A queued op must
    schedule its own retry, and a proven-good network (successful pulse
    tick) must flush the queue - guarded structurally; the behavior is
    browser-tested end to end (test_queued_mark_read_self_heals)."""
    import re
    src = open(client.application.static_folder + "/viewer.js",
               encoding="utf-8").read()
    m = re.search(r"function qAdd\(op\) \{.*?\n  \}", src, re.S)
    assert m and "qSchedule()" in m.group(0), "qAdd must schedule a retry"
    m = re.search(r"function qSchedule\(delay\) \{.*?\n  \}", src, re.S)
    assert m and "setTimeout" in m.group(0) and "qReplay()" in m.group(0)
    # the successful-tick flush (the tick body, after its failure ladder)
    assert "qSchedule(2000)" in src, "a good tick must flush the queue"


def test_cache_headers_right_sized_for_installed_pwas(client):
    """The stale-PWA scar, fixed at the source: HTML revalidates EVERY
    launch (so a deploy's fresh ?v= refs reach the home-screen app),
    content-hashed statics are immutable, /img gets a day."""
    assert "no-cache" in client.get("/").headers["Cache-Control"]
    assert "no-cache" in client.get("/admin").headers["Cache-Control"]
    st = client.get("/static/viewer.js?v=deadbeefcafe")
    assert "immutable" in st.headers["Cache-Control"]
    plain = client.get("/static/favicon.svg")
    assert "max-age=300" in plain.headers["Cache-Control"]
    assert "no-store" in client.get("/sw.js").headers["Cache-Control"]
