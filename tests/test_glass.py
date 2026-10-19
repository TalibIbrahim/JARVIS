"""
tests/test_glass.py — Liquid Glass material (ui/glass.py, ui/materials.py, glass controls, Settings wiring).

Non-visual: numeric contrast guarantees, material/fallback selection, live setting plumbing, page-order list.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from config import Config
from tools.visual_gate_helper import get_app, set_theme
from ui import glass, materials


def _pump(n: int = 3) -> None:
    for _ in range(n):
        QApplication.processEvents()


class TestContrast(unittest.TestCase):
    """Primary and secondary labels keep >= 4.5:1 in dark and light, at transparency 0 and 1, both modes,
    even over the worst possible backdrop (pure white under a dark window, pure black under a light one)."""

    @classmethod
    def setUpClass(cls):
        get_app()

    def test_labels_meet_4_5_on_every_text_surface(self):
        for dark in (True, False):
            worst = 255.0 if dark else 0.0
            for mode in ("tinted", "clear"):
                for t in (0.0, 0.6, 1.0):
                    for material in ("blur", "mica", "solid"):
                        win = glass.resolve_spec(dark, mode, t, material, worst, popup=False)
                        pop = glass.resolve_spec(dark, mode, t, material, worst, popup=True)
                        self.assertGreaterEqual(win.min_text_contrast(False), 4.5,
                                                f"window dark={dark} {mode} t={t} {material}")
                        self.assertGreaterEqual(pop.min_text_contrast(True), 4.5,
                                                f"popup dark={dark} {mode} t={t} {material}")

    def test_transparency_zero_is_opaque_and_one_lets_backdrop_through(self):
        for dark in (True, False):
            s0 = glass.resolve_spec(dark, "tinted", 0.0, "blur", 128.0)
            s1 = glass.resolve_spec(dark, "clear", 1.0, "blur", 128.0)
            self.assertEqual(s0.window_alpha, 1.0)
            self.assertLess(s1.window_alpha, 0.5)

    def test_clear_is_more_transparent_than_tinted(self):
        for dark in (True, False):
            c = glass.resolve_spec(dark, "clear", 0.6, "blur", 128.0)
            t = glass.resolve_spec(dark, "tinted", 0.6, "blur", 128.0)
            self.assertLess(c.window_alpha, t.window_alpha)
            self.assertLess(c.plate_alpha, t.plate_alpha)

    def test_popup_tint_is_content_adaptive(self):
        bright = glass.resolve_spec(True, "clear", 1.0, "blur", 230.0, popup=True)
        dim = glass.resolve_spec(True, "clear", 1.0, "blur", 20.0, popup=True)
        self.assertGreater(bright.window_alpha, dim.window_alpha)

    def test_accessible_fills(self):
        self.assertGreaterEqual(glass.contrast_ratio((255, 255, 255),
                                (glass.solid_fill_for_white(QColor("#0A84FF")).red(),
                                 glass.solid_fill_for_white(QColor("#0A84FF")).green(),
                                 glass.solid_fill_for_white(QColor("#0A84FF")).blue())), 4.5)
        green = QColor("#30D158")
        txt = glass.readable_on(green)
        self.assertGreaterEqual(glass.contrast_ratio((txt.red(), txt.green(), txt.blue()),
                                                     (green.red(), green.green(), green.blue())), 4.5)

    def test_label_color_returns_copies(self):
        c = glass.label_color("label", True)
        c.setAlphaF(0.1)
        self.assertEqual(glass.label_color("label", True).alpha(), 255)   # shared token was not mutated


class TestMaterials(unittest.TestCase):
    def test_fallback_rule_transparency_off_is_solid_on_every_build(self):
        with patch("sys.platform", "win32"):
            for build in (19045, 22000, 22631):
                for role in ("window", "popup"):
                    self.assertEqual(materials.resolve_material(role, build=build, transparency=False), "solid")

    def test_material_selection_by_build(self):
        with patch("sys.platform", "win32"):
            self.assertEqual(materials.resolve_material("window", build=19045, transparency=True), "blur")
            self.assertEqual(materials.resolve_material("popup", build=19045, transparency=True), "blur")
            self.assertEqual(materials.resolve_material("window", build=22631, transparency=True), "mica")
            self.assertEqual(materials.resolve_material("popup", build=22631, transparency=True), "acrylic")

    def test_apply_acrylic_never_blurs_when_transparency_off(self):
        """Known bug: the Win10 accent path used to blur even with Windows transparency effects off."""
        user32 = MagicMock()
        with patch.object(materials, "_transparency_enabled", return_value=False), \
                patch.object(materials, "_build_number", return_value=19045), \
                patch("ctypes.windll", create=True) as windll:
            windll.user32 = user32
            self.assertFalse(materials.apply_acrylic(1234, True))
            self.assertFalse(materials.apply_blur_behind(1234))
            user32.SetWindowCompositionAttribute.assert_not_called()      # no blur policy was ever set
            # best-backdrop may still CLEAR an earlier blur (remove_backdrop) but must report solid
            self.assertEqual(materials.apply_best_backdrop(1234, True, prefer_mica=True), "solid")

    def test_current_spec_is_solid_when_transparency_off(self):
        get_app()
        with patch.object(materials, "_transparency_enabled", return_value=False):
            glass.environment(force=True)
            spec = glass.current("window", True)
            self.assertEqual(spec.material, "solid")
            self.assertEqual(spec.window_alpha, 1.0)
            self.assertFalse(spec.blurred)
        glass.environment(force=True)


class TestSetting(unittest.TestCase):
    def setUp(self):
        get_app()
        self._saved = glass.settings()

    def tearDown(self):
        glass.configure(mode=self._saved[0], transparency=self._saved[1])

    def test_defaults_and_clamp_via_getattr(self):
        class Bare:
            pass
        glass.configure(Bare())
        self.assertEqual(glass.settings(), ("tinted", 0.6))
        glass.configure(mode="clear", transparency=7.0)
        self.assertEqual(glass.settings(), ("clear", 1.0))
        glass.configure(mode="bogus", transparency=float("nan"))
        self.assertEqual(glass.settings()[0], "tinted")
        self.assertEqual(glass.settings()[1], 0.6)

    def test_listener_fires_on_change_only(self):
        calls = []

        class L:
            def cb(self):
                calls.append(1)
        l = L()
        glass.add_listener(l.cb)
        glass.configure(mode="tinted", transparency=0.3)
        n = len(calls)
        glass.configure(mode="tinted", transparency=0.3)
        self.assertEqual(len(calls), n)          # unchanged -> no notify
        glass.configure(mode="clear")
        self.assertGreater(len(calls), n)
        glass.remove_listener(l.cb)

    def test_config_has_glass_fields(self):
        cfg = Config()
        self.assertEqual(cfg.glass_mode, "tinted")
        self.assertAlmostEqual(cfg.glass_transparency, 0.6)


class TestPageOrderList(unittest.TestCase):
    def setUp(self):
        get_app()
        from ui.components import PageOrderList
        self.defs = [("media", "Media", ""), ("calendar", "Calendar", ""), ("weather", "Weather", "")]
        self.w = PageOrderList(self.defs, ["media", "calendar", "weather"], {"media": True, "weather": False})
        self.w.resize(500, self.w.height())

    def test_order_and_signals(self):
        got = []
        self.w.orderChanged.connect(lambda o: got.append(list(o)))
        self.assertFalse(self.w.move_page("media", -1))        # already first
        self.assertTrue(self.w.move_page("media", +1))
        self.assertEqual(self.w.order(), ["calendar", "media", "weather"])
        self.assertEqual(got[-1], ["calendar", "media", "weather"])
        self.assertTrue(self.w.move_page("weather", -1))
        self.assertEqual(self.w.order(), ["calendar", "weather", "media"])
        self.assertFalse(self.w.rows["calendar"].btn_up.isEnabled())
        self.assertFalse(self.w.rows["media"].btn_down.isEnabled())

    def test_enabled_state_and_signal(self):
        got = []
        self.w.enabledChanged.connect(lambda k, v: got.append((k, v)))
        self.assertFalse(self.w.enabled_map()["weather"])
        self.w.switch_for("weather").setChecked(True)
        self.assertEqual(got, [("weather", True)])
        self.assertTrue(self.w.enabled_map()["weather"])

    def test_unknown_pages_ignored_and_missing_appended(self):
        from ui.components import PageOrderList
        w = PageOrderList(self.defs, ["ghost", "weather"], {})
        self.assertEqual(w.order(), ["weather", "media", "calendar"])

    def test_rows_settle_into_slots(self):
        from PySide6.QtTest import QTest
        self.w.move_page("media", +1)
        QTest.qWait(1500)
        ys = {p: self.w.rows[p].y() for p in self.w.order()}
        self.assertEqual(ys["calendar"], 0)
        self.assertEqual(ys["media"], self.w.ROW_H + self.w.GAP)


class TestSettingsGlassWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        get_app()

    def setUp(self):
        set_theme("dark")
        self._saved = glass.settings()
        self.p = patch("ui.settings_window.list_cameras", return_value=[{"index": 0, "name": "cam"}])
        self.p.start()
        from ui.settings_window import SettingsWindow
        self.cfg = Config()
        self.dlg = SettingsWindow(cfg=self.cfg)
        self.saved = {}
        self.dlg._save = lambda k, v: (self.saved.__setitem__(k, v), setattr(self.cfg, k, v),
                                       glass.configure(self.cfg) if k.startswith("glass_") else None)

    def tearDown(self):
        self.dlg._theme_timer.stop()
        self.dlg.reject()
        self.p.stop()
        glass.configure(mode=self._saved[0], transparency=self._saved[1])

    def test_glass_rows_exist_and_dead_blur_row_is_gone(self):
        self.assertTrue(hasattr(self.dlg, "glass_mode_control"))
        self.assertTrue(hasattr(self.dlg, "glass_transparency_slider"))
        import inspect
        import ui.settings_window as sw
        self.assertNotIn("glass_blur", inspect.getsource(sw))
        self.assertNotIn("Glass Backdrop", inspect.getsource(sw))

    def test_mode_and_transparency_apply_live(self):
        self.dlg.glass_mode_control.setValue("clear")
        self.assertEqual(glass.settings()[0], "clear")
        self.assertEqual(self.saved["glass_mode"], "clear")
        self.dlg.glass_transparency_slider.setValue(0.2)
        self.assertAlmostEqual(glass.settings()[1], 0.2, places=3)       # live immediately
        self.assertTrue(self.dlg._glass_save_timer.isActive())           # persistence debounced
        self.dlg._flush_glass_save()
        self.assertAlmostEqual(self.saved["glass_transparency"], 0.2, places=3)

    def test_page_order_writes_canonical_keys(self):
        self.dlg.page_order_list.move_page("media", +1)
        self.assertEqual(self.saved["island_pages"][:2], ["calendar", "media"])
        self.dlg.page_order_list.switch_for("weather").setChecked(False)
        self.assertFalse(self.saved["island_pages_enabled"]["weather"])

    def test_island_options_present_with_footnotes(self):
        for attr in ("shape_style_control", "island_size_control", "compact_content_control",
                     "clock_format_control", "first_weekday_control", "city_combo", "units_control",
                     "pin_open_switch", "expand_trigger_control", "hide_fullscreen_switch",
                     "page_order_list"):
            self.assertTrue(hasattr(self.dlg, attr), attr)

    def test_window_paints_for_every_material(self):
        for allowed in (True, False):
            with patch.object(materials, "_transparency_enabled", lambda a=allowed: a):
                glass.environment(force=True)
                pm = QPixmap(self.dlg.size())
                pm.fill(QColor(0, 0, 0, 0))
                p = QPainter(pm)
                self.dlg.render(p, QPoint())
                p.end()
                self.assertFalse(pm.isNull())
        glass.environment(force=True)


class TestRadialAndTooltip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        get_app()

    def test_radial_paints_and_morphs(self):
        from ui.cursor_overlay import CursorFeedbackOverlay
        set_theme("dark")
        cur = CursorFeedbackOverlay()
        cur.radial_open = True
        cur.radial_hover = "click"
        cur.resize(220, 220)
        pm = QPixmap(220, 220)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        cur.render(p, QPoint())
        p.end()
        st = cur._glass_radial
        x0 = st["x"].value
        cur.radial_hover = "scroll"
        p = QPainter(pm)
        cur.render(p, QPoint())
        p.end()
        self.assertNotEqual(st["x"].target, x0)          # capsule target moved: spring morph, not a jump
        cur.close()

    def test_tooltip_attach_and_show(self):
        w = QWidget()
        w.setToolTip("hello glass")
        self.assertEqual(glass.attach_tooltips(w), 1)
        tip = glass.GlassTooltip.instance()
        tip.set_text("hello glass")
        self.assertGreater(tip.width(), 40)
        pm = QPixmap(tip.size())
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        tip.render(p, QPoint())
        p.end()


if __name__ == "__main__":
    unittest.main()
