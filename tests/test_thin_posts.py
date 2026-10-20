"""Legitimately thin posts (the photo-blog class): a page with photos and a
handful of words is CONTENT, not a stub. When both text paths fall under the
digest floors, publish image-led (zero tokens) - the photos are the card AND
the junk guard: a cookie-banner stub has neither photos nor words. Also the
extractor fix underneath: sites that write post bodies as bare text + <br>
inside divs (Ameba) had their words invisible to the <p>-only collector."""
from rssgate import db, imgstore, refresh
from rssgate.config import load_config
from rssgate.extract import extract_article_text

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40


class FakeResp:
    def __init__(self, body=b"", text=None, ok=True, ctype="image/jpeg"):
        self.body, self._text, self.ok, self.ctype = body, text, ok, ctype
        self.status_code = 200 if ok else 500
        self.headers = {"content-type": ctype}

    @property
    def text(self):
        return self._text if self._text is not None else self.body.decode()

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_content(self, n): yield self.body


class LLM:
    provider, model = "fake", "fake"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **k):
        self.calls += 1
        return "A real digest of the full article.", {"prompt_tokens": 9,
                                                       "completion_tokens": 9}


# production-shaped (ameblo.jp entry-12981913684): body = text nodes + <br>
# + plain divs inside div#entryBody, NO <p> tags; photos as big <img>.
THIN_PHOTO_HTML = """<html><head><title>uta | blog</title></head><body>
<article class="js-entryWrapper"><div class="skinArticleBody">
<h2 class="skinArticleHeading">\u3046\u305f\u5cf6\u30fc\uff01\uff08\u5927\u4e45\u91ce\u5cf6\u306e\u3046\u3055\u304e\u3055\u3093\uff09</h2>
<div id="entryBody" class="articleText">\u3053\u3093\u306b\u3061\u306f
<img class="emoji" src="https://stat100.ameba.jp/emoji.png" height="24" width="24">
<div><br/></div>
<div>\u4eca\u65e5\u304b\u3089\u3088\u308d\u3057\u304f\u304a\u9858\u3044\u3044\u305f\u3057\u307e\u3059</div>
<div><img alt="\u3046\u3055\u304e" src="https://stat.ameba.jp/user_images/photo1.jpg"
     width="706" height="1024"></div>
</div></div></article></body></html>"""

THIN_NOIMG_HTML = THIN_PHOTO_HTML.replace(
    '<img alt="\u3046\u3055\u304e" src="https://stat.ameba.jp/user_images/photo1.jpg"\n'
    '     width="706" height="1024">', "")
FEED_TEXT = "\u3053\u3093\u306b\u3061\u306f \u3046\u3055\u304e\u5199\u771f\u5bb6uta"  # < EXCERPT_MIN


def _seed(conn, urlhost):
    fid = db.add_feed(conn, f"https://{urlhost}/feed", type_="feed")["id"]
    aid = db.upsert_article(conn, fid, "g1", f"https://{urlhost}/entry",
                            "\u3046\u305f\u5cf6", "2026-10-18T02:38:47Z",
                            feed_text=FEED_TEXT)
    return fid, aid


def _serve(monkeypatch, tmp_path, html):
    monkeypatch.setattr(imgstore, "_dir", tmp_path / "images")
    (tmp_path / "images").mkdir(exist_ok=True)
    hits = []

    def fake_get(url, **kw):
        hits.append(url)
        if url.endswith(".jpg"):
            return FakeResp(JPEG)
        return FakeResp(text=html)
    monkeypatch.setattr("requests.get", fake_get)
    return hits


def test_extractor_reads_div_and_br_text():
    """Ameba writes post bodies as bare text + <br> in divs; the collector
    used to see only the title (its block set is p/li/h*, none of which the
    body uses). The words must come out of the container."""
    text = extract_article_text(THIN_PHOTO_HTML)
    assert "\u3053\u3093\u306b\u3061\u306f" in text                    # first line
    assert "\u4eca\u65e5\u304b\u3089\u3088\u308d\u3057\u304f" in text  # div-line
    assert "\u3046\u305f\u5cf6\u30fc\uff01" in text                     # title survives


def test_thin_photo_page_publishes_image_led_with_zero_tokens(
        conn, cfg, tmp_path, monkeypatch):
    """Thin page + thin feed text + real photos -> published, not failed:
    the longest available text becomes the summary, the photos are cached
    and the LLM is never asked (token discipline: this post costs nothing)."""
    hits = _serve(monkeypatch, tmp_path, THIN_PHOTO_HTML)
    llm = LLM()
    _, aid = _seed(conn, "thin-photo.test")
    n = refresh.summarize_pending(conn, load_config(cfg), llm, limit=1)
    assert n == 1
    art = conn.execute("SELECT status, digest_source, summary, image"
                       " FROM articles WHERE id=?", (aid,)).fetchone()
    assert art["status"] == "ready" and art["digest_source"] == "image"
    assert "\u3046\u305f\u5cf6" in art["summary"]          # longest text won
    assert "##" not in art["summary"], art["summary"]   # no markdown markers
    # the card shows this text verbatim - the extractor's heading markers
    # ("## title") are an LLM cue, not something to print at the reader
    assert art["summary"].lstrip().startswith("\u3046\u305f\u5cf6")
    assert art["image"] and (tmp_path / "images" / art["image"]).read_bytes() \
        .startswith(b"\xff\xd8")
    assert llm.calls == 0                          # zero tokens spent
    assert any(u.endswith("/entry") for u in hits)  # the page WAS fetched
    # queued for a real digest in case the page grows words later
    assert conn.execute("SELECT upgrade_at FROM articles WHERE id=?",
                        (aid,)).fetchone()["upgrade_at"]


def test_avatar_filter_keeps_user_slash_and_carves_user_images():
    """The v0.84.0 user_images carve-out must not widen the avatar hole:
    user/avatar.png (a classic avatar path) stays rejected, Ameba's content
    CDN user_images/ stays accepted. The carve-out replaced [-_/] with [-_]
    once and silently let user/ avatars through - this test is the scar."""
    from rssgate.extract import AVATAR_RE
    assert AVATAR_RE.search("https://x.com/user/photo123.jpg")
    assert AVATAR_RE.search("profile/user-2/icon.jpg")
    assert not AVATAR_RE.search("https://stat.ameba.jp/user_images/cat.jpg")


def test_thin_stub_without_images_still_fails(conn, cfg, tmp_path, monkeypatch):
    """The junk guard: under the floors AND no photos = stub, not a photo
    post. It must fail exactly as before (no publishing cookie banners)."""
    _serve(monkeypatch, tmp_path, THIN_NOIMG_HTML)
    _, aid = _seed(conn, "thin-stub.test")
    refresh.summarize_pending(conn, load_config(cfg), LLM(), limit=1)
    art = conn.execute("SELECT status, error_msg FROM articles WHERE id=?",
                       (aid,)).fetchone()
    assert art["status"] == "error"
    assert "extracted text too short" in art["error_msg"]


def test_image_led_post_upgrades_in_place(conn, cfg, tmp_path, monkeypatch):
    """An image-led card is not terminal: when the page finally yields real
    text the digest replaces it IN PLACE (same row, digest_source cleared),
    exactly like the excerpt ladder."""
    _serve(monkeypatch, tmp_path, THIN_PHOTO_HTML)
    _, aid = _seed(conn, "thin-up.test")
    refresh.summarize_pending(conn, load_config(cfg), LLM(), limit=1)
    assert conn.execute("SELECT digest_source FROM articles WHERE id=?",
                        (aid,)).fetchone()["digest_source"] == "image"
    # due for its next page try; the page now has a real article
    real = "<html><body><article>" + "".join(
        f"<p>Paragraph {i}: the rabbits of the island thrive.</p>"
        for i in range(25)) + "</article></body></html>"

    def fake_get(url, **kw):
        if url.endswith(".jpg"):
            return FakeResp(JPEG)
        return FakeResp(text=real)
    monkeypatch.setattr("requests.get", fake_get)
    conn.execute("UPDATE articles SET upgrade_at='2000-01-01T00:00:00Z'"
                 " WHERE id=?", (aid,))
    conn.commit()
    assert refresh.upgrade_excerpts(conn, load_config(cfg), LLM(), limit=1) == 1
    art = conn.execute("SELECT digest_source, summary FROM articles"
                       " WHERE id=?", (aid,)).fetchone()
    assert art["digest_source"] == ""
    assert art["summary"] == "A real digest of the full article."
