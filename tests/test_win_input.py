"""win_input with a fake user32: SendInput is recorded, never sent to the real desktop."""
import ctypes
from unittest.mock import MagicMock, patch

import pytest

import win_input as wi


class FakeUser32:
    def __init__(self):
        self.batches = []

    def SendInput(self, n, arr, size):
        assert size == ctypes.sizeof(wi.INPUT)
        evs = []
        for i in range(n):
            it = arr[i]
            if it.type == wi.INPUT_KEYBOARD:
                evs.append(("key", it.ki.wVk, it.ki.wScan, it.ki.dwFlags))
            else:
                evs.append(("mouse", it.mi.dwFlags, it.mi.mouseData))
        self.batches.append(evs)
        return n


@pytest.fixture
def u32(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(wi, "_user32", fake)
    return fake


def _keys(batches):
    return [(vk, bool(fl & wi.KEYEVENTF_KEYUP)) for b in batches for (_, vk, _, fl) in b]


def test_hotkey_presses_in_order_and_releases_in_reverse(u32):
    wi.hotkey("alt", "shift", "tab")
    assert len(u32.batches) == 1
    assert _keys(u32.batches) == [(0x12, False), (0x10, False), (0x09, False),
                                  (0x09, True), (0x10, True), (0x12, True)]


def test_press_repeats_and_marks_media_keys_extended(u32):
    wi.press("volumeup", presses=3)
    evs = u32.batches[0]
    assert len(evs) == 6 and all(e[1] == 0xAF and e[3] & wi.KEYEVENTF_EXTENDEDKEY for e in evs)
    wi.press("f4")
    assert u32.batches[1][0][3] & wi.KEYEVENTF_EXTENDEDKEY == 0


def test_unknown_key_raises(u32):
    with pytest.raises(ValueError):
        wi.press("nosuchkey")
    assert u32.batches == []


def test_write_sends_unicode_events_with_interval(u32):
    with patch.object(wi.time, "sleep") as sl:
        wi.write("hé", interval=0.01)
    assert [c.args for c in sl.call_args_list] == [(0.01,)]
    down_h, up_h = u32.batches[0]
    assert down_h == ("key", 0, ord("h"), wi.KEYEVENTF_UNICODE)
    assert up_h == ("key", 0, ord("h"), wi.KEYEVENTF_UNICODE | wi.KEYEVENTF_KEYUP)
    assert u32.batches[1][0][2] == ord("é")


def test_write_surrogate_pair_and_newline(u32):
    wi.write("\U0001F600\n")
    emoji = u32.batches[0]
    assert [e[2] for e in emoji] == [0xD83D, 0xDE00, 0xD83D, 0xDE00]
    assert [bool(e[3] & wi.KEYEVENTF_KEYUP) for e in emoji] == [False, False, True, True]
    assert _keys([u32.batches[1]]) == [(0x0D, False), (0x0D, True)]


def test_mouse_click_and_scroll(u32):
    wi.click("right", clicks=2)
    assert [e[1] for e in u32.batches[0]] == [wi.MOUSEEVENTF_RIGHTDOWN, wi.MOUSEEVENTF_RIGHTUP] * 2
    wi.scroll(-600)
    assert u32.batches[1] == [("mouse", wi.MOUSEEVENTF_WHEEL, (-600) & 0xFFFFFFFF)]


def test_screenshot_uses_imagegrab(tmp_path):
    img = MagicMock()
    with patch("PIL.ImageGrab.grab", return_value=img) as grab:
        assert wi.screenshot(str(tmp_path / "s.png")) is img
    grab.assert_called_once_with()
    img.save.assert_called_once_with(str(tmp_path / "s.png"))
