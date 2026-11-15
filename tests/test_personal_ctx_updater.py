"""ctx API (submit_text, listen_now, hidden, surface, onboarding), personal intents, hotkey map, updater."""
import hashlib
import io
import json
import threading
from types import SimpleNamespace as NS
from unittest import mock

import pytest

import commands
import personal
import updater
import win_events
from main import Context, schedule_first_run_onboarding


def _cfg(**kw):
    base = dict(learn_profile=True, learn_corrections=True, learn_voice_clips=True, learn_habits=True,
                wake_aliases=["jarvis", "hey jarvis", "javis"], wake_word="jarvis",
                island_hidden=False, island_surface="gradient", onboarding_done=False)
    base.update(kw)
    return NS(**base)


@pytest.fixture
def pstore(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROFILE_DIR", str(tmp_path / "p"))
    personal._reset_caches()
    personal.configure(_cfg())
    commands._PREV_UTTERANCE.update(text="", at=0.0)
    yield
    personal._reset_caches()
    personal.configure(_cfg(learn_profile=False, learn_corrections=False, learn_voice_clips=False, learn_habits=False))


@pytest.fixture
def ctx(monkeypatch):
    saves = []
    import config
    monkeypatch.setattr(config, "save_config", lambda cfg, path=None: saves.append(1) or True)
    overlay = mock.MagicMock()
    c = Context(NS(request=lambda *a: None), overlay, None, threading.Event(), voice=mock.MagicMock(), cfg=_cfg())
    c.saves = saves
    return c


# ---- ctx API ----
def test_hidden_persists_and_posts(ctx):
    assert ctx.is_hidden() is False
    ctx.set_hidden(True)
    assert ctx.is_hidden() and ctx.cfg.island_hidden is True and ctx.saves
    ctx.overlay.post.assert_called_with("hidden", on=True)


def test_surface_validated(ctx):
    assert ctx.set_island_surface("black") is True
    ctx.overlay.post.assert_called_with("island_surface", style="black")
    assert ctx.cfg.island_surface == "black"
    ctx.overlay.post.reset_mock()
    assert ctx.set_island_surface("neon") is False
    ctx.overlay.post.assert_not_called()
    assert ctx.cfg.island_surface == "black"


def test_ask_page_and_onboarding_events(ctx):
    ctx.open_ask_page()
    ctx.overlay.post.assert_called_with("ask_page", focus=True)
    ctx.start_onboarding()
    ctx.overlay.post.assert_called_with("onboarding")
    assert ctx.personal is personal


def test_listen_now_uses_voice(ctx):
    ctx.voice.request_listen.return_value = True
    assert ctx.listen_now() is True
    ctx.voice.request_listen.assert_called_once()
    ctx.voice = None
    assert ctx.listen_now() is False


def test_submit_text_runs_pipeline_and_replies(ctx, monkeypatch):
    done = threading.Event()
    seen = []

    def fake_run(text, c):
        seen.append(text)
        return "Done, sir."

    monkeypatch.setattr(commands, "run_command", fake_run)
    ctx.overlay.post.side_effect = lambda kind, **kw: done.set() if (kind == "thinking" and kw.get("active") is False) else None
    assert ctx.submit_text("  open   notepad ") is True
    assert done.wait(3)
    assert seen == ["open notepad"]
    kinds = [c.args[0] for c in ctx.overlay.post.call_args_list]
    assert kinds == ["prompt", "thinking", "say", "thinking"]
    assert ctx.overlay.post.call_args_list[2].kwargs["text"] == "Done, sir."
    assert ctx.submit_text("   ") is False


def test_submit_text_reports_failures(ctx, monkeypatch):
    done = threading.Event()
    monkeypatch.setattr(commands, "run_command", mock.Mock(side_effect=RuntimeError("boom")))
    ctx.overlay.post.side_effect = lambda kind, **kw: done.set() if (kind == "thinking" and kw.get("active") is False) else None
    ctx.submit_text("explode")
    assert done.wait(3)
    assert any(c.args[0] == "error" for c in ctx.overlay.post.call_args_list)


def test_onboarding_scheduled_only_first_run(ctx):
    t = schedule_first_run_onboarding(ctx, delay=0.01)
    t.join(2)
    ctx.overlay.post.assert_called_with("onboarding")
    ctx.overlay.post.reset_mock()
    ctx.cfg.onboarding_done = True
    assert schedule_first_run_onboarding(ctx, delay=0.01) is None
    ctx.cfg.onboarding_done = False
    ctx.background = True
    assert schedule_first_run_onboarding(ctx, delay=0.01) is None
    ctx.overlay.post.assert_not_called()


# ---- hotkeys ----
def test_hotkey_mapping_and_dispatch():
    ids = {h[0]: h for h in win_events.HOTKEYS}
    assert ids[win_events.HOTKEY_HIDE][1:3] == (0x48, "Ctrl+Alt+H")
    assert ids[win_events.HOTKEY_ASK][1:3] == (0x20, "Ctrl+Alt+Space")
    assert len(ids) == 4 and ids[win_events.HOTKEY_TOGGLE][1] == 0x4A and ids[win_events.HOTKEY_QUIT][1] == 0x51
    calls = []
    ev = win_events.WinEvents(on_toggle=lambda: calls.append("t"), on_quit=lambda: calls.append("q"),
                              on_hide=lambda: calls.append("h"), on_ask=lambda: calls.append("a"))
    for hid in (1, 2, 3, 4, 99):
        assert ev.handle_message(win_events.WM_HOTKEY, hid, 0) == 0
    assert calls == ["t", "q", "h", "a"]


# ---- personal intents ----
def test_hide_show_voice_commands():
    c = NS(set_hidden=mock.Mock(), overlay=mock.MagicMock())
    for phrase in ("hide yourself", "Jarvis, hide jarvis"):
        assert commands.run_command(phrase, c)
        c.set_hidden.assert_called_with(True)
    for phrase in ("show yourself", "unhide"):
        assert commands.run_command(phrase, c)
        c.set_hidden.assert_called_with(False)


def test_call_me_and_remember(pstore):
    c = NS(overlay=mock.MagicMock())
    assert "Talib" in commands.run_command("call me talib", c)
    assert personal.get_profile()["name"] == "Talib"
    assert "ma'am" in commands.run_command("call me ma'am", c)
    assert personal.get_profile()["address_as"] == "ma'am"
    commands.run_command("my name is Sara Khan", c)
    assert personal.get_profile()["name"] == "Sara Khan"
    commands.run_command("remember that I play Elden Ring", c)
    commands.run_command("remember that I use Blender", c)
    commands.run_command("remember that I like coding", c)
    commands.run_command("remember my browser is Edge", c)
    p = personal.get_profile()
    assert p["games"] == ["Elden Ring"] and p["favorite_apps"] == ["Blender"] and p["uses"] == ["coding"]
    assert p["browser"] == "edge"


def test_profile_statements_report_when_learning_off(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROFILE_DIR", str(tmp_path / "p"))
    personal._reset_caches()
    personal.configure(_cfg(learn_profile=False))
    reply = commands.run_command("call me Talib", NS(overlay=mock.MagicMock()))
    assert "switched off" in reply and not (tmp_path / "p").exists()
    personal.configure(_cfg(learn_profile=False, learn_corrections=False, learn_voice_clips=False, learn_habits=False))


@pytest.mark.parametrize("prev,meant,pairs", [
    ("open elder ring", "elden ring", [("elder", "elden")]),
    ("open elder ring", "open elden ring", [("elder", "elden")]),
    ("play drake on spotify", "play drake on spotify", []),
    ("open chrome", "completely different sentence here", []),
    ("launch blender", "launch blender", []),
])
def test_correction_pairs(prev, meant, pairs):
    assert commands.correction_pairs(prev, meant) == pairs


@pytest.mark.parametrize("phrase", ["no, I said elden ring", "I meant elden ring", "that's wrong, I said elden ring",
                                    "Nope I said Elden Ring"])
def test_correction_intent_learns_and_reruns(pstore, monkeypatch, phrase):
    ran = []
    monkeypatch.setattr(commands, "_run_single_command", lambda t, c: ran.append(t) or "Opening.")
    import chained_commands
    monkeypatch.setattr(chained_commands, "split_chained_commands", lambda t: [t])
    c = NS(overlay=mock.MagicMock())
    commands._remember_utterance("open elder ring")
    reply = commands.run_command(phrase, c)
    assert reply == "Opening."
    assert ran[-1].lower() == "open elden ring"
    assert personal.list_corrections()[0]["heard"] == "elder" and personal.list_corrections()[0]["meant"] == "elden"


def test_correction_without_history_and_bare_its(pstore):
    c = NS(overlay=mock.MagicMock())
    assert "anything to correct" in commands.run_command("no, I said elden ring", c)
    assert commands.handle_personal_intent("it's a nice day", c) is None


def test_corrections_applied_to_next_commands(pstore, monkeypatch):
    seen = []
    monkeypatch.setattr(commands, "_run_single_command", lambda t, c: seen.append(t) or "ok")
    import chained_commands
    monkeypatch.setattr(chained_commands, "split_chained_commands", lambda t: [t])
    personal.add_correction("elder ring", "Elden Ring")
    commands.run_command("open elder ring", NS(overlay=mock.MagicMock()))
    assert seen == ["open Elden Ring"]


def test_search_uses_profile_engine_and_browser(pstore, monkeypatch):
    assert "google.com/search?q=cats+dogs" in commands.web_search_url("cats  dogs")
    personal.update_profile(search_engine="duckduckgo", browser="firefox")
    assert commands.web_search_url("cats") == "https://duckduckgo.com/?q=cats"
    popen = mock.Mock()
    monkeypatch.setattr(commands.subprocess, "Popen", popen)
    commands.open_web_url("https://x.test/?q=1")
    assert popen.call_args.args[0] == ["cmd.exe", "/c", "start", "", "firefox", "https://x.test/?q=1"]
    personal.update_profile(browser="")
    wb = mock.Mock()
    monkeypatch.setattr(commands.webbrowser, "open", wb)
    commands.open_web_url("https://x.test")
    wb.assert_called_once_with("https://x.test")


# ---- ai: profile prompt and answer cache ----
def test_system_prompt_uses_profile_and_cache_is_profile_scoped(pstore):
    import ai
    assert "as 'sir'" in ai.build_system_prompt()
    personal.update_profile(name="Talib", address_as="name", games=["Elden Ring"])
    prompt = ai.build_system_prompt()
    assert "as 'Talib'" in prompt and "Elden Ring" in prompt
    ai.clear_answer_cache()
    personal.update_profile(address_as="sir", name="")
    assert ai.answer_cache_put("what is the capital of france", "Paris, sir.")
    assert ai.answer_cache_get("what is the capital of france") == "Paris, sir."
    personal.update_profile(name="Talib", address_as="name")
    assert ai.answer_cache_get("what is the capital of france") is None
    ai.clear_answer_cache()


# ---- updater ----
ASSETS = [
    {"name": "JARVIS.exe", "browser_download_url": "u/exe", "size": 1},
    {"name": "JARVIS-Setup-1.4.0.exe.sha256", "browser_download_url": "u/sha", "size": 90},
    {"name": "JARVIS-Setup-1.4.0.exe", "browser_download_url": "u/setup", "size": 1234},
]


def test_asset_selection():
    got = updater.select_assets(ASSETS)
    assert got == {"name": "JARVIS-Setup-1.4.0.exe", "url": "u/setup", "size": 1234, "sha256_url": "u/sha"}
    assert updater.select_assets([ASSETS[0]])["url"] is None
    assert updater.select_assets(ASSETS[2:])["sha256_url"] is None


def test_installer_command_line():
    assert updater.installer_command(r"C:\t\JARVIS-Setup-1.4.0.exe") == [
        r"C:\t\JARVIS-Setup-1.4.0.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]


def test_check_picks_installer_asset():
    payload = {"tag_name": "v99.0.0", "assets": [dict(a, name=a["name"].replace("1.4.0", "99.0.0")) for a in ASSETS]}
    u = updater.AutoUpdater(overlay=mock.MagicMock(), auto_check=False)
    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
        u._check_worker()
    assert u.latest_info["asset_name"] == "JARVIS-Setup-99.0.0.exe" and u.latest_info["sha256_url"] == "u/sha"


class _Resp(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _downloader(monkeypatch, tmp_path, body, sha=None, size=None):
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    u = updater.AutoUpdater(overlay=mock.MagicMock(), auto_check=False)
    u.latest_info = {"version": "v9.0.0", "asset_url": "https://x.test/setup", "asset_name": "JARVIS-Setup-9.0.0.exe",
                     "asset_size": len(body) if size is None else size, "sha256_url": "https://x.test/sha" if sha else None}

    def fake_open(req, timeout=0):
        if req.full_url == "https://x.test/sha":
            return _Resp(sha.encode())
        r = _Resp(body)
        r.headers = {"Content-Length": str(len(body))}
        return r

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_open)
    return u


def test_download_verifies_sha256(monkeypatch, tmp_path):
    body = b"installer-bytes" * 100
    u = _downloader(monkeypatch, tmp_path, body, sha=hashlib.sha256(body).hexdigest() + "  JARVIS-Setup-9.0.0.exe")
    u._download_worker(None, None)
    assert u.status == "downloaded" and open(u.downloaded_path, "rb").read() == body


def test_download_rejects_bad_checksum_and_size(monkeypatch, tmp_path):
    body = b"installer-bytes" * 100
    u = _downloader(monkeypatch, tmp_path, body, sha="0" * 64)
    u._download_worker(None, None)
    assert u.status == "error" and u.downloaded_path is None
    assert not list((tmp_path / "jarvis_updater").glob("*.exe*"))
    u2 = _downloader(monkeypatch, tmp_path, body, size=len(body) + 5)
    u2._download_worker(None, None)
    assert u2.status == "error" and u2.downloaded_path is None


def test_download_without_checksum_asset_is_size_checked(monkeypatch, tmp_path):
    body = b"x" * 5000
    u = _downloader(monkeypatch, tmp_path, body)
    u._download_worker(None, None)
    assert u.status == "downloaded"


def test_apply_launches_installer_and_quits(monkeypatch, tmp_path):
    exe = tmp_path / "JARVIS-Setup-9.0.0.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    popen = mock.Mock()
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    ctx, overlay = mock.MagicMock(), mock.MagicMock()
    u = updater.AutoUpdater(ctx=ctx, overlay=overlay, auto_check=False)
    u.downloaded_path = str(exe)
    assert u.apply_update_and_restart() is True
    assert popen.call_args.args[0] == [str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    ctx.quit.assert_called_once()
    overlay.quit_app.assert_called_once()


def test_source_run_never_self_updates(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    popen = mock.Mock()
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    ctx = mock.MagicMock()
    u = updater.AutoUpdater(ctx=ctx, overlay=mock.MagicMock(), auto_check=False)
    u.downloaded_path = __file__
    u.latest_info = {"asset_url": "x"}
    assert u.apply_update_and_restart() is False
    u.download_async()
    assert u.status == "error"
    popen.assert_not_called()
    ctx.quit.assert_not_called()
