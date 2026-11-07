import pytest

from rssgate import db
from rssgate.config import load_config, save_config


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.sqlite"))
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def cfg(tmp_path):
    path = str(tmp_path / "config.yaml")
    save_config(load_config(path), path)
    return path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Flask test client with all outbound network stubbed."""
    import rssgate.web as web
    monkeypatch.setattr(web, "refresh_all", lambda conn, cfg, llm=None: [])
    monkeypatch.setattr(web, "refresh_feed", lambda conn, feed, cfg, llm=None: "stub")
    cfg_path = str(tmp_path / "config.yaml")
    save_config(load_config(cfg_path), cfg_path)
    conn = db.connect(str(tmp_path / "api.sqlite"))
    db.init_db(conn)
    # stub the LLM client used by /api/llm/test and /api/models
    import rssgate.llm as llm_mod
    monkeypatch.setattr(llm_mod.LLMClient, "chat",
                        lambda self, messages, max_tokens=1200, model="":
                        ("OK", {"prompt_tokens": 5, "completion_tokens": 1}))
    monkeypatch.setattr(llm_mod.LLMClient, "list_models", lambda self: ["alpha", "beta"])
    from rssgate.web import create_app
    app = create_app(cfg_path, conn=conn)
    app.config["TESTING"] = True
    from rssgate import imgstore
    # create_app re-inits imgstore at the config data_dir (./data = LIVE
    # dir); re-isolate AFTER it, whatever the fixture order was
    imgstore.init(str(tmp_path / "img"))
    with app.test_client() as c:
        c.conn = conn
        yield c


@pytest.fixture(autouse=True)
def _isolate_imgstore(tmp_path):
    """Tests must NEVER see the live cache dir. create_app() points imgstore
    at the config's data_dir (default ./data = the repo's LIVE images when
    pytest runs from the root), and maint.run_all/prune_orphans with a test
    DB then deletes every real file - it did (v0.91.0 dev, 2026-11-07:
    960 MB gone via a run_all test that forgot the per-test redirect).
    Per-test monkeypatching was the fragile pattern; this is the box.
    Tests wanting a specific dir still monkeypatch _dir afterwards."""
    from rssgate import imgstore
    imgstore.init(str(tmp_path / "img"))
    yield


@pytest.fixture(autouse=True)
def _no_host_pacing():
    """net pacing is process-global; a config PUT in one test must not make
    later tests sleep 3s per request."""
    from rssgate import net
    yield
    net._cfg.update({"ua": "", "interval": 0.0})
    net._next_ok.clear()
