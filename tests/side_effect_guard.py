"""Shared side-effect guard for command-pipeline tests (benchmarks + fuzzers).

Everything that could touch the real machine is replaced by a recorder for the duration of the
`with SideEffectGuard() as g:` block: system_control (all public functions and the adapter),
pycaw/comtypes, pyautogui, subprocess, os.startfile/system/popen, webbrowser, requests/urllib/socket,
ctypes.windll, the media adapter, file_ops opener/explorer, screen/camera capture, the scheduler
singleton (never started), clipboard/typing in productivity, and the Groq client.

`g.calls` holds (name, args, kwargs) tuples; `g.names()` the call names. Nothing here is ever
passed through to the real implementation.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import socket
import subprocess
import sys
import tempfile
import types
import urllib.request
import webbrowser
from types import SimpleNamespace
from unittest import mock

_TUPLE_OK = (True, "mocked")


def _default_for(fn):
    ann = ""
    try:
        ann = str(fn.__annotations__.get("return", ""))
    except Exception:
        pass
    if ann.startswith("Tuple"):
        return _TUPLE_OK
    if ann == "str":
        return "mocked window"
    if ann.startswith("Dict"):
        return {}
    if ann == "bool":
        return False
    return None


class SideEffectGuard:
    def __init__(self, cfg=None):
        self.calls: list = []
        self.cfg = cfg
        self._stack = contextlib.ExitStack()
        self._tmp = None

    # ------------------------------------------------------------------ recording helpers
    def rec(self, name, ret=None, raises=None):
        def _f(*a, **k):
            self.calls.append((name, a, k))
            if raises is not None:
                raise raises
            return ret() if callable(ret) else ret
        return _f

    def names(self):
        return [c[0] for c in self.calls]

    def of(self, prefix):
        return [c for c in self.calls if c[0] == prefix or c[0].startswith(prefix)]

    # ------------------------------------------------------------------ install
    def __enter__(self):
        st = self._stack
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis_guard_")
        st.callback(self._tmp.cleanup)

        def patch_attr(obj, name, value, create=False):
            p = mock.patch.object(obj, name, value, create=create)
            st.enter_context(p)

        fake_proc = lambda: mock.MagicMock(returncode=0, stdout="", stderr="", pid=0)
        for n in ("Popen", "run", "call", "check_call", "check_output"):
            patch_attr(subprocess, n, self.rec("subprocess." + n, ret=fake_proc))
        patch_attr(os, "startfile", self.rec("os.startfile"), create=True)
        patch_attr(os, "system", self.rec("os.system", ret=0))
        patch_attr(os, "popen", self.rec("os.popen", ret=fake_proc))
        for n in ("open", "open_new", "open_new_tab"):
            patch_attr(webbrowser, n, self.rec("webbrowser." + n, ret=True))

        # network: record and fail closed
        net_err = OSError("network blocked by SideEffectGuard")
        patch_attr(socket, "create_connection", self.rec("socket.create_connection", raises=net_err))
        patch_attr(socket, "getaddrinfo", self.rec("socket.getaddrinfo", raises=net_err))
        patch_attr(socket.socket, "connect", self.rec("socket.connect", raises=net_err))
        patch_attr(urllib.request, "urlopen", self.rec("urllib.urlopen", raises=net_err))
        try:
            import requests
            patch_attr(requests.Session, "request", self.rec("requests.request", raises=net_err))
            for n in ("get", "post", "put", "delete", "head", "patch", "request"):
                patch_attr(requests, n, self.rec("requests." + n, raises=net_err))
        except ImportError:
            pass

        # raw Win32 (keybd_event, LockWorkStation, clipboard, ...)
        patch_attr(ctypes, "windll", mock.MagicMock(name="windll"), create=True)

        # pycaw / comtypes never really imported
        # (set/restore only these keys: mock.patch.dict(sys.modules) would drop every module imported
        # inside the block on exit, e.g. numpy/cv2, and corrupt the whole pytest session)
        for modname in ("pycaw", "pycaw.pycaw", "pycaw.utils", "comtypes"):
            had = modname in sys.modules
            prev = sys.modules.get(modname)
            sys.modules[modname] = mock.MagicMock(name=modname)
            st.callback(lambda m=modname, h=had, p=prev: sys.modules.__setitem__(m, p) if h else sys.modules.pop(m, None))

        # pyautogui: every public function is a recorder
        import pyautogui
        for n in dir(pyautogui):
            o = getattr(pyautogui, n, None)
            if not n.startswith("_") and isinstance(o, types.FunctionType):
                patch_attr(pyautogui, n, self.rec("pyautogui." + n))

        # system_control: every public function + the adapter factory
        import system_control
        for n, o in list(vars(system_control).items()):
            if (not n.startswith("_") and isinstance(o, types.FunctionType)
                    and o.__module__ == system_control.__name__):
                patch_attr(system_control, n, self.rec("system_control." + n, ret=_default_for(o)))
        patch_attr(system_control, "get_system_control",
                   self.rec("system_control.get_system_control", ret=lambda: mock.MagicMock(name="adapter")))

        # media adapter
        import media_adapter
        patch_attr(media_adapter, "_MEDIA_ADAPTER", mock.MagicMock(name="media_adapter"))

        # file ops: recorders for the opener and the explorer launcher; empty indexer (no scan)
        import file_ops
        import folder_indexer
        cfg = self.cfg
        if cfg is None:
            from config import Config
            cfg = Config()
        self.file_cfg = cfg
        patch_attr(file_ops, "_GLOBAL_ADAPTER", file_ops.FileOperationsAdapter(
            indexer=folder_indexer.FolderIndexer(roots=[]), config=cfg,
            opener=self.rec("file_ops.open"), explorer_launcher=self.rec("file_ops.explorer")))

        # scheduler singleton: temp storage, worker never started
        import scheduler
        patch_attr(scheduler, "_SCHEDULER", scheduler.LocalScheduler(
            storage_path=os.path.join(self._tmp.name, "sched.json")))

        # productivity: notes to temp, typing/clipboard recorded
        import productivity
        patch_attr(productivity, "get_default_notes_path",
                   lambda cfg=None: os.path.join(self._tmp.name, "notes.md"))
        patch_attr(productivity, "type_text", self.rec("productivity.type_text", ret=True))
        patch_attr(productivity, "set_clipboard_text", self.rec("productivity.set_clipboard", ret=True))
        patch_attr(productivity, "get_clipboard_text", self.rec("productivity.get_clipboard", ret=""))

        # screen / camera capture (must stay at zero while ai_enabled is False)
        import screen_awareness
        cap = SimpleNamespace(capture_screen_jpeg=self.rec("capture.screen", ret=b""))
        patch_attr(screen_awareness, "_CAPTURE_ADAPTER", cap)
        if hasattr(screen_awareness, "ImageGrab"):
            patch_attr(screen_awareness, "ImageGrab", SimpleNamespace(grab=self.rec("capture.ImageGrab")))
        try:
            import cv2
            patch_attr(cv2, "VideoCapture", self.rec("capture.camera", ret=lambda: mock.MagicMock()))
        except ImportError:
            pass

        # AI: key present (so the ai_enabled gate is what is being tested), client never networks
        import ai
        import commands
        fake_client = self.rec("groq.client", raises=RuntimeError("groq blocked by SideEffectGuard"))
        patch_attr(ai, "get_api_key", lambda *a, **k: "gsk_fake_guard_key")
        patch_attr(commands, "get_api_key", lambda *a, **k: "gsk_fake_guard_key")
        patch_attr(screen_awareness, "get_api_key", lambda *a, **k: "gsk_fake_guard_key")
        patch_attr(commands, "GroqClient", fake_client)
        patch_attr(screen_awareness, "GroqClient", fake_client)
        patch_attr(ai, "GroqClient", fake_client)

        # config persistence (remember folder, routines, island settings) goes to a temp file, never the repo's
        import config as _config
        _cfg_path = os.path.join(self._tmp.name, "jarvis_config.json")
        patch_attr(_config, "get_config_path", lambda explicit_path=None: explicit_path or _cfg_path)

        commands._PENDING_CONFIRMATION = None
        return self

    def __exit__(self, *exc):
        import commands
        commands._PENDING_CONFIRMATION = None
        self._stack.close()
        return False


def make_ctx(guard: SideEffectGuard, cfg=None):
    """A context object whose every power/UI method is a recorder (no real quit/standby)."""
    import config
    cfg = cfg or config.Config()

    class _Overlay:
        def post(self, kind, **kw):
            guard.calls.append(("overlay.post", (kind,), kw))

        def open_settings(self, *a, **k):
            guard.calls.append(("overlay.open_settings", a, k))

    tracker = SimpleNamespace(get_latest_frame=guard.rec("capture.camera_frame", ret=None))

    class _Ctx:
        def __init__(self):
            self.overlay = _Overlay()
            self.cfg = cfg
            self.tracker = tracker
            self.updater = None
            self.engine = SimpleNamespace(cfg=cfg)
            self.speaker = mock.MagicMock()
            self.quit = guard.rec("ctx.quit")
            self.say = guard.rec("ctx.say")
            self.chirp = guard.rec("ctx.chirp")
            self.set_tracking_paused = guard.rec("ctx.set_tracking_paused")

        def set_active(self, v):
            guard.calls.append(("ctx.set_active", (bool(v),), {}))

        def __getattr__(self, n):
            if n.startswith("__"):
                raise AttributeError(n)
            return guard.rec("ctx." + n)

    return _Ctx()
