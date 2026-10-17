"""
tests/test_fullscreen_detector.py — Unit tests for Windows Fullscreen App & Game Detection.

Verifies:
  - QUNS_BUSY (2) returns True.
  - QUNS_RUNNING_D3D_FULL_SCREEN (3) returns True.
  - QUNS_ACCEPTS_NOTIFICATIONS (5) returns False.
  - Non-fullscreen notification states (1, 4, 6, 7) return False.
  - Safe error handling when native API fails or raises exception.
  - Zero Qt imports; 100% testable with mock query functions.
"""

import unittest
from fullscreen_detector import (
    FullscreenDetector,
    is_fullscreen_active,
    QUNS_NOT_PRESENT,
    QUNS_BUSY,
    QUNS_RUNNING_D3D_FULL_SCREEN,
    QUNS_PRESENTATION_MODE,
    QUNS_ACCEPTS_NOTIFICATIONS,
    QUNS_QUIET_TIME,
    QUNS_APP,
)


class TestFullscreenDetector(unittest.TestCase):
    def test_01_busy_fullscreen_app(self):
        """QUNS_BUSY=2 indicates fullscreen app or game active."""
        detector = FullscreenDetector(query_fn=lambda: QUNS_BUSY)
        self.assertTrue(detector.is_fullscreen())
        self.assertEqual(detector.get_state(), QUNS_BUSY)

    def test_02_direct3d_fullscreen_game(self):
        """QUNS_RUNNING_D3D_FULL_SCREEN=3 indicates fullscreen Direct3D game active."""
        detector = FullscreenDetector(query_fn=lambda: QUNS_RUNNING_D3D_FULL_SCREEN)
        self.assertTrue(detector.is_fullscreen())
        self.assertEqual(detector.get_state(), QUNS_RUNNING_D3D_FULL_SCREEN)

    def test_03_normal_desktop(self):
        """QUNS_ACCEPTS_NOTIFICATIONS=5 indicates standard desktop state."""
        detector = FullscreenDetector(query_fn=lambda: QUNS_ACCEPTS_NOTIFICATIONS)
        self.assertFalse(detector.is_fullscreen())
        self.assertEqual(detector.get_state(), QUNS_ACCEPTS_NOTIFICATIONS)

    def test_04_other_states_not_fullscreen(self):
        """Other shell states (not present, quiet time, app) return False for fullscreen."""
        for state in (QUNS_NOT_PRESENT, QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME, QUNS_APP):
            detector = FullscreenDetector(query_fn=lambda s=state: s)
            self.assertFalse(detector.is_fullscreen())

    def test_05_exception_handling_safe_fallback(self):
        """When query function raises, detector safely catches and returns False."""
        def bad_query():
            raise OSError("Win32 API error")

        detector = FullscreenDetector(query_fn=bad_query)
        self.assertFalse(detector.is_fullscreen())
        self.assertEqual(detector.get_state(), QUNS_ACCEPTS_NOTIFICATIONS)

    def test_06_is_fullscreen_active_helper(self):
        """Top-level helper function supports query_fn injection."""
        self.assertTrue(is_fullscreen_active(query_fn=lambda: QUNS_BUSY))
        self.assertFalse(is_fullscreen_active(query_fn=lambda: QUNS_ACCEPTS_NOTIFICATIONS))


if __name__ == "__main__":
    unittest.main()
