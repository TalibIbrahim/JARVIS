import threading
import time
from types import SimpleNamespace

import lyrics_client as lc

LRC = "[ar:Someone]\n[00:01.50] Hello\n[00:03.00][01:02.5]Chorus\n\n[bad]\n"


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, timeout=None, headers=None):
        assert url.startswith("https://") and timeout == 5.0
        self.calls.append((url.rsplit("/", 1)[1], dict(params)))
        r = self.routes.pop(0)
        return r


def test_parse():
    assert lc.parse_lrc(LRC) == [(1.5, "Hello"), (3.0, "Chorus"), (62.5, "Chorus")]


def test_normalize():
    assert lc.normalize_title("Song (Remastered 2011)") == "Song"
    assert lc.normalize_title("Song - Slowed") == "Song"
    assert lc.normalize_title("Song (feat. X) [Live]") == "Song"
    assert lc.normalize_title("Plain Song") == "Plain Song"


def test_cache_hit_miss_and_negative(tmp_path, monkeypatch):
    monkeypatch.setattr("privacy_log.record_cloud_transmission", lambda **k: None)
    now = [1000.0]
    s = FakeSession([Resp(200, {"syncedLyrics": LRC, "plainLyrics": "p", "instrumental": False})])
    c = lc.LyricsClient(session=s, cache_dir=str(tmp_path), clock=lambda: now[0])
    l1 = c.get_lyrics("Song", "Art", "Alb", 200.2)
    assert l1.synced[0] == (1.5, "Hello") and len(s.calls) == 1
    assert c.get_lyrics("Song", "Art", "Alb", 200.2).plain == "p" and len(s.calls) == 1  # hit
    # not found: get 404 -> search with out-of-tolerance -> None; normalized retry; negative cached
    s2 = FakeSession([Resp(404), Resp(200, [{"duration": 250, "syncedLyrics": LRC}]),
                      Resp(404), Resp(200, [])])
    c2 = lc.LyricsClient(session=s2, cache_dir=str(tmp_path), clock=lambda: now[0])
    assert c2.get_lyrics("Nope (Remastered 2011)", "A", "", 100) is None
    assert [x[0] for x in s2.calls] == ["get", "search", "get", "search"]
    assert c2.get_lyrics("Nope (Remastered 2011)", "A", "", 100) is None and len(s2.calls) == 4
    now[0] += 8 * 86400   # negative TTL expired
    s2.routes = [Resp(404), Resp(200, [{"duration": 101, "syncedLyrics": LRC}])]
    assert c2.get_lyrics("Nope (Remastered 2011)", "A", "", 100).synced


def test_prune(tmp_path, monkeypatch):
    monkeypatch.setattr("privacy_log.record_cloud_transmission", lambda **k: None)
    c = lc.LyricsClient(session=FakeSession([]), cache_dir=str(tmp_path), max_files=3)
    for i in range(6):
        c._cache_write(f"k{i}", None)
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_http_error_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("privacy_log.record_cloud_transmission", lambda **k: None)
    c = lc.LyricsClient(session=FakeSession([Resp(500)]), cache_dir=str(tmp_path))
    try:
        c.get_lyrics("S", "A", "", 100)
        assert False
    except lc.LyricsError:
        pass


def test_feed_events(monkeypatch):
    import main
    posts = []
    ov = SimpleNamespace(post=lambda k, **kw: posts.append((k, kw)))
    ctx = SimpleNamespace(cfg=SimpleNamespace(island_lyrics_enabled=False))
    client = SimpleNamespace(get_lyrics=lambda t, a, al, d: lc.Lyrics([(1.0, "x")], "p", False))
    stop = threading.Event()
    feed = main.LyricsFeed(ctx, ov, stop, client=client)
    meta = SimpleNamespace(has_media=True, title="T", artist="A", album="", duration=100)
    feed.on_media(meta)
    assert posts[-1][1]["status"] == "off"
    ctx.cfg.island_lyrics_enabled = True
    feed.refresh()
    for _ in range(100):
        if posts[-1][1]["status"] == "ok":
            break
        time.sleep(0.02)
    st = [p[1]["status"] for p in posts]
    assert st == ["off", "loading", "ok"]
    assert posts[-1][1]["track_key"] == "T|A" and posts[-1][1]["synced"] == [[1.0, "x"]]
    stop.set()
