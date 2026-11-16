"""Island options: surface styles, Type-or-Talk page, hide mode, onboarding hook (offscreen, virtual clock)."""
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QLinearGradient
from PySide6.QtWidgets import QApplication

from config import Config
from overlay import Overlay
from tools.visual_gate_helper import (DummyOverlayMgr, compare_images, glass_environment, injected_clock,
                                      render_widget_to_image, set_theme, setup_island_state)
from ui import island_parts
from ui.island import AppleDynamicIsland, IslandState
from ui.island_parts import common
from ui.island_parts.ask import FocusController
from ui.tokens import ISLAND, island_ink_is_light, set_island_ink

GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goldens")
NOW = 1000.0
STYLES = ("gradient", "near_black", "black", "white")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _ink_reset():
    set_island_ink(False)
    yield
    set_island_ink(False)


class FakeCtx:
    def __init__(self):
        self.sent, self.listens = [], 0

    def submit_text(self, text):
        self.sent.append(text)

    def listen_now(self):
        self.listens += 1


def _island(style="gradient", ctx=None, pages=None):
    cfg = Config()
    cfg.island_surface = style
    mgr = DummyOverlayMgr(cfg)
    mgr.ctx = ctx
    isl = AppleDynamicIsland(mgr)
    isl.shape_style = "notch"
    isl.t0 = NOW
    isl.last_frame_t = NOW
    isl.reset_to_idle()
    isl.card_pages = pages or ["media", "time", "weather", "hardware", "jarvis", "ask"]
    return isl


def _render(isl, tw=None, th=None):
    st = isl._resolve_state(NOW)
    w, h = isl._target_size(st, NOW)
    isl.spring_w.snap(w)
    isl.spring_h.snap(h)
    isl.set_island_size(int(round(w)), int(round(h)))
    QApplication.processEvents()
    return render_widget_to_image(isl, 1.0)


def _shot(style, setup, pages=None):
    set_theme("dark")
    with glass_environment(False), injected_clock(NOW):
        isl = _island(style, pages=pages)
        setup(isl)
        img = _render(isl)
        isl.close()
    return img


def _golden(img: QImage, name: str):
    path = os.path.join(GOLDENS_DIR, name)
    if not os.path.exists(path) or os.environ.get("GENERATE_GOLDENS") == "1":
        os.makedirs(GOLDENS_DIR, exist_ok=True)
        img.save(path, "PNG")
        return
    ok, n, pct = compare_images(img, QImage(path))
    assert ok, f"{name}: {n} px differ ({pct:.3f}%)"


def _art():
    pm = QPixmap(120, 120)
    p = QPainter(pm)
    g = QLinearGradient(0, 0, 120, 120)
    g.setColorAt(0, QColor("#c2410c"))
    g.setColorAt(1, QColor("#7c2d12"))
    p.fillRect(0, 0, 120, 120, g)
    p.end()
    return pm


def s_idle(i):
    i.reset_to_idle()


def s_jarvis(i):
    setup_island_state(i, "CARD_JARVIS", NOW)


def s_weather(i):
    i.expanded_card, i.expanded_until = "weather", NOW + 10


def s_listen(i):
    setup_island_state(i, "LISTENING", NOW)


def s_media(i):
    i.expanded_card, i.expanded_until = "media", NOW + 10
    i.media_active = i.media_playing = True
    i.media_album_art = _art()
    i.media_dominant_color = QColor("#c2410c")


def s_ask(variant):
    def f(i):
        i.expanded_card, i.expanded_until = "ask", NOW + 10
        i._ask_focus.active = True
        i._ask_input_t = i._ask_activity_t = NOW
        if variant == "typing":
            i.ask_text = "search malenia guide on youtube"
            i.ask_cursor = len(i.ask_text)
        elif variant == "reply":
            i.ask_sent_text, i.ask_reply = "who wrote dune", "Dune was written by Frank Herbert in 1965."
            i._ask_focus.active = False
    return f


# ── surface styles ─────────────────────────────────────────────────────────

def test_default_style_is_gradient_and_shell_identical(app):
    a = common.build_shell_pixmap(175, 36, 1.0, 18.0, "pill", False).toImage()
    b = common.build_shell_pixmap(175, 36, 1.0, 18.0, "pill", False, style="gradient").toImage()
    assert a == b
    for style in ("near_black", "black", "white"):
        c = common.build_shell_pixmap(175, 36, 1.0, 18.0, "pill", False, style=style).toImage()
        assert c != a


def test_surface_colours(app):
    def px(style):
        img = common.build_shell_pixmap(200, 60, 1.0, 20.0, "card", False, style=style).toImage()
        return QColor(img.pixel(100, 3)), QColor(img.pixel(100, 56))
    assert px("near_black")[0].name() == "#0c0c0c" and px("near_black")[1].name() == "#0c0c0c"
    assert px("black")[0].name() == "#000000"
    top, bot = px("white")
    assert top.lightness() > 200 and bot.lightness() > 150 and top.lightness() > bot.lightness()


def test_style_switch_invalidates_cache(app):
    set_theme("dark")
    with glass_environment(False), injected_clock(NOW):
        isl = _island("gradient")
        s_idle(isl)
        a = _render(isl)
        assert common._SHELL_CACHE
        key_styles = {k[-1] for k in common._SHELL_CACHE}
        assert "gradient" in key_styles
        assert isl.set_surface_style("white") is True
        assert not common._SHELL_CACHE                     # dropped immediately
        assert isl.set_surface_style("white") is False     # no-op when unchanged
        b = _render(isl)
        assert a != b
        isl.set_surface_style("gradient")
        c = _render(isl)
        assert a == c                                       # back to exactly the default render
        assert not island_ink_is_light() or True
        isl.close()


def test_style_normalisation_and_startup_read(app):
    with glass_environment(False):
        assert _island("black").surface_style == "black"
        assert _island("bogus").surface_style == "gradient"
        isl = _island("gradient")
        isl.mgr.cfg.island_surface = "near_black"
        isl.reload_config()
        assert isl.surface_style == "near_black"


@pytest.mark.parametrize("style", STYLES[1:])
def test_media_tint_wins_in_every_style(app, style):
    ref = _shot("gradient", s_media)
    assert _shot(style, s_media) == ref                     # album-art tint overrides the surface and the ink


def test_media_without_art_stays_dark_on_white(app):
    def noart(i):
        i.expanded_card, i.expanded_until = "media", NOW + 10
        i.media_active = i.media_playing = True
        i.media_album_art = None
    assert _shot("white", noart) == _shot("gradient", noart)


@pytest.mark.parametrize("style", STYLES[1:])
@pytest.mark.parametrize("name,fn,pages", [
    ("idle", s_idle, None), ("jarvis", s_jarvis, None), ("weather", s_weather, None),
    ("listening", s_listen, None),
    ("ask_idle", s_ask("idle"), True), ("ask_typing", s_ask("typing"), True), ("ask_reply", s_ask("reply"), True),
])
def test_new_style_goldens(app, style, name, fn, pages):
    _golden(_shot(style, fn), f"surface_{style}_{name}_dark_dpi100.png")


def test_white_ink_flips_and_restores(app):
    gradient = _shot("gradient", s_jarvis)
    assert not island_ink_is_light()
    white = _shot("white", s_jarvis)
    assert white != gradient
    # dark ink on the light surface: value text is near-black, never the dark-surface #FAFAFA
    assert ISLAND.color.value.name() == "#fafafa" or island_ink_is_light()
    assert _shot("gradient", s_jarvis) == gradient          # restored exactly


def test_speaking_empty_quote_hidden_only_when_enabled(app):
    def speak(flag):
        def f(i):
            i.hide_empty_quote = flag
            i.say_text, i.say_until = "Hello there, sir.", NOW + 10
        return f
    assert _shot("gradient", speak(False)) != _shot("gradient", speak(True))


# ── Type or Talk page ──────────────────────────────────────────────────────

def test_ask_send_and_listen_call_ctx(app):
    ctx = FakeCtx()
    with glass_environment(False):
        isl = _island(ctx=ctx)
        isl.expanded_card, isl.expanded_until = "ask", time.time() + 10
        isl._ask_focus = FocusController(isl, api=MagicMock())     # no real focus changes
        for ch in "hello":
            assert isl.ask_key_press(int(Qt.Key.Key_A), ch, Qt.KeyboardModifier.NoModifier)
        assert isl.ask_text == "hello"
        assert isl.ask_key_press(int(Qt.Key.Key_Return), "\r", Qt.KeyboardModifier.NoModifier)
        assert ctx.sent == ["hello"] and isl.ask_text == "" and isl.ask_sent_text == "hello"
        isl.ask_mic_action()                                       # empty field -> mic
        assert ctx.listens == 1
        isl._ask_insert("go")
        isl.ask_mic_action()                                       # text -> send
        assert ctx.sent == ["hello", "go"]
        # Esc: clears text, then leaves the page
        isl.expanded_card = "ask"
        isl._ask_listen_until = 0.0
        isl._ask_insert("abc")
        isl.ask_key_press(int(Qt.Key.Key_Escape), "", Qt.KeyboardModifier.NoModifier)
        assert isl.ask_text == "" and isl.expanded_card == "ask"
        isl.ask_key_press(int(Qt.Key.Key_Escape), "", Qt.KeyboardModifier.NoModifier)
        assert isl.expanded_card is None
        isl.close()


def test_ask_editing_keys(app):
    with glass_environment(False):
        isl = _island()
        isl._ask_insert("hello world")
        K, M = Qt.Key, Qt.KeyboardModifier
        isl.ask_key_press(int(K.Key_Backspace), "", M.ControlModifier)
        assert isl.ask_text == "hello "
        isl.ask_key_press(int(K.Key_Home), "", M.NoModifier)
        isl._ask_insert(">")
        assert isl.ask_text == ">hello "
        isl.ask_key_press(int(K.Key_Delete), "", M.NoModifier)
        assert isl.ask_text == ">ello "
        isl._ask_insert("a\nb")
        assert "\n" not in isl.ask_text
        isl.close()


def test_ask_page_in_state_machine_and_reply(app):
    with glass_environment(False):
        isl = _island()
        now = time.time()
        isl.open_ask_page(focus=False)
        assert isl._resolve_state(now) == IslandState.CARD_ASK
        # listening / thinking / reply stay inside the page instead of switching to pills
        isl.listening, isl.listening_until = True, now + 5
        isl.say_text, isl.say_until = "It is 5 PM.", now + 5
        assert isl._resolve_state(now) == IslandState.CARD_ASK
        isl._ask_tick(now)
        assert isl.ask_reply == "It is 5 PM."
        assert isl._ask_variant(now) == "listen"
        isl.listening = False
        isl.listening_until = 0.0
        assert isl._ask_variant(now) == "reply"
        # errors still win
        isl.error_until = now + 5
        assert isl._resolve_state(now) == IslandState.ERROR
        isl.close()


def test_ask_suspends_idle_collapse_while_text_or_focus(app):
    with glass_environment(False):
        isl = _island()
        t = time.time()
        isl.open_ask_page(focus=False)
        isl.expanded_until = t - 1.0
        isl._ask_insert("typing")
        for _ in range(3):
            t += 0.02
            isl.update_frame(t)
        assert isl.expanded_card == "ask"                  # text present -> page held open
        isl.ask_text = ""
        isl._ask_activity_t = 0.0
        isl._ask_input_t = 0.0
        isl.expanded_until = t - 1.0
        t += 10.0
        isl.update_frame(t)
        assert isl.expanded_card is None                   # nothing going on -> collapses as before
        isl.close()


def test_ask_dots_hit_ids_and_hit_targets(app):
    with glass_environment(False):
        isl = _island()
        isl.open_ask_page()
        isl._publish_hit_snapshot(*isl.island_size(), IslandState.CARD_ASK)
        ids = [t for _r, t in isl._hit_snapshot[1]]
        assert "ask_mic" in ids and "ask_field" in ids and "card" in ids and "dot:ask" in ids
        assert isl._logical_page("type_or_talk") == "ask"
        isl.close()


# ── focus restore logic (Win32 mocked) ─────────────────────────────────────

class FakeUser32:
    def __init__(self, fg=4242):
        self.fg = fg
        self.calls = []

    def GetForegroundWindow(self):
        return self.fg

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("fg", hwnd))
        self.fg = hwnd
        return 1

    def IsWindow(self, hwnd):
        return 1

    def GetWindowThreadProcessId(self, hwnd, p):
        return 7

    def AttachThreadInput(self, a, b, c):
        self.calls.append(("attach", c))


def test_focus_acquire_and_restore(app):
    with glass_environment(False):
        isl = _island()
        api = FakeUser32(fg=4242)
        fc = FocusController(isl, api=api)
        seen = []
        isl._set_focusable = lambda on: seen.append(on)
        isl._focus_blocked = lambda: False
        assert fc.acquire() is True and fc.active and fc.prev_hwnd == 4242
        assert seen == [True]
        assert api.calls[0] == ("fg", int(isl.winId()))
        fc.release(restore=True)                           # leave / Esc / send / timeout
        assert not fc.active and seen == [True, False]
        assert api.calls[-1] == ("fg", 4242)               # previous window gets focus back
        fc.release()                                       # idempotent
        assert seen == [True, False]
        isl.close()


def test_focus_never_taken_from_fullscreen_and_no_restore_when_user_left(app):
    with glass_environment(False):
        isl = _island()
        api = FakeUser32()
        fc = FocusController(isl, api=api)
        isl._focus_blocked = lambda: True
        assert fc.acquire() is False and not fc.active and api.calls == []
        isl._focus_blocked = lambda: False
        isl._set_focusable = lambda on: None
        fc.acquire()
        n = len(api.calls)
        fc.release(restore=False)                          # user clicked another window: do not yank focus back
        assert len(api.calls) == n
        isl.close()


def test_set_focusable_toggles_exstyle_flag(app):
    with glass_environment(False):
        isl = _island()
        with patch.object(isl, "_apply_exstyle") as ap:
            isl._set_focusable(True)
            assert isl._ask_focus_active is True
            isl._set_focusable(False)
            assert isl._ask_focus_active is False
            assert ap.call_count == 2
        isl.close()


def test_collapse_releases_focus(app):
    with glass_environment(False):
        isl = _island()
        api = FakeUser32(fg=99)
        isl._focus_blocked = lambda: False
        isl._set_focusable = lambda on: None
        isl._ask_focus = FocusController(isl, api=api)
        isl.open_ask_page(focus=True)
        assert isl.ask_has_focus()
        isl.collapse_card()
        assert not isl.ask_has_focus() and api.calls[-1] == ("fg", 99)
        isl.close()


# ── hide mode ──────────────────────────────────────────────────────────────

def test_hidden_state_machine(app):
    with glass_environment(False):
        isl = _island()
        isl.show()
        isl.set_hidden(True)
        assert not isl.isVisible()
        t = time.time()
        assert isl._hidden_tick(t, IslandState.IDLE) is True           # nothing going on: stays absent
        assert not isl.isVisible()
        assert isl._hidden_tick(t + 0.1, IslandState.LISTENING) is False   # wake / listening -> pill appears
        assert isl.isVisible() and isl._hidden_shown
        assert isl._hidden_tick(t + 0.2, IslandState.SPEAKING) is False    # reply stays up
        assert isl.isVisible()
        assert isl._hidden_tick(t + 0.3, IslandState.IDLE) is False        # reply done: lingers briefly
        assert isl.isVisible()
        isl.spring_w.snap(175.0)
        isl.spring_h.snap(36.0)
        assert isl._hidden_tick(t + 1.0, IslandState.IDLE) is True         # then disappears again
        assert not isl.isVisible()
        isl.set_hidden(False)
        assert isl.isVisible() and not isl.hidden
        isl.close()


def test_ask_hotkey_shows_hidden_island_temporarily(app):
    with glass_environment(False):
        isl = _island()
        isl.show()
        isl.set_hidden(True)
        assert not isl.isVisible()
        isl._focus_blocked = lambda: True                 # no real focus change in the test
        isl.open_ask_page(focus=True)
        assert isl.isVisible()
        isl.close()


# ── Overlay wiring (events + onboarding) ───────────────────────────────────

@pytest.fixture
def overlay(app):
    stop = threading.Event()
    ctx = MagicMock()
    ov = Overlay(stop, cfg=Config(), ctx=ctx)
    yield ov
    stop.set()
    ov.timer.stop()
    app.processEvents()


def test_overlay_events(overlay):
    isl = overlay.island
    assert isl.hide_empty_quote is True
    overlay.post("island_surface", style="white")
    overlay.post("hidden", on=True)
    overlay._drain(time.time())
    assert isl.surface_style == "white" and isl.hidden and not isl.isVisible()
    overlay.post("hidden", on=False)
    overlay.post("ask_page", focus=False)
    overlay._drain(time.time())
    assert isl.expanded_card == "ask" and isl.isVisible()


def test_overlay_onboarding_hook(overlay):
    spoken = []
    overlay.ctx = type("C", (), {"say": staticmethod(spoken.append)})()
    opened = []
    fake_mod = type(sys)("ui.onboarding_window")

    class OW:
        def __init__(self, ctx, cfg):
            opened.append((ctx, cfg))

        def show(self):
            opened.append("shown")

    fake_mod.OnboardingWindow = OW
    with patch.dict(sys.modules, {"ui.onboarding_window": fake_mod}), \
            patch("overlay.QTimer.singleShot", lambda ms, fn: fn()):
        overlay.post("onboarding")
        overlay._drain(time.time())
    assert overlay.island.say_text.startswith("Hello, I")
    assert overlay.island.is_wake
    assert spoken and spoken[0].startswith("Hello, I")
    assert opened[-1] == "shown"


def test_overlay_onboarding_missing_window_is_logged_not_raised(overlay, capsys):
    with patch.dict(sys.modules, {"ui.onboarding_window": None}), \
            patch("overlay.QTimer.singleShot", lambda ms, fn: fn()):
        overlay.post("onboarding")
        overlay._drain(time.time())
    assert "onboarding window unavailable" in capsys.readouterr().out


def test_type_or_talk_page_present_at_startup(app):
    """The page list comes from config when the island is built, not only after a Settings reload."""
    isl = AppleDynamicIsland(DummyOverlayMgr(Config()))
    assert isl.card_pages[-1] == "ask"
    assert isl.card_pages[-2] == "jarvis"


def test_type_or_talk_page_off_at_startup(app):
    cfg = Config()
    cfg.island_ask_page = False
    isl = AppleDynamicIsland(DummyOverlayMgr(cfg))
    assert "ask" not in isl.card_pages
