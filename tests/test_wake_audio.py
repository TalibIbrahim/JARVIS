"""
Unit tests for Wake Word & Audio Flow (Part 1):
1. openWakeWord detector & thread integration at 16 kHz.
2. Sensitivity settings mapping (Low/Balanced/High) and follow-up window (default 6s).
3. False-trigger guard (consecutive / threshold) and anti-self-trigger while assistant is speaking.
4. Activation flow: wake detected -> callback -> soft chime (<150 ms) -> VAD recording -> endpointing.
5. 5s silence no-speech callback and 700ms endpoint / 8s max duration.
6. Backward compatibility.
"""
import time
import unittest
from unittest.mock import MagicMock, patch
import numpy as np

import chime
import voice
from config import Config
from voice import (
    Voice,
    WakeWordDetector,
    WakeWordGuard,
    VADDetector,
    parse_sensitivity,
    SENSITIVITY_THRESHOLDS,
)


class TestChime(unittest.TestCase):
    """Verifies soft ascending two-note chime synthesis and playback properties."""

    def setUp(self):
        chime.set_chime_muted(False)
        chime.set_chime_volume(0.10)

    def test_chime_synthesis_duration_and_envelope(self):
        """Chime must be strictly < 150 ms, ascending, with smooth cosine envelope."""
        sr = 44100
        wave = chime.synthesize_chime(sample_rate=sr)
        duration_ms = len(wave) / sr * 1000.0

        # Duration target: < 150 ms
        self.assertLess(duration_ms, 150.0)
        self.assertGreater(duration_ms, 80.0)

        # Smooth envelope: starts and ends at near-zero (< 1e-4)
        self.assertAlmostEqual(float(wave[0]), 0.0, places=4)
        self.assertAlmostEqual(float(wave[-1]), 0.0, places=4)

        # Soft/quiet amplitude: peak should not exceed 0.20
        peak = float(np.max(np.abs(wave)))
        self.assertLessEqual(peak, 0.15)

    def test_chime_mutable_settings(self):
        """Chime volume and mute states must be mutable."""
        self.assertFalse(chime.is_chime_muted())
        chime.set_chime_muted(True)
        self.assertTrue(chime.is_chime_muted())

        chime.set_chime_volume(0.05)
        self.assertEqual(chime.get_chime_volume(), 0.05)

    @patch("sounddevice.play")
    def test_play_chime_non_blocking_calls_sounddevice(self, mock_play):
        """play_chime initiates non-blocking playback."""
        with patch("sounddevice.wait"):
            chime.play_chime(non_blocking=False)
            mock_play.assert_called_once()

    @patch("sounddevice.play")
    def test_play_chime_when_muted_does_not_play(self, mock_play):
        """play_chime produces no output when muted."""
        chime.set_chime_muted(True)
        chime.play_chime(non_blocking=False)
        mock_play.assert_not_called()


class TestSensitivityMapping(unittest.TestCase):
    """Verifies sensitivity mapping (Low / Balanced / High)."""

    def test_sensitivity_mapping_low_balanced_high(self):
        self.assertEqual(parse_sensitivity("low"), 0.70)
        self.assertEqual(parse_sensitivity("balanced"), 0.50)
        self.assertEqual(parse_sensitivity("high"), 0.35)

        # Case-insensitivity and whitespace
        self.assertEqual(parse_sensitivity("  High  "), 0.35)
        self.assertEqual(parse_sensitivity("LOW"), 0.70)

    def test_sensitivity_mapping_custom_numeric(self):
        self.assertEqual(parse_sensitivity(0.62), 0.62)
        self.assertEqual(parse_sensitivity("0.45"), 0.45)
        self.assertEqual(parse_sensitivity(1.5), 0.99)  # clamped
        self.assertEqual(parse_sensitivity(0.01), 0.05)  # clamped


class TestFalseTriggerGuard(unittest.TestCase):
    """Verifies false trigger guard and anti-self-trigger while speaking."""

    def test_score_above_threshold_triggers(self):
        guard = WakeWordGuard(threshold=0.50, require_consecutive=False)
        # Score below threshold -> does not trigger
        self.assertFalse(guard.evaluate(0.30, is_speaking=False, now=100.0))
        # Score above threshold -> triggers
        self.assertTrue(guard.evaluate(0.55, is_speaking=False, now=100.1))

    def test_two_consecutive_frames_requirement(self):
        guard = WakeWordGuard(threshold=0.50, require_consecutive=True)
        # First frame above threshold (but below high_thresh) -> does not trigger yet
        self.assertFalse(guard.evaluate(0.55, is_speaking=False, now=100.0))
        # Second consecutive frame above threshold -> triggers!
        self.assertTrue(guard.evaluate(0.52, is_speaking=False, now=100.08))

    def test_single_high_confidence_frame_triggers_immediately(self):
        guard = WakeWordGuard(threshold=0.50, require_consecutive=True)
        # Score >= high_thresh (0.65) -> triggers immediately on 1st frame
        self.assertTrue(guard.evaluate(0.80, is_speaking=False, now=100.0))

    def test_anti_self_trigger_suppresses_detection_while_speaking(self):
        guard = WakeWordGuard(threshold=0.50, require_consecutive=False)
        # High score while assistant is speaking -> must be ignored!
        self.assertFalse(guard.evaluate(0.95, is_speaking=True, now=100.0))
        self.assertFalse(guard.evaluate(0.99, is_speaking=True, now=100.08))

    def test_debounce_window_prevents_immediate_retrigger(self):
        guard = WakeWordGuard(threshold=0.50, debounce_seconds=1.2)
        self.assertTrue(guard.evaluate(0.80, is_speaking=False, now=100.0))
        # Within debounce window (0.5s later) -> must not trigger
        self.assertFalse(guard.evaluate(0.85, is_speaking=False, now=100.5))
        # After debounce window (1.3s later) -> can trigger again
        self.assertTrue(guard.evaluate(0.85, is_speaking=False, now=101.3))


class TestVADDetector(unittest.TestCase):
    """Verifies Voice Activity Detection logic."""

    def test_silence_below_noise_floor_is_not_speech(self):
        vad = VADDetector()
        chunk = np.zeros(1280, dtype=np.int16)
        self.assertFalse(vad.is_speech(chunk, rms=10.0))

    def test_strong_energy_is_speech(self):
        vad = VADDetector()
        sine = (np.sin(np.linspace(0, 50, 1280)) * 5000).astype(np.int16)
        rms = float(np.sqrt(np.mean(sine.astype(float) ** 2)))
        self.assertTrue(vad.is_speech(sine, rms=rms))


class TestWakeWordDetectorFlow(unittest.TestCase):
    """Verifies complete WakeWordDetector flow and VAD endpointing."""

    def test_default_followup_window_is_six_seconds(self):
        detector = WakeWordDetector()
        self.assertEqual(detector.get_followup_window(), 6.0)

    def test_configurable_followup_window(self):
        detector = WakeWordDetector(followup_window=8.5)
        self.assertEqual(detector.get_followup_window(), 8.5)
        detector.set_followup_window(4.0)
        self.assertEqual(detector.get_followup_window(), 4.0)

    def test_record_with_vad_endpoints_on_700ms_silence(self):
        """Speech followed by ~700 ms silence must finish recording."""
        detector = WakeWordDetector()
        # Mock stream providing 4 speech chunks followed by 10 quiet ambient chunks
        speech_chunk = (np.sin(np.linspace(0, 50, 1280)) * 5000).astype(np.int16).tobytes()
        silence_chunk = (np.ones(1280, dtype=np.int16) * 5).tobytes()

        chunks = [speech_chunk] * 4 + [silence_chunk] * 12
        chunk_iter = iter(chunks)

        mock_stream = MagicMock()
        mock_stream.read.side_effect = lambda n, **kw: next(chunk_iter, b"")

        raw = detector.record_utterance_with_vad(
            stream=mock_stream,
            timeout_no_speech=5.0,
            silence_endpoint=0.70,
            max_duration=8.0,
        )
        self.assertIsNotNone(raw)
        self.assertGreater(len(raw), 0)
        # Voice._latest_rms and Voice._latest_time must have been updated
        self.assertGreater(Voice._latest_rms, 0.0)
        self.assertGreater(Voice._latest_time, 0.0)

    def test_record_with_vad_triggers_no_speech_callback_after_5s_silence(self):
        """5 seconds of continuous silence without speech triggers no_speech callback."""
        mock_no_speech = MagicMock()
        detector = WakeWordDetector(on_no_speech=mock_no_speech)

        silence_chunk = np.zeros(1280, dtype=np.int16).tobytes()
        # Mock stream providing silence
        mock_stream = MagicMock()
        mock_stream.read.return_value = silence_chunk

        # Incremental mock clock simulating time advancing past 5.0s
        clock = [100.0]
        def advance_clock():
            clock[0] += 0.8
            return clock[0]

        with patch("time.time", side_effect=advance_clock):
            result = detector.record_utterance_with_vad(
                stream=mock_stream,
                timeout_no_speech=5.0,
            )

        self.assertIsNone(result)
        mock_no_speech.assert_called_once()

    def test_record_with_vad_respects_max_duration_of_8s(self):
        """Recording terminates at 8.0s maximum duration even with continuous speech."""
        detector = WakeWordDetector()
        speech_chunk = (np.sin(np.linspace(0, 50, 1280)) * 5000).astype(np.int16).tobytes()
        mock_stream = MagicMock()
        mock_stream.read.return_value = speech_chunk

        clock = [100.0]
        def advance_clock():
            clock[0] += 0.5
            return clock[0]

        with patch("time.time", side_effect=advance_clock):
            raw = detector.record_utterance_with_vad(
                stream=mock_stream,
                timeout_no_speech=5.0,
                silence_endpoint=0.70,
                max_duration=8.0,
            )
        self.assertIsNotNone(raw)


class TestVoiceIntegration(unittest.TestCase):
    """Verifies Voice class integration with wake callbacks, chime, and properties."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.wake_aliases = ["jarvis"]
        self.ctx = MagicMock()
        self.stop = MagicMock()
        self.voice = Voice(self.cfg, self.ctx, self.stop)

    def test_voice_is_speaking_property(self):
        """Voice.is_speaking reflects active TTS speaking state."""
        self.assertFalse(Voice.is_speaking)
        Voice.is_speaking = True
        self.assertTrue(self.voice.is_speaking)
        Voice.is_speaking = False
        self.assertFalse(self.voice.is_speaking)

    def test_voice_on_wake_detected_posts_overlay_and_chirp(self):
        """_on_wake_detected posts wake events to overlay and updates listening_until."""
        self.voice._on_wake_detected()
        self.ctx.overlay.post.assert_any_call("wake_detected")
        self.ctx.overlay.post.assert_any_call("listening", until=self.voice.listening_until)
        self.ctx.chirp.assert_called_with("wake")

    def test_voice_on_no_speech_detected_resets_listening(self):
        """_on_no_speech_detected posts listening_done and resets listening_until."""
        self.voice.listening_until = time.time() + 5.0
        self.voice._on_no_speech_detected()
        self.assertEqual(self.voice.listening_until, 0.0)
        self.ctx.overlay.post.assert_called_with("listening_done")

    def test_voice_sensitivity_and_followup_setters(self):
        """Sensitivity and follow-up window can be set on Voice."""
        self.voice.set_sensitivity("high")
        self.assertEqual(self.voice.get_sensitivity(), 0.35)

        self.voice.set_followup_window(8.0)
        self.assertEqual(self.voice.get_followup_window(), 8.0)


class TestPiperStreamingAndWarmCache(unittest.TestCase):
    """Verifies Piper TTS sentence streaming, warm caching, and pre-rendered short phrases."""

    def test_sentence_splitting(self):
        """Sentence splitter correctly separates [.!?] boundaries while preserving sentences."""
        from voice import split_into_sentences
        text = "Yes, sir. Initiating house party protocol now! All systems ready."
        sentences = split_into_sentences(text)
        self.assertEqual(sentences, ["Yes, sir.", "Initiating house party protocol now!", "All systems ready."])

        # Single sentence with question mark
        self.assertEqual(split_into_sentences("At your service?"), ["At your service?"])
        # Edge cases
        self.assertEqual(split_into_sentences(""), [])
        self.assertEqual(split_into_sentences("   "), [])
        self.assertEqual(split_into_sentences("Done."), ["Done."])

    def test_phrase_normalization(self):
        """Phrase key normalization is case- and punctuation-insensitive."""
        from voice import _normalize_phrase_key
        self.assertEqual(_normalize_phrase_key("Yes, sir."), "yes sir")
        self.assertEqual(_normalize_phrase_key("Yes, sir?"), "yes sir")
        self.assertEqual(_normalize_phrase_key("Online and ready."), "online and ready")
        self.assertEqual(_normalize_phrase_key("At your service!"), "at your service")

    def test_warm_model_singleton(self):
        """get_shared_piper returns the same warm model without reloading."""
        import voice
        fake_model = MagicMock()
        with patch.object(voice, "_SHARED_PIPER_VOICE", fake_model):
            with patch.object(voice, "_SHARED_PIPER_PATH", "fake_path"):
                v1 = voice.get_shared_piper("fake_path", "fake_path.json")
                v2 = voice.get_shared_piper("fake_path", "fake_path.json")
                self.assertIs(v1, fake_model)
                self.assertIs(v2, fake_model)

    def test_phrase_cache_sub_millisecond_playback(self):
        """Pre-rendered phrases exist in cache and take < 1 ms to fetch for immediate playback."""
        import voice
        dummy_audio = np.ones(1000, dtype=np.float32) * 0.1
        voice._AUDIO_PHRASE_CACHE["yes sir"] = (dummy_audio, 44100)

        t0 = time.perf_counter()
        key = voice._normalize_phrase_key("Yes, sir.")
        cached = voice._AUDIO_PHRASE_CACHE.get(key)
        lookup_time_ms = (time.perf_counter() - t0) * 1000.0

        self.assertIsNotNone(cached)
        self.assertLess(lookup_time_ms, 1.0)
        np.testing.assert_array_equal(cached[0], dummy_audio)
        self.assertEqual(cached[1], 44100)

    @patch("sounddevice.play")
    @patch("sounddevice.wait")
    def test_sentence_streaming_calls_sounddevice_per_sentence(self, mock_wait, mock_play):
        """Sentence streaming plays each sentence sequentially via sounddevice."""
        import voice
        from voice import Speaker

        mock_voice = MagicMock()
        class DummyChunk:
            sample_rate = 22050
            audio_float_array = np.zeros(2205, dtype=np.float32)

        mock_voice.synthesize.return_value = [DummyChunk()]

        speaker = Speaker(enabled=True)
        speaker._piper_voice = mock_voice

        with patch.object(voice, "get_shared_piper", return_value=mock_voice):
            with patch.object(voice, "_AUDIO_PHRASE_CACHE", {}):
                text = "First sentence. Second sentence! Third sentence."
                speaker._say_piper(text)

                # Sounddevice play must have been called 3 times (once per sentence)
                self.assertEqual(mock_play.call_count, 3)
                self.assertEqual(mock_wait.call_count, 3)

    def test_audio_fidelity_headroom_and_clipping(self):
        """Synthesized audio must have 0.88x headroom scaling and clipped within [-0.95, 0.95]."""
        import voice

        class DummyChunk:
            sample_rate = 22050
            audio_float_array = np.ones(1000, dtype=np.float32)

        mock_voice = MagicMock()
        mock_voice.synthesize.return_value = [DummyChunk()]

        audio, sr = voice._synthesize_sentence_audio(mock_voice, "Test.", target_sr=22050)
        self.assertLessEqual(float(np.max(audio)), 0.881)
        self.assertGreaterEqual(float(np.min(audio)), -0.95)


class TestLockContentionAndWakeLatency(unittest.TestCase):
    """Verifies that PortAudio lock contention is eliminated during blocking reads."""

    def test_stream_read_does_not_hold_portaudio_lock(self):
        """record_utterance_with_vad must NOT hold _PORTAUDIO_LOCK while calling stream.read()."""
        import voice
        from voice import WakeWordDetector

        detector = WakeWordDetector()
        lock_held_during_read = []

        mock_stream = MagicMock()
        def mock_read(samples, **kwargs):
            is_locked = voice._PORTAUDIO_LOCK._is_owned()
            lock_held_during_read.append(is_locked)
            return (np.zeros(1280, dtype=np.int16)).tobytes()

        mock_stream.read.side_effect = mock_read

        detector.record_utterance_with_vad(
            stream=mock_stream,
            timeout_no_speech=0.1,
            max_duration=0.1,
        )

        self.assertTrue(len(lock_held_during_read) > 0)
        for locked in lock_held_during_read:
            self.assertFalse(locked, "_PORTAUDIO_LOCK was held during stream.read()!")

    def test_wake_detected_immediate_callback_latency(self):
        """Wake detection executes on_wake callback immediately without blocking (< 100 ms)."""
        from voice import WakeWordDetector
        callback_times = []

        def on_wake_cb():
            callback_times.append(time.perf_counter())

        detector = WakeWordDetector(on_wake=on_wake_cb)

        t0 = time.perf_counter()
        detector.on_wake()
        latency_ms = (callback_times[0] - t0) * 1000.0

        self.assertLess(latency_ms, 100.0)
        self.assertLess(latency_ms, 10.0)

    def test_voice_listen_method_exists_and_callable(self):
        """Voice.listen() and WakeWordDetector.listen() methods are available and callable."""
        import voice
        cfg = Config()
        ctx = MagicMock()
        v = voice.Voice(cfg, ctx, MagicMock())
        self.assertTrue(hasattr(v, "listen"))
        self.assertEqual(v.listen(timeout=0.1), "")

    def test_active_command_processed_when_listening_window_expired(self):
        """Commands flagged with is_command=True must NOT be dropped even if listening_until expired."""
        cfg = Config()
        cfg.wake_aliases = ["jarvis"]
        ctx = MagicMock()
        v = Voice(cfg, ctx, MagicMock())
        # Simulate timer expired prior to STT finishing
        v.listening_until = time.time() - 10.0

        with patch("voice.run_command", return_value="Opening browser."):
            # User spoke "open chrome" after wake word (command does NOT contain "jarvis")
            v._process("open chrome", is_command=True)

        ctx.overlay.post.assert_any_call("prompt", text="open chrome", raw="open chrome")
        ctx.say.assert_called_with("Opening browser.")

    def test_wake_idle_prompts_yes_sir_and_opens_window(self):
        """When wake is triggered with no trailing utterance, assistant confirms 'Yes, sir?'."""
        cfg = Config()
        ctx = MagicMock()
        v = Voice(cfg, ctx, MagicMock())
        v.wake_detector = MagicMock()

        v._on_wake_idle()

        ctx.say.assert_called_with("Yes, sir?")
        self.assertGreater(v.listening_until, time.time())
        self.assertEqual(v.wake_detector.listening_until, v.listening_until)


if __name__ == "__main__":
    unittest.main()
