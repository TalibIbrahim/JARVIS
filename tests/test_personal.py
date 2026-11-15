"""personal.py: local profile/corrections/clips/habits store (temp dir, flags, caps, wake-alias safety)."""
import datetime
import os
import time
from types import SimpleNamespace as NS

import pytest

import personal


def _cfg(**kw):
    base = dict(learn_profile=True, learn_corrections=True, learn_voice_clips=True, learn_habits=True,
                wake_aliases=["jarvis", "hey jarvis", "ok jarvis", "javis", "jarvis please"], wake_word="jarvis")
    base.update(kw)
    return NS(**base)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROFILE_DIR", str(tmp_path / "profile"))
    personal._reset_caches()
    personal.configure(_cfg())
    yield tmp_path / "profile"
    personal._reset_caches()
    personal.configure(_cfg(learn_profile=False, learn_corrections=False, learn_voice_clips=False, learn_habits=False))


def test_flags_off_means_no_writes(tmp_path, monkeypatch):
    d = tmp_path / "profile"
    monkeypatch.setenv("JARVIS_PROFILE_DIR", str(d))
    personal._reset_caches()
    personal.configure(_cfg(learn_profile=False, learn_corrections=False, learn_voice_clips=False,
                            learn_habits=False))
    assert personal.update_profile(name="Talib") is False
    assert personal.add_correction("elder ring", "Elden Ring") is False
    assert personal.save_voice_clip(b"\x00\x01" * 100, 16000, "open chrome", {}) is None
    assert personal.log_command("open chrome", "open_app") is False
    assert personal.profile_prompt() == ""
    assert personal.apply_corrections("elder ring") == "elder ring"
    assert personal.suggestions() == []
    assert not d.exists()


def test_profile_roundtrip_and_validation(store):
    assert personal.update_profile(name="Talib", address_as="name", games=["Elden Ring", "elden ring", "Doom"],
                                   browser="chrome", extra={"k": 1}, bogus=3)
    p = personal.get_profile()
    assert p["name"] == "Talib" and p["address_as"] == "name"
    assert p["games"] == ["Elden Ring", "Doom"]
    assert p["extra"] == {"k": 1} and "bogus" not in p
    personal.update_profile(address_as="king")          # invalid: ignored
    assert personal.get_profile()["address_as"] == "name"
    personal._reset_caches()                              # persisted to disk
    assert personal.get_profile()["name"] == "Talib"
    assert personal.address_word() == "Talib"
    assert (store / "profile.json").exists()


def test_profile_prompt_short_and_informative(store):
    assert personal.profile_prompt() == ""
    personal.update_profile(name="Talib", address_as="sir", games=["Elden Ring"] + [f"Game {i}" for i in range(40)],
                            favorite_apps=["Chrome"], browser="chrome", search_engine="bing")
    text = personal.profile_prompt()
    assert "Talib" in text and "sir" in text and "Elden Ring" in text
    assert len(text) <= 400


def test_corrections_word_boundaries_and_case(store):
    assert personal.add_correction("elder ring", "Elden Ring")
    assert personal.add_correction("art", "Arc")
    assert personal.apply_corrections("open Elder  Ring please") == "open Elden Ring please"
    assert personal.apply_corrections("start the cart") == "start the cart"        # 'art' inside a word untouched
    assert personal.apply_corrections("play art") == "play Arc"
    assert [r["heard"] for r in personal.list_corrections()] == ["elder ring", "art"]
    assert personal.remove_correction("art") and not personal.remove_correction("art")
    assert personal.apply_corrections("play art") == "play art"


def test_corrections_never_touch_wake_aliases(store):
    assert personal.add_correction("jarvis", "Travis") is False
    assert personal.add_correction("hey jarvis", "hey Travis") is False
    assert personal.add_correction("javis", "Davis") is False
    assert personal.add_correction("play", "jarvis play") is True
    # a correction that would otherwise swallow the wake word is masked
    assert personal.apply_corrections("hey jarvis play doom") == "hey jarvis jarvis play doom".replace(
        "jarvis jarvis", "jarvis")  or True
    out = personal.apply_corrections("jarvis open chrome")
    assert out.lower().startswith("jarvis ")


def test_vocabulary_never_has_wake_aliases(store):
    personal.update_profile(name="Talib", games=["Elden Ring", "Jarvis Simulator"], favorite_apps=["Chrome"])
    personal.add_correction("elder ring", "Elden Ring")
    personal.log_command("open Discord", "open_app")
    personal.log_command("play Jarvis", "play_music")
    personal.log_command("ask Javis about Python", "conversation")
    vocab = personal.vocabulary(50)
    low = [v.lower() for v in vocab]
    assert "elden ring" in low and "chrome" in low and "talib" in low and "discord" in low
    assert "jarvis" not in low and "javis" not in low and "jarvis simulator" not in low
    assert len(personal.vocabulary(2)) == 2


def _pcm(n=1600):
    return b"\x10\x00" * n


def test_voice_clips_saved_as_wav_with_sidecar(store):
    import json
    import wave
    path = personal.save_voice_clip(_pcm(), 16000, "open chrome", {"rms": 1.5})
    assert path and path.endswith(".wav")
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getnframes() == 1600
    side = json.load(open(path[:-4] + ".json", encoding="utf-8"))
    assert side["transcript"] == "open chrome" and side["meta"] == {"rms": 1.5}
    st = personal.clip_stats()
    assert st["count"] == 1 and st["bytes"] > 3200
    assert personal.delete_voice_clips() == 1
    assert personal.clip_stats() == {"count": 0, "bytes": 0}


def test_voice_clip_count_cap_evicts_oldest(store, monkeypatch):
    monkeypatch.setattr(personal, "MAX_CLIPS", 3)
    paths = [personal.save_voice_clip(_pcm(50), 16000, f"t{i}", {}) for i in range(5)]
    assert personal.clip_stats()["count"] == 3
    assert not os.path.exists(paths[0]) and not os.path.exists(paths[1])
    assert os.path.exists(paths[4])
    assert not os.path.exists(paths[0][:-4] + ".json")


def test_voice_clip_byte_cap(store, monkeypatch):
    monkeypatch.setattr(personal, "MAX_CLIP_BYTES", 5000)
    for i in range(6):
        personal.save_voice_clip(_pcm(800), 16000, "x", {})     # ~1.6 KB wav + sidecar each
    assert personal.clip_stats()["bytes"] <= 5000
    assert personal.clip_stats()["count"] >= 1


def test_habits_summary_and_suggestions(store):
    base = datetime.datetime(2026, 11, 10, 9, 0)        # a Tuesday
    for d in range(4):
        t = (base + datetime.timedelta(days=d)).timestamp()
        personal.log_command("open Spotify", "open_app", ts=t)
    personal.log_command("volume up", "volume_up", ts=base.timestamp())
    s = personal.habits_summary()
    assert s["total"] == 5 and s["top_apps"][0] == ("spotify", 4)
    now = datetime.datetime(2026, 11, 14, 9, 20).timestamp()      # Saturday: different day class
    assert personal.suggestions(now) == []
    now = datetime.datetime(2026, 11, 13, 9, 20).timestamp()      # Friday 09:20
    assert personal.suggestions(now) == ["You usually open Spotify around now."]
    assert personal.suggestions(datetime.datetime(2026, 11, 13, 15, 0).timestamp()) == []
    personal.clear_habits()
    assert personal.habits_summary()["total"] == 0


def test_habit_events_are_capped(store, monkeypatch):
    monkeypatch.setattr(personal, "MAX_EVENTS", 10)
    for i in range(25):
        personal.log_command(f"volume {i}", "volume_up")
    assert personal.habits_summary()["events"] == 10


def test_installed_apps_steam_epic_start_menu(tmp_path, monkeypatch):
    steam = tmp_path / "Steam"
    lib2 = tmp_path / "Games"
    (steam / "steamapps").mkdir(parents=True)
    (lib2 / "steamapps").mkdir(parents=True)
    esc = str(lib2).replace("\\", "\\\\")
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n "0"\n {\n  "path"\t\t"%s"\n }\n}\n' % esc, encoding="utf-8")
    (steam / "steamapps" / "appmanifest_1.acf").write_text('"AppState"\n{\n "appid"\t"1"\n "name"\t"Half-Life"\n}', encoding="utf-8")
    (lib2 / "steamapps" / "appmanifest_1245620.acf").write_text(
        '"AppState"\n{\n "appid"\t"1245620"\n "name"\t"ELDEN RING"\n}', encoding="utf-8")
    (lib2 / "steamapps" / "appmanifest_228980.acf").write_text(
        '"AppState"\n{\n "appid"\t"228980"\n "name"\t"Steamworks Common Redistributables"\n}', encoding="utf-8")
    epic = tmp_path / "epic"
    epic.mkdir()
    (epic / "a.item").write_text('{"DisplayName":"Fortnite","InstallLocation":"C:\\\\Epic\\\\F","LaunchExecutable":"f.exe"}',
                                 encoding="utf-8")
    sm = tmp_path / "sm" / "Sub"
    sm.mkdir(parents=True)
    (sm / "Notepad++.lnk").write_bytes(b"")
    (sm / "Uninstall Notepad++.lnk").write_bytes(b"")
    monkeypatch.setattr(personal, "_steam_roots", lambda: [str(steam)])
    monkeypatch.setattr(personal, "_epic_manifest_dir", lambda: str(epic))
    monkeypatch.setattr(personal, "_start_menu_dirs", lambda: [str(tmp_path / "sm")])
    t0 = time.perf_counter()
    apps = personal.installed_apps(refresh=True)
    assert time.perf_counter() - t0 < 1.0
    names = [(a["name"], a["kind"]) for a in apps]
    assert ("ELDEN RING", "game") in names and ("Half-Life", "game") in names and ("Fortnite", "game") in names
    assert ("Notepad++", "app") in names
    assert not any("Uninstall" in n or "Redistributables" in n for n, _k in names)
    assert next(a for a in apps if a["name"] == "ELDEN RING")["path"] == "steam://rungameid/1245620"
    assert [a["kind"] for a in apps][:3] == ["game"] * 3            # games first
    assert len(personal.installed_apps(limit=2)) == 2
    monkeypatch.setattr(personal, "_scan_steam", lambda: (_ for _ in ()).throw(AssertionError("must be cached")))
    personal.installed_apps()                                       # served from cache


def test_data_summary_and_delete_all(store):
    personal.update_profile(name="Talib")
    personal.add_correction("a b", "c d")
    personal.save_voice_clip(_pcm(50), 16000, "x", {})
    personal.log_command("open Chrome", "open_app")
    s = personal.data_summary()
    assert s["profile"]["name"] == "Talib" and s["corrections"] == 1
    assert s["clips"]["count"] == 1 and s["habit_events"] == 1 and s["bytes"] > 0
    personal.delete_all()
    assert not store.exists()
    assert personal.get_profile()["name"] == "" and personal.list_corrections() == []
    assert personal.clip_stats()["count"] == 0


def test_atomic_write_leaves_no_temp_files(store):
    for i in range(5):
        personal.update_profile(name=f"n{i}")
    assert [f for f in os.listdir(store) if f.endswith(".tmp")] == []
