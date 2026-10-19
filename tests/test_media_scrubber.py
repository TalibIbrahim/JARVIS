"""Media scrubber: releasing a drag seeks the player; nothing moves the knob mid-drag (offscreen)."""
import sys
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from config import Config
from overlay import Overlay


def _release(button=Qt.MouseButton.LeftButton) -> QMouseEvent:
    return QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(0, 0), QPointF(0, 0),
                       button, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)


class TestMediaScrubber(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.stop_evt = threading.Event()
        self.ov = Overlay(self.stop_evt, cfg=Config())
        self.island = self.ov.island
        self.island.reset_to_idle()
        self.island.media_active = True
        self.island.media_playing = True
        self.island.media_duration_sec = 200.0

    def tearDown(self):
        self.stop_evt.set()
        self.ov.timer.stop()
        self.app.processEvents()

    def test_release_after_drag_seeks_to_knob(self):
        self.island._dragging_scrubber = True
        self.island.media_progress = 0.5
        with patch.object(self.island, "media_seek") as seek:
            self.island.mouseReleaseEvent(_release())
        seek.assert_called_once_with(0.5)
        self.assertFalse(self.island._dragging_scrubber)

    def test_release_without_drag_does_not_seek(self):
        with patch.object(self.island, "media_seek") as seek:
            self.island.mouseReleaseEvent(_release())
        seek.assert_not_called()

    def test_release_with_unknown_duration_does_not_seek(self):
        self.island.media_duration_sec = 0.0
        self.island._dragging_scrubber = True
        with patch.object(self.island, "media_seek") as seek:
            self.island.mouseReleaseEvent(_release())
        seek.assert_not_called()

    def test_playback_tick_holds_knob_mid_drag(self):
        self.island._dragging_scrubber = True
        self.island.media_position_sec, self.island.media_progress = 50.0, 0.25
        for i in range(5):
            self.island.update_frame(1000.0 + i * 0.1)
        self.assertEqual((self.island.media_position_sec, self.island.media_progress), (50.0, 0.25))


if __name__ == "__main__":
    unittest.main()
