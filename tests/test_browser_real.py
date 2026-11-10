"""Real headless Chromium against a local Cloudflare-shaped server (ui tier:
needs playwright + chromium). A challenge answers 403 and only THEN
redirects; reporting goto()'s 403 for the solved page made refresh pause the
feed as a site block (v0.93.3)."""
import http.server
import threading

import pytest

pytestmark = pytest.mark.ui


class _CF(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/ok"):
            body = ("<html><head><title>Real article</title></head><body>"
                    + "<p>word " * 600 + "</body></html>").encode()
            self.send_response(200)
        else:
            body = (b"<html><title>Just a moment...</title><script>"
                    b"setTimeout(()=>location.replace('/ok'),300)</script></html>")
            self.send_response(403)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_solved_challenge_reports_the_final_status(tmp_path):
    pytest.importorskip("playwright")
    from rssgate import browser
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CF)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = browser.fetch(f"http://127.0.0.1:{srv.server_port}/a", "127.0.0.1",
                          {"browser_profile_dir": str(tmp_path),
                           "llm_rescue": False})
    finally:
        srv.shutdown()
    assert r is not None, "chromium did not launch"
    assert "Real article" in r.text
    assert r.status_code == 200          # was 403 -> feed_block
