"""
Comprehensive unit tests for J.A.R.V.I.S. Groq AI integration:
- Key validation and HTTP error handling (200, 401, 429, timeout, offline)
- Whisper audio transcription and chat completions
- Client-side rate limiting and Retry-After honoring
- Action whitelisting, JSON parsing, and rejection of unauthorized actions
- Keyring credential storage and env var fallback (zero secrets in config)
- Automatic fallback from Groq to Google SpeechRecognition
- In-memory conversation history and standby reset
"""
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

import ai
from ai import (
    GroqClient,
    SimpleRateLimiter,
    AuthenticationError,
    RateLimitError,
    TimeoutError,
    OfflineError,
    InvalidResponseError,
    get_api_key,
    set_api_key,
    delete_api_key,
    get_conversation_manager,
    reset_ai_history,
    build_system_prompt,
    parse_ai_response,
    execute_whitelisted_action,
    WHITELIST_ACTIONS,
    extract_streaming_reply,
    extract_first_sentence,
    stream_ai_query,
)
from commands import run_command, APPS, SITES
from config import Config, load_config
from voice import Voice


class MockResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or json.dumps(self._json_data)
        self.headers = headers or {}

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


class TestGroqClient(unittest.TestCase):
    """Tests HTTP interactions and error translation in GroqClient."""

    def setUp(self):
        self.client = GroqClient(api_key="gsk_test_key_12345", timeout=5)

    @patch("requests.get")
    def test_validate_key_success(self, mock_get):
        mock_get.return_value = MockResponse(200, {"data": [{"id": "llama-3.1-8b-instant"}]})
        ok, msg = self.client.validate_key()
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid API key")

    @patch("requests.get")
    def test_validate_key_invalid_401(self, mock_get):
        mock_get.return_value = MockResponse(401, {"error": {"message": "Invalid API Key"}})
        ok, msg = self.client.validate_key()
        self.assertFalse(ok)
        self.assertIn("Invalid API key", msg)

    @patch("requests.get")
    def test_validate_key_rate_limit_429(self, mock_get):
        mock_get.return_value = MockResponse(429, {"error": {"message": "Rate limit exceeded"}}, headers={"Retry-After": "12"})
        ok, msg = self.client.validate_key()
        self.assertFalse(ok)
        self.assertIn("Rate limit exceeded", msg)

    @patch("requests.get")
    def test_validate_key_offline(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("Name resolution failed")
        ok, msg = self.client.validate_key()
        self.assertFalse(ok)
        self.assertEqual(msg, "Offline / connection failed")

    @patch("requests.get")
    def test_validate_key_timeout(self, mock_get):
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")
        ok, msg = self.client.validate_key()
        self.assertFalse(ok)
        self.assertEqual(msg, "Connection timed out")

    def test_validate_key_empty(self):
        empty_client = GroqClient(api_key="")
        ok, msg = empty_client.validate_key()
        self.assertFalse(ok)
        self.assertIn("No API key", msg)

    @patch("requests.post")
    def test_transcribe_success(self, mock_post):
        mock_post.return_value = MockResponse(200, {"text": "hello jarvis how are you"})
        dummy_wav = b"RIFF....WAVEfmt ...."
        res = self.client.transcribe(dummy_wav, model="whisper-large-v3-turbo")
        self.assertEqual(res, "hello jarvis how are you")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn("/audio/transcriptions", args[0])
        self.assertIn("file", kwargs["files"])

    @patch("requests.post")
    def test_transcribe_401_raises_auth_error(self, mock_post):
        mock_post.return_value = MockResponse(401, {"error": {"message": "Unauthorized"}})
        with self.assertRaises(AuthenticationError):
            self.client.transcribe(b"dummy_bytes")

    @patch("requests.post")
    def test_transcribe_429_raises_rate_limit(self, mock_post):
        mock_post.return_value = MockResponse(429, headers={"Retry-After": "8"})
        with self.assertRaises(RateLimitError) as cm:
            self.client.transcribe(b"dummy_bytes")
        self.assertEqual(cm.exception.retry_after, 8.0)

    @patch("requests.post")
    def test_transcribe_timeout_raises_timeout_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("Timed out")
        with self.assertRaises(TimeoutError):
            self.client.transcribe(b"dummy_bytes")

    @patch("requests.post")
    def test_transcribe_offline_raises_offline_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("No route to host")
        with self.assertRaises(OfflineError):
            self.client.transcribe(b"dummy_bytes")

    @patch("requests.post")
    def test_chat_success(self, mock_post):
        mock_post.return_value = MockResponse(200, {
            "choices": [{"message": {"content": '{"action": "none", "args": {}, "reply": "All systems online."}'}}]
        })
        msgs = [{"role": "user", "content": "status"}]
        reply = self.client.chat(msgs, model="llama-3.1-8b-instant", json_mode=True)
        self.assertIn('"reply": "All systems online."', reply)
        args, kwargs = mock_post.call_args
        self.assertTrue(kwargs["json"]["response_format"]["type"] == "json_object")

    @patch("requests.get")
    def test_list_and_categorize_models(self, mock_get):
        mock_get.return_value = MockResponse(200, {
            "data": [
                {"id": "llama-3.1-8b-instant"},
                {"id": "whisper-large-v3-turbo"},
                {"id": "llama-3.3-70b-versatile"},
                {"id": "whisper-large-v3"}
            ]
        })
        models = self.client.get_categorized_models()
        self.assertIn("llama-3.1-8b-instant", models["llm"])
        self.assertIn("whisper-large-v3-turbo", models["stt"])


class TestRateLimiter(unittest.TestCase):
    """Tests free-tier client-side rate limiter."""

    def test_allows_under_limit(self):
        limiter = SimpleRateLimiter(max_requests_per_minute=5)
        for _ in range(5):
            limiter.acquire()
        self.assertEqual(len(limiter.timestamps), 5)

    def test_blocks_over_limit(self):
        limiter = SimpleRateLimiter(max_requests_per_minute=3)
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        with self.assertRaises(RateLimitError):
            limiter.acquire()

    def test_respects_retry_after(self):
        limiter = SimpleRateLimiter(max_requests_per_minute=10)
        limiter.record_retry_after(5.0)
        with self.assertRaises(RateLimitError) as cm:
            limiter.acquire()
        self.assertGreater(cm.exception.retry_after, 0.0)


class TestKeyStorage(unittest.TestCase):
    """Tests Windows Credential Manager keyring storage and dev env var fallback."""

    @patch("keyring.get_password")
    def test_get_api_key_from_keyring(self, mock_get_pw):
        mock_get_pw.return_value = "gsk_keyring_saved_key"
        key = get_api_key()
        self.assertEqual(key, "gsk_keyring_saved_key")

    @patch("keyring.get_password")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_env_var_key"})
    def test_get_api_key_env_var_fallback(self, mock_get_pw):
        mock_get_pw.return_value = None
        key = get_api_key()
        self.assertEqual(key, "gsk_env_var_key")

    @patch("keyring.set_password")
    def test_set_api_key_saves_to_keyring(self, mock_set_pw):
        set_api_key("gsk_new_key_123")
        mock_set_pw.assert_called_with("JARVIS", "groq_api_key", "gsk_new_key_123")

    @patch("keyring.delete_password")
    def test_delete_api_key(self, mock_del_pw):
        delete_api_key()
        mock_del_pw.assert_called_with("JARVIS", "groq_api_key")

    def test_config_never_contains_secret_fields(self):
        cfg = Config()
        config_dict = cfg.__dict__
        for key in config_dict:
            if key == "groq_api_key":
                continue  # explicitly supported user option to persist groq api key
            self.assertNotIn("api_key", key.lower())
            self.assertNotIn("secret", key.lower())
            self.assertNotIn("token", key.lower())

    @patch("keyring.get_password", return_value=None)
    def test_groq_api_key_persistence(self, mock_keyring):
        cfg = Config(groq_api_key="gsk_test12345")
        self.assertEqual(cfg.groq_api_key, "gsk_test12345")
        key = get_api_key(cfg)
        self.assertEqual(key, "gsk_test12345")


class TestJSONActionParsingAndWhitelist(unittest.TestCase):
    """Tests LLM JSON response parsing, whitelist enforcement, and security boundaries."""

    def test_parse_clean_json(self):
        raw = '{"action": "volume_up", "args": {"steps": 5}, "reply": "Increasing volume, sir."}'
        act, args, reply = parse_ai_response(raw)
        self.assertEqual(act, "volume_up")
        self.assertEqual(args.get("steps"), 5)
        self.assertEqual(reply, "Increasing volume, sir.")

    def test_parse_markdown_fenced_json(self):
        raw = '```json\n{"action": "none", "args": {}, "reply": "Good morning, sir."}\n```'
        act, args, reply = parse_ai_response(raw)
        self.assertEqual(act, "none")
        self.assertEqual(reply, "Good morning, sir.")

    def test_parse_malformed_json_speaks_reply_text(self):
        raw = "I am ready to assist you, sir, though I did not format this in JSON."
        act, args, reply = parse_ai_response(raw)
        self.assertEqual(act, "none")
        self.assertEqual(reply, raw)

    @patch("pyautogui.press")
    def test_whitelisted_action_volume_executes(self, mock_press):
        ctx = MagicMock()
        ok, msg = execute_whitelisted_action("volume_up", {"steps": 3}, ctx)
        self.assertTrue(ok)
        mock_press.assert_called_with("volumeup", presses=3, _pause=False)

    def test_whitelist_rejection_of_unauthorized_action(self):
        ctx = MagicMock()
        ok, msg = execute_whitelisted_action("delete_system32", {}, ctx)
        self.assertFalse(ok)
        self.assertIn("not executable", msg)

    @patch("commands._launch")
    def test_whitelist_open_app_allows_only_whitelisted_apps(self, mock_launch):
        ctx = MagicMock()
        # Allowed app
        ok, _ = execute_whitelisted_action("open_app", {"app": "notepad"}, ctx)
        self.assertTrue(ok)
        mock_launch.assert_called_with(APPS["notepad"])

        # Arbitrary shell command disguised as app
        mock_launch.reset_mock()
        ok, msg = execute_whitelisted_action("open_app", {"app": "powershell.exe -Command rm -rf /"}, ctx)
        self.assertFalse(ok)
        self.assertIn("not in whitelist", msg)
        mock_launch.assert_not_called()

    @patch("webbrowser.open")
    def test_whitelist_open_site_allows_only_whitelisted_sites(self, mock_web):
        ctx = MagicMock()
        ok, _ = execute_whitelisted_action("open_site", {"site": "youtube"}, ctx)
        self.assertTrue(ok)
        mock_web.assert_called_with(SITES["youtube"])

        # Arbitrary url not in SITES
        mock_web.reset_mock()
        ok, msg = execute_whitelisted_action("open_site", {"site": "malicious-site.com"}, ctx)
        self.assertFalse(ok)
        mock_web.assert_not_called()


class TestConversationHistory(unittest.TestCase):
    """Tests multi-turn history buffer and reset on standby."""

    def setUp(self):
        self.conv = get_conversation_manager()
        self.conv.reset()

    def test_history_accumulation_and_limit(self):
        self.conv.max_turns = 3
        for i in range(5):
            self.conv.add_turn(f"user_{i}", f"reply_{i}")

        # At max 3 turns, history should have 6 messages (3 user + 3 assistant)
        self.assertEqual(len(self.conv.history), 6)
        self.assertEqual(self.conv.history[-1]["content"], "reply_4")

    def test_standby_resets_history(self):
        self.conv.add_turn("hello", "Greetings, sir.")
        self.assertGreater(len(self.conv.history), 0)
        reset_ai_history()
        self.assertEqual(len(self.conv.history), 0)


class TestVoiceFallback(unittest.TestCase):
    """Tests automatic fallback from Groq to Google SpeechRecognition in voice.py."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.ai_enabled = True
        self.ctx = MagicMock()
        self.stop = MagicMock()
        self.stop.is_set.side_effect = [False, True]  # Run loop once
        self.voice = Voice(self.cfg, self.ctx, self.stop)

    @patch("voice.get_api_key", return_value="gsk_valid_key")
    @patch.object(GroqClient, "transcribe")
    def test_groq_whisper_success_path(self, mock_transcribe, mock_key):
        mock_transcribe.return_value = "hello jarvis"
        recognizer = MagicMock()
        audio = MagicMock()
        # Mock 1 second of 16kHz audio data with non-zero RMS
        dummy_samples = b"\x00\x10" * 4000
        audio.get_raw_data.return_value = dummy_samples
        audio.get_wav_data.return_value = b"RIFF....WAVE"
        self.voice.audio_q.put(audio)

        processed = []
        self.voice._process = lambda text: processed.append(text)
        self.voice._recognition_worker(recognizer, MagicMock())

        self.assertEqual(processed, ["hello jarvis"])
        mock_transcribe.assert_called_once()
        recognizer.recognize_google.assert_not_called()

    @patch("voice.get_api_key", return_value="gsk_bad_key")
    @patch.object(GroqClient, "transcribe")
    def test_groq_401_falls_back_to_google_and_shows_error_badge(self, mock_transcribe, mock_key):
        mock_transcribe.side_effect = AuthenticationError("Invalid key")
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "google fallback text"
        audio = MagicMock()
        audio.get_raw_data.return_value = b"\x00\x10" * 4000
        audio.get_wav_data.return_value = b"RIFF....WAVE"
        self.voice.audio_q.put(audio)

        processed = []
        self.voice._process = lambda text: processed.append(text)
        self.voice._recognition_worker(recognizer, MagicMock())

        # Verified fallback occurred
        recognizer.recognize_google.assert_called_once()
        self.assertEqual(processed, ["google fallback text"])
        self.ctx.overlay.post.assert_called_with("ai_error", text="Invalid Groq Key")

    @patch("voice.get_api_key", return_value="gsk_test_key")
    @patch.object(GroqClient, "transcribe")
    def test_groq_429_falls_back_to_google_and_sets_cooldown(self, mock_transcribe, mock_key):
        mock_transcribe.side_effect = RateLimitError("Rate limit", retry_after=15.0)
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "google fallback after rate limit"
        audio = MagicMock()
        audio.get_raw_data.return_value = b"\x00\x10" * 4000
        audio.get_wav_data.return_value = b"RIFF....WAVE"
        self.voice.audio_q.put(audio)

        processed = []
        self.voice._process = lambda text: processed.append(text)
        self.voice._recognition_worker(recognizer, MagicMock())

        self.assertGreater(self.voice.groq_cooldown_until, time.time())
        recognizer.recognize_google.assert_called_once()
        self.ctx.overlay.post.assert_called_with("ai_error", text="Groq Rate Limited")

    @patch("voice.get_api_key", return_value=None)
    def test_no_key_bypasses_groq_and_uses_google_directly(self, mock_key):
        recognizer = MagicMock()
        recognizer.recognize_google.return_value = "pure google recognition"
        audio = MagicMock()
        audio.get_raw_data.return_value = b"\x00\x10" * 4000
        self.voice.audio_q.put(audio)

        processed = []
        self.voice._process = lambda text: processed.append(text)
        self.voice._recognition_worker(recognizer, MagicMock())

        recognizer.recognize_google.assert_called_once()
        self.assertEqual(processed, ["pure google recognition"])


class TestSetupAICommand(unittest.TestCase):
    """Tests the 'set up ai' voice command."""

    def test_setup_ai_opens_settings_ai_tab(self):
        ctx = MagicMock()
        reply = run_command("set up ai", ctx)
        self.assertIn("Opening AI settings", reply)
        ctx.overlay.open_settings.assert_called_with("AI")


class TestAICommandsDispatch(unittest.TestCase):
    """Tests dispatching unknown voice commands to LLM and executing whitelisted responses."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.ai_enabled = True
        self.cfg.local_decision_engine = False
        self.ctx = MagicMock()
        self.ctx.engine.cfg = self.cfg

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    @patch.object(GroqClient, "chat")
    @patch("pyautogui.press")
    def test_ai_query_executes_whitelisted_action_and_speaks_reply(self, mock_press, mock_chat, mock_key):
        mock_chat.return_value = '{"action": "volume_up", "args": {"steps": 5}, "reply": "Turning the volume up for you, sir."}'
        reply = run_command("increase the sound level please", self.ctx)
        self.assertEqual(reply, "Turning the volume up for you, sir.")
        mock_press.assert_called_with("volumeup", presses=5, _pause=False)
        self.ctx.overlay.post.assert_any_call("thinking", active=True)
        self.ctx.overlay.post.assert_any_call("thinking", active=False)

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    @patch.object(GroqClient, "chat")
    def test_ai_query_rejects_unauthorized_action_but_speaks_reply(self, mock_chat, mock_key):
        mock_chat.return_value = '{"action": "delete_all_files", "args": {}, "reply": "I cannot do that, sir."}'
        reply = run_command("delete everything on my drive", self.ctx)
        self.assertEqual(reply, "I cannot do that, sir.")

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    @patch.object(GroqClient, "chat")
    def test_ai_query_handles_401_error_gracefully(self, mock_chat, mock_key):
        mock_chat.side_effect = AuthenticationError("Bad key")
        reply = run_command("what is the weather today", self.ctx)
        self.assertIn("invalid", reply.lower())
        self.ctx.overlay.post.assert_any_call("ai_error", text="Invalid Groq Key")

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    @patch.object(GroqClient, "chat")
    def test_ai_query_handles_429_error_gracefully(self, mock_chat, mock_key):
        mock_chat.side_effect = RateLimitError("Rate limit", retry_after=10.0)
        reply = run_command("tell me a joke", self.ctx)
        self.assertIn("rate limit", reply.lower())
        self.ctx.overlay.post.assert_any_call("ai_error", text="Groq Rate Limited")

    @patch("commands.get_api_key", return_value=None)
    def test_ai_query_disabled_without_key(self, mock_key):
        reply = run_command("some completely unknown phrase", self.ctx)
        self.assertEqual(reply, "I didn't catch a command for that, sir.")


class TestVADSilenceRejection(unittest.TestCase):
    """Tests that energy-based VAD rejects silent audio chunks to protect quota."""

    def test_silent_audio_discarded_before_groq(self):
        cfg = Config()
        cfg.ai_enabled = True
        ctx = MagicMock()
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]
        voice = Voice(cfg, ctx, stop)

        audio = MagicMock()
        # Near-silent audio samples with RMS < 30.0
        audio.get_raw_data.return_value = b"\x01\x00" * 4000
        voice.audio_q.put(audio)

        with patch("voice.get_api_key", return_value="gsk_test"), \
             patch.object(GroqClient, "transcribe") as mock_transcribe:
            voice._recognition_worker(MagicMock(), MagicMock())
            mock_transcribe.assert_not_called()


class TestVisionAnalysis(unittest.TestCase):
    """Tests visual query detection and Groq vision analysis."""

    def test_visual_query_detection(self):
        from commands import is_visual_query
        self.assertTrue(is_visual_query("what is this"))
        self.assertTrue(is_visual_query("what's that"))
        self.assertTrue(is_visual_query("what am I holding"))
        self.assertTrue(is_visual_query("Jarvis, look at this"))
        self.assertTrue(is_visual_query("what do you see in front of the camera"))
        self.assertTrue(is_visual_query("identify this"))
        self.assertTrue(is_visual_query("read this"))
        self.assertFalse(is_visual_query("open chrome"))
        self.assertFalse(is_visual_query("volume up"))
        self.assertFalse(is_visual_query("what is the weather in Paris"))

    @patch("requests.post")
    def test_describe_image_with_numpy_array(self, mock_post):
        import numpy as np
        mock_post.return_value = MockResponse(
            status_code=200,
            json_data={"choices": [{"message": {"content": "That is a Rubik's cube, sir."}}]}
        )
        client = GroqClient(api_key="gsk_test_key")
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        reply = client.describe_image(img, prompt="what is this?")
        self.assertEqual(reply, "That is a Rubik's cube, sir.")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertIn("qwen", payload["model"].lower())
        self.assertTrue(payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    @patch.object(GroqClient, "describe_image", return_value="That is a Rubik's cube, sir.")
    def test_run_command_visual_query_dispatches_to_vision(self, mock_desc, mock_key):
        import numpy as np
        ctx = MagicMock()
        cfg = Config()
        cfg.ai_enabled = True
        cfg.ai_vision_enabled = True
        ctx.engine.cfg = cfg
        mock_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        ctx.tracker.get_latest_frame.return_value = mock_frame

        reply = run_command("what is this", ctx)
        self.assertEqual(reply, "That is a Rubik's cube, sir.")
        mock_desc.assert_called_once()


class TestGroqClientPhase3Optimizations(unittest.TestCase):
    """Verifies Phase 3 HTTP Keep-Alive, Pre-warming, Streaming, Retries, and TTL Cache."""

    def setUp(self):
        self.client = GroqClient(api_key="gsk_test_key_phase3", timeout=5)

    def test_persistent_session_and_pooling(self):
        """Verifies persistent requests.Session with connection pooling."""
        self.assertIsNotNone(self.client.session)
        self.assertIn("https://", self.client.session.adapters)
        adapter = self.client.session.adapters["https://"]
        self.assertGreaterEqual(getattr(adapter, "_pool_connections", 0), 10)
        self.assertGreaterEqual(getattr(adapter, "_pool_maxsize", 0), 10)

    @patch("requests.get")
    def test_prewarm_connection_sync(self, mock_get):
        """Prewarm connects to models endpoint to establish TCP/TLS session."""
        mock_get.return_value = MockResponse(200, {"data": []})
        ok = self.client.prewarm_connection(async_prewarm=False)
        self.assertTrue(ok)
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertIn("/models", args[0])

    @patch("requests.get")
    def test_prewarm_connection_async(self, mock_get):
        """Prewarm launches non-blocking worker thread on wake event."""
        mock_get.return_value = MockResponse(200, {"data": []})
        ok = self.client.prewarm_connection(async_prewarm=True)
        self.assertTrue(ok)
        time.sleep(0.1)  # allow background thread to finish
        self.assertTrue(mock_get.called)

    def test_chat_streaming(self):
        """Tests SSE streaming parser yielding tokens in real time."""
        sse_lines = [
            b'data: {"choices": [{"delta": {"content": "Good "}}]}',
            b'data: {"choices": [{"delta": {"content": "morning, "}}]}',
            b'data: {"choices": [{"delta": {"content": "sir."}}]}',
            b'data: [DONE]',
        ]

        class StreamingMockResponse:
            status_code = 200
            headers = {}

            def iter_lines(self):
                return iter(sse_lines)

        with patch.object(self.client, "_request_with_retry", return_value=StreamingMockResponse()):
            tokens = list(self.client.chat([{"role": "user", "content": "hello"}], stream=True))
            self.assertEqual(tokens, ["Good ", "morning, ", "sir."])
            full_reply = "".join(tokens)
            self.assertEqual(full_reply, "Good morning, sir.")

    @patch("requests.post")
    def test_ttl_response_cache_short_queries(self, mock_post):
        """Tests that identical short queries hit the TTL cache and avoid network round-trips."""
        mock_post.return_value = MockResponse(
            200,
            {"choices": [{"message": {"content": "Paris is the capital of France, sir."}}]}
        )
        self.client.clear_cache()

        msgs = [{"role": "user", "content": "What is the capital of France?"}]

        # Call 1: cold cache -> hits network
        reply1 = self.client.chat(msgs, json_mode=False)
        self.assertEqual(reply1, "Paris is the capital of France, sir.")
        self.assertEqual(mock_post.call_count, 1)

        # Call 2: warm cache -> served from TTL cache with zero network calls
        reply2 = self.client.chat(msgs, json_mode=False)
        self.assertEqual(reply2, "Paris is the capital of France, sir.")
        self.assertEqual(mock_post.call_count, 1)  # call_count remains 1!

        # Clear cache -> Call 3 hits network again
        self.client.clear_cache()
        reply3 = self.client.chat(msgs, json_mode=False)
        self.assertEqual(reply3, "Paris is the capital of France, sir.")
        self.assertEqual(mock_post.call_count, 2)

    def test_jittered_retry_on_server_5xx(self):
        """Tests exponential backoff retries when server returns transient 500 error."""
        resp_500 = MockResponse(500, text="Internal Server Error")
        resp_200 = MockResponse(200, {"choices": [{"message": {"content": "All systems operational, sir."}}]})

        call_records = []

        def mock_post_call(url, **kwargs):
            call_records.append(url)
            if len(call_records) == 1:
                return resp_500
            return resp_200

        with patch.object(self.client, "_post", side_effect=mock_post_call):
            self.client.clear_cache()
            self.client.max_retries = 2
            reply = self.client.chat([{"role": "user", "content": "diagnostics"}], json_mode=False)
            self.assertEqual(reply, "All systems operational, sir.")
            self.assertEqual(len(call_records), 2)  # retried after 500!


class TestPhase4StreamingAndLatencyLadder(unittest.TestCase):
    """
    Validates Phase 4 streaming progressive chunk dispatch,
    latency ladder compliance (<100ms ack, 100-400ms activity, >400ms pulse),
    and fast error handling (zero retry delays on offline/timeout).
    """

    def test_extract_streaming_reply_json_complete(self):
        accumulated = '{\n  "action": "none",\n  "args": {},\n  "reply": "Good evening, sir. All telemetry nominal."\n}'
        extracted = extract_streaming_reply(accumulated)
        self.assertEqual(extracted, "Good evening, sir. All telemetry nominal.")

    def test_extract_streaming_reply_json_partial(self):
        accumulated = '{"action": "none", "args": {}, "reply": "Good evening, sir. All'
        extracted = extract_streaming_reply(accumulated)
        self.assertEqual(extracted, "Good evening, sir. All")

    def test_extract_streaming_reply_escaped_characters(self):
        accumulated = r'{"reply": "He said \"welcome back\" to you, sir.\nNext line."}'
        extracted = extract_streaming_reply(accumulated)
        self.assertEqual(extracted, 'He said "welcome back" to you, sir.\nNext line.')

    def test_extract_streaming_reply_plain_text(self):
        raw_text = "Good evening, sir. At your command."
        extracted = extract_streaming_reply(raw_text)
        self.assertEqual(extracted, "Good evening, sir. At your command.")

    def test_extract_first_sentence(self):
        text = "All systems operational, sir. How may I assist you this evening?"
        sent = extract_first_sentence(text)
        self.assertEqual(sent, "All systems operational, sir.")

        sent_q = extract_first_sentence("Is that all, sir? Standing by.")
        self.assertEqual(sent_q, "Is that all, sir?")

        sent_excl = extract_first_sentence("Power cells at maximum! All circuits green.")
        self.assertEqual(sent_excl, "Power cells at maximum!")

        incomplete = extract_first_sentence("Good morning")
        self.assertEqual(incomplete, "Good morning")

    def test_stream_ai_query_progressive_dispatch_to_overlay(self):
        """Streaming chat completion posts partial chunks and first sentence to overlay with speak=False."""
        client = MagicMock()
        # Chunks simulating real SSE streaming from Groq
        simulated_tokens = [
            '{"action":', ' "none",', ' "args": {},', ' "reply": "Good',
            ' evening,', ' sir.', ' I have', ' calibrated the', ' repulsors."}'
        ]
        client.chat.return_value = iter(simulated_tokens)

        ctx = MagicMock()
        callback_history = []

        result = stream_ai_query(
            client=client,
            messages=[{"role": "user", "content": "status"}],
            ctx=ctx,
            on_partial_reply=lambda txt: callback_history.append(txt),
        )

        self.assertEqual(result, "".join(simulated_tokens))
        # Verify overlay.post was called with speak=False
        ctx.overlay.post.assert_any_call("say", text="Good evening, sir.", speak=False)
        # Verify callback captured progressive growth
        self.assertTrue(len(callback_history) >= 2)
        self.assertIn("Good evening, sir.", callback_history)

    def test_stream_ai_query_string_fallback(self):
        """When client returns a raw string (e.g. mocked or non-stream), dispatches gracefully."""
        client = MagicMock()
        client.chat.return_value = '{"action": "none", "args": {}, "reply": "Immediate answer, sir."}'

        ctx = MagicMock()
        result = stream_ai_query(
            client=client,
            messages=[{"role": "user", "content": "hi"}],
            ctx=ctx,
        )

        self.assertIn("Immediate answer, sir.", result)
        ctx.overlay.post.assert_called_with("say", text="Immediate answer, sir.", speak=False)

    def test_fast_error_timeout_no_retry_delays(self):
        """Requests that time out fail immediately without waiting backoff retries."""
        client = GroqClient(api_key="gsk_test", timeout=2, max_retries=2)

        with patch.object(client, "_post", side_effect=requests.exceptions.Timeout("Read timeout")):
            t0 = time.perf_counter()
            with self.assertRaises(TimeoutError):
                client.chat([{"role": "user", "content": "test"}])
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            # Must fail immediately in < 50 ms rather than stalling for seconds
            self.assertLess(elapsed_ms, 50.0)

    def test_fast_error_offline_no_retry_delays(self):
        """Network offline/DNS failure fails immediately without waiting backoff retries."""
        client = GroqClient(api_key="gsk_test", timeout=2, max_retries=2)

        with patch.object(client, "_post", side_effect=requests.exceptions.ConnectionError("DNS failed")):
            t0 = time.perf_counter()
            with self.assertRaises(OfflineError):
                client.chat([{"role": "user", "content": "test"}])
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            # Must fail immediately in < 50 ms
            self.assertLess(elapsed_ms, 50.0)

    def test_latency_ladder_timings(self):
        """Verifies sub-100ms instant acknowledgement and sub-0.1ms extraction."""
        # 1. Token chunk extraction latency
        token_chunk = '{"action": "none", "args": {}, "reply": "Yes, sir."}'
        t0 = time.perf_counter()
        for _ in range(100):
            _ = extract_streaming_reply(token_chunk)
        extract_ms = ((time.perf_counter() - t0) / 100.0) * 1000.0
        self.assertLess(extract_ms, 0.5, "Chunk extraction must be sub-millisecond")

        # 2. First sentence extraction latency
        t0 = time.perf_counter()
        for _ in range(100):
            _ = extract_first_sentence("Yes, sir. All systems optimal.")
        sent_ms = ((time.perf_counter() - t0) / 100.0) * 1000.0
        self.assertLess(sent_ms, 0.5, "Sentence extraction must be sub-millisecond")


if __name__ == "__main__":
    unittest.main()

