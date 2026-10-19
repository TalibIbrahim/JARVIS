"""EchoCanceller with the loopback replaced by feed_reference()/enable_for_test(). No audio devices."""
import logging

import numpy as np
import pytest

import echo_canceller as ec
from echo_canceller import EchoCanceller, FRAME, SR

try:
    import aec_audio_processing  # noqa: F401
    _HAVE_APM = True
except Exception:
    _HAVE_APM = False

needs_apm = pytest.mark.skipif(not _HAVE_APM, reason="aec_audio_processing cannot be imported")

CHUNK = 1280


def _canceller():
    c = EchoCanceller()
    c.enable_for_test()
    return c


def test_silence_returns_same_object():
    c = _canceller()
    x = (np.random.default_rng(0).normal(0, 500, CHUNK)).astype(np.int16)
    assert c.process(x) is x       # no reference fed: passthrough, identical object
    c.feed_reference(np.zeros(CHUNK, dtype=np.int16))
    assert c.process(x) is x       # silent reference: still passthrough


def test_disabled_returns_input_unchanged():
    c = EchoCanceller()            # never enabled
    x = np.ones(CHUNK, dtype=np.int16)
    assert c.process(x) is x


@needs_apm
def test_echo_reduced_by_more_than_10db():
    rng = np.random.default_rng(1)
    n_chunks = 150                 # 12 s
    total = n_chunks * CHUNK
    # band-limited noise "music", then the mic hears a delayed, attenuated copy
    music = rng.normal(0, 4000, total)
    music = np.convolve(music, np.ones(4) / 4, mode="same")
    ref = np.clip(music, -32768, 32767).astype(np.int16)
    delay = int(SR * 0.08)
    echo = np.zeros(total)
    echo[delay:] = 0.5 * ref[:-delay]
    mic = np.clip(echo, -32768, 32767).astype(np.int16)

    c = _canceller()
    e_in = e_out = 0.0
    for k in range(n_chunks):
        sl = slice(k * CHUNK, (k + 1) * CHUNK)
        c.feed_reference(ref[sl])
        out = c.process(mic[sl])
        assert len(out) == CHUNK
        if k >= n_chunks // 2:     # measure after convergence
            e_in += float(np.sum(mic[sl].astype(np.float64) ** 2))
            e_out += float(np.sum(out.astype(np.float64) ** 2))
    erle = 10 * np.log10(e_in / max(e_out, 1e-9))
    assert erle > 10.0, f"ERLE {erle:.1f} dB"
    st = c.stats()
    assert st["frames"] > 0 and st["erle_db_estimate"] > 0
    c.close()


def test_start_failure_passthrough_and_single_warning(monkeypatch, caplog):
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "pyaudiowpatch":
            raise ImportError("no loopback in test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    c = EchoCanceller()
    with caplog.at_level(logging.WARNING, logger=ec.log.name):
        assert c.start() is False
        assert c.start() is False          # second failure: still no raise
    monkeypatch.undo()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in warns]
    x = np.ones(CHUNK, dtype=np.int16)
    assert c.process(x) is x
    c.close()


def test_start_failure_from_device_error(monkeypatch, caplog):
    import sys
    import types
    fake = types.ModuleType("pyaudiowpatch")
    fake.paInt16 = 8

    class PA:
        terminated = 0

        def get_default_wasapi_loopback(self):
            raise OSError("no wasapi loopback device")

        def terminate(self):
            PA.terminated += 1

    fake.PyAudio = PA
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", fake)
    c = EchoCanceller()
    with caplog.at_level(logging.WARNING, logger=ec.log.name):
        assert c.start() is False
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
    assert PA.terminated == 1      # the PyAudio instance is released on failure


def test_close_is_idempotent():
    c = _canceller()
    c.close()
    c.close()
    c.stop()
    x = np.ones(CHUNK, dtype=np.int16)
    assert c.process(x) is x       # closed => passthrough


def test_close_closes_stream_once():
    from unittest.mock import MagicMock
    c = _canceller()
    stream, pa = MagicMock(), MagicMock()
    c._stream, c._pa = stream, pa
    c.close()
    c.close()
    stream.close.assert_called_once()
    pa.terminate.assert_called_once()


def test_stats_keys():
    st = _canceller().stats()
    assert set(st) >= {"enabled", "loopback_device", "erle_db_estimate", "cpu_ms_per_s", "frames"}
    assert st["enabled"] is True and st["frames"] == 0 and st["cpu_ms_per_s"] == 0.0
    assert EchoCanceller().stats()["enabled"] is False
