"""
Unit tests for J.A.R.V.I.S. Context Memory & Disambiguation Logic.

Strictly NO Qt imports. 100% unit-testable with pure Python and mocks.

Verifies:
- In-memory turn storage (retains last ~5 turns, zero disk writes).
- Referential / pronoun resolution ("open it", "louder", "quieter").
- Action repetition ("do that again").
- Secondary candidate switching ("no, the other one").
- Disambiguation Engine session management and overlay notification.
- Numbered voice selection ("one", "two", "three", "first", "second", etc.).
- Candidate label matching and UI click / dwell callbacks.
- Full integration with commands.run_command.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from context_resolver import (
    Candidate,
    ContextResolver,
    DisambiguationEngine,
    TurnContext,
    extract_entities_from_text,
    get_context_resolver,
    get_disambiguation_engine,
    reset_context,
)
from commands import run_command


class TestContextResolverMemory(unittest.TestCase):
    """Verifies volatile in-memory storage bounded to ~5 turns."""

    def setUp(self):
        self.resolver = ContextResolver(max_turns=5)

    def test_retains_last_5_turns_strictly_in_ram(self):
        for i in range(1, 8):
            self.resolver.record_turn(
                user_utterance=f"utterance {i}",
                resolved_action=f"action_{i}",
                arguments={"step": i},
            )

        history = self.resolver.get_history()
        self.assertEqual(len(history), 5)
        # Oldest turns 1 and 2 should have been evicted; 3 through 7 remain
        self.assertEqual(history[0].user_utterance, "utterance 3")
        self.assertEqual(history[-1].user_utterance, "utterance 7")

    def test_entity_auto_extraction(self):
        turn1 = self.resolver.record_turn("open chrome please")
        self.assertEqual(turn1.entities.get("app"), "chrome")

        turn2 = self.resolver.record_turn("go to youtube")
        self.assertEqual(turn2.entities.get("site"), "youtube")

        turn3 = self.resolver.record_turn("open downloads folder")
        self.assertEqual(turn3.entities.get("folder"), "downloads")

        turn4 = self.resolver.record_turn("open report.pdf")
        self.assertEqual(turn4.entities.get("file"), "report.pdf")


class TestPronounReferentialResolution(unittest.TestCase):
    """Verifies 'open it', 'make that louder', 'quieter', etc."""

    def setUp(self):
        self.resolver = ContextResolver(max_turns=5)

    def test_open_it_resolves_last_app(self):
        self.resolver.record_turn(
            user_utterance="open notepad",
            resolved_action="open_app",
            arguments={"app": "notepad"},
        )
        res = self.resolver.resolve_referential("open it")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_app")
        self.assertEqual(res.args.get("app"), "notepad")

    def test_open_it_resolves_last_site(self):
        self.resolver.record_turn(
            user_utterance="bring up github",
            resolved_action="open_site",
            arguments={"site": "github"},
        )
        res = self.resolver.resolve_referential("open it")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_site")
        self.assertEqual(res.args.get("site"), "github")

    def test_open_it_resolves_last_folder(self):
        self.resolver.record_turn(
            user_utterance="show downloads folder",
            resolved_action="open_folder",
            arguments={"folder": "downloads"},
        )
        res = self.resolver.resolve_referential("open that")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_folder")
        self.assertEqual(res.args.get("folder"), "downloads")

    def test_open_it_resolves_last_file(self):
        self.resolver.record_turn(
            user_utterance="inspect summary.pdf",
            entities={"file": "summary.pdf"},
        )
        res = self.resolver.resolve_referential("launch it")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_file")
        self.assertEqual(res.args.get("file"), "summary.pdf")

    def test_open_it_without_prior_entity_returns_none(self):
        self.resolver.record_turn("what time is it", resolved_action="time")
        res = self.resolver.resolve_referential("open it")
        self.assertIsNone(res)

    def test_volume_referential_louder(self):
        phrases = ["make that louder", "louder", "turn it up", "turn that up", "a bit louder", "crank it up"]
        for p in phrases:
            res = self.resolver.resolve_referential(p)
            self.assertIsNotNone(res, f"Failed for phrase: '{p}'")
            self.assertEqual(res.action, "volume_up")
            self.assertEqual(res.args.get("steps"), 5)

    def test_volume_referential_quieter(self):
        phrases = ["make that quieter", "quieter", "turn it down", "turn that down", "lower it", "a bit quieter"]
        for p in phrases:
            res = self.resolver.resolve_referential(p)
            self.assertIsNotNone(res, f"Failed for phrase: '{p}'")
            self.assertEqual(res.action, "volume_down")
            self.assertEqual(res.args.get("steps"), 5)


class TestRepeatAction(unittest.TestCase):
    """Verifies 'do that again' / 'repeat that'."""

    def setUp(self):
        self.resolver = ContextResolver(max_turns=5)

    def test_repeat_previous_action(self):
        self.resolver.record_turn(
            user_utterance="take a screenshot",
            resolved_action="screenshot",
            arguments={},
        )
        res = self.resolver.resolve_referential("do that again")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "screenshot")
        self.assertEqual(res.args, {})

    def test_repeat_with_arguments(self):
        self.resolver.record_turn(
            user_utterance="play drake on spotify",
            resolved_action="play_music",
            arguments={"query": "drake", "platform": "spotify"},
        )
        for phrase in ("do that again", "repeat that", "once more", "again", "one more time"):
            res = self.resolver.resolve_referential(phrase)
            self.assertIsNotNone(res, f"Failed for phrase: '{phrase}'")
            self.assertEqual(res.action, "play_music")
            self.assertEqual(res.args.get("query"), "drake")
            self.assertEqual(res.args.get("platform"), "spotify")

    def test_repeat_when_empty_history_returns_none(self):
        res = self.resolver.resolve_referential("do that again")
        self.assertIsNone(res)


class TestAlternativeCandidate(unittest.TestCase):
    """Verifies 'no, the other one' switching to secondary candidate."""

    def setUp(self):
        self.resolver = ContextResolver(max_turns=5)

    def test_switch_to_secondary_candidate(self):
        c1 = Candidate(index=1, action="open_app", args={"app": "chrome"}, label="Open Chrome")
        c2 = Candidate(index=2, action="open_site", args={"site": "google"}, label="Open Google")

        self.resolver.record_turn(
            user_utterance="open chrome or google",
            resolved_action="open_app",
            arguments={"app": "chrome"},
            candidates=[c1, c2],
        )

        for phrase in ("no, the other one", "the other one", "try the other one", "the other option", "the second one"):
            res = self.resolver.resolve_referential(phrase)
            self.assertIsNotNone(res, f"Failed for phrase: '{phrase}'")
            self.assertEqual(res.action, "open_site")
            self.assertEqual(res.args.get("site"), "google")
            self.assertEqual(res.label, "Open Google")

    def test_switch_to_primary_if_secondary_was_executed(self):
        c1 = Candidate(index=1, action="open_app", args={"app": "chrome"}, label="Open Chrome")
        c2 = Candidate(index=2, action="open_site", args={"site": "google"}, label="Open Google")

        self.resolver.record_turn(
            user_utterance="open chrome or google",
            resolved_action="open_site",
            arguments={"site": "google"},
            candidates=[c1, c2],
        )

        res = self.resolver.resolve_referential("no, the other one")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_app")
        self.assertEqual(res.args.get("app"), "chrome")


class TestDisambiguationEngine(unittest.TestCase):
    """Verifies disambiguation state management, overlay notification, and selection resolution."""

    def setUp(self):
        self.engine = DisambiguationEngine(timeout=30.0)
        self.mock_overlay = MagicMock()

    def test_start_session_notifies_overlay(self):
        candidates = [
            Candidate(index=1, action="volume_up", args={"steps": 5}, label="Volume Up", confidence=0.42),
            Candidate(index=2, action="volume_down", args={"steps": 5}, label="Volume Down", confidence=0.38),
        ]
        session = self.engine.start_session(candidates, overlay=self.mock_overlay)
        self.assertTrue(self.engine.is_active())
        self.assertEqual(len(session.candidates), 2)

        # Verify overlay.post was called with "disambiguate" and serialized candidates
        self.mock_overlay.post.assert_called_once()
        call_args, call_kwargs = self.mock_overlay.post.call_args
        self.assertEqual(call_args[0], "disambiguate")
        self.assertIn("candidates", call_kwargs)
        self.assertEqual(len(call_kwargs["candidates"]), 2)
        self.assertEqual(call_kwargs["candidates"][0]["action"], "volume_up")

    def test_resolve_voice_number_one(self):
        c1 = Candidate(index=1, action="open_app", args={"app": "chrome"}, label="Open Chrome")
        c2 = Candidate(index=2, action="open_site", args={"site": "google"}, label="Open Google")

        for phrase in ("one", "first", "number one", "option one", "the first one", "1", "#1"):
            self.engine.start_session([c1, c2], overlay=self.mock_overlay)
            res = self.engine.resolve_selection(phrase)
            self.assertIsNotNone(res, f"Failed for phrase: '{phrase}'")
            self.assertEqual(res.index, 1)
            self.assertEqual(res.action, "open_app")
            self.assertFalse(self.engine.is_active())

    def test_resolve_voice_number_two(self):
        c1 = Candidate(index=1, action="open_app", args={"app": "chrome"}, label="Open Chrome")
        c2 = Candidate(index=2, action="open_site", args={"site": "google"}, label="Open Google")

        for phrase in ("two", "second", "number two", "option two", "the second one", "2", "#2", "the other one"):
            self.engine.start_session([c1, c2], overlay=self.mock_overlay)
            res = self.engine.resolve_selection(phrase)
            self.assertIsNotNone(res, f"Failed for phrase: '{phrase}'")
            self.assertEqual(res.index, 2)
            self.assertEqual(res.action, "open_site")
            self.assertFalse(self.engine.is_active())

    def test_resolve_voice_number_three(self):
        c1 = Candidate(index=1, action="volume_up", args={"steps": 5}, label="Volume Up")
        c2 = Candidate(index=2, action="volume_down", args={"steps": 5}, label="Volume Down")
        c3 = Candidate(index=3, action="mute", args={}, label="Mute Audio")

        for phrase in ("three", "third", "number three", "option three", "3", "#3"):
            self.engine.start_session([c1, c2, c3], overlay=self.mock_overlay)
            res = self.engine.resolve_selection(phrase)
            self.assertIsNotNone(res, f"Failed for phrase: '{phrase}'")
            self.assertEqual(res.index, 3)
            self.assertEqual(res.action, "mute")

    def test_resolve_spoken_candidate_label(self):
        c1 = Candidate(index=1, action="open_app", args={"app": "chrome"}, label="Open Chrome")
        c2 = Candidate(index=2, action="open_site", args={"site": "google"}, label="Open Google")
        self.engine.start_session([c1, c2], overlay=self.mock_overlay)

        res = self.engine.resolve_selection("chrome")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_app")

        self.engine.start_session([c1, c2], overlay=self.mock_overlay)
        res = self.engine.resolve_selection("google")
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_site")

    def test_resolve_ui_click_or_dwell_callback(self):
        c1 = Candidate(index=1, action="open_app", args={"app": "chrome"}, label="Open Chrome")
        c2 = Candidate(index=2, action="open_site", args={"site": "google"}, label="Open Google")
        self.engine.start_session([c1, c2], overlay=self.mock_overlay)

        # Click option 2
        res = self.engine.resolve_click(2)
        self.assertIsNotNone(res)
        self.assertEqual(res.action, "open_site")
        self.assertFalse(self.engine.is_active())

    def test_cancel_disambiguation_session(self):
        c1 = Candidate(index=1, action="volume_up", args={"steps": 5}, label="Volume Up")
        c2 = Candidate(index=2, action="volume_down", args={"steps": 5}, label="Volume Down")
        self.engine.start_session([c1, c2], overlay=self.mock_overlay)

        res = self.engine.resolve_selection("never mind")
        self.assertIsNone(res)
        self.assertFalse(self.engine.is_active())


class TestContextAndDisambiguationIntegration(unittest.TestCase):
    """Verifies end-to-end integration with commands.run_command."""

    def setUp(self):
        reset_context()
        self.mock_ctx = MagicMock()
        self.mock_ctx.overlay = MagicMock()
        self.mock_ctx.engine = MagicMock()
        self.mock_ctx.engine.cfg = MagicMock()
        self.mock_ctx.engine.cfg.local_decision_engine = True

    def tearDown(self):
        reset_context()

    @patch("webbrowser.open")
    def test_end_to_end_pronoun_resolution(self, mock_web):
        # 1. Turn 1: user opens github
        reply1 = run_command("open github", self.mock_ctx)
        self.assertIn("Opening github", reply1)
        mock_web.assert_called_with("https://github.com")

        # 2. Turn 2: user says "open it"
        reply2 = run_command("open it", self.mock_ctx)
        self.assertIn("Opening github", reply2)

    @patch("pyautogui.screenshot")
    def test_end_to_end_repeat_action(self, mock_shot):
        # 1. Turn 1: take a screenshot
        reply1 = run_command("take a screenshot", self.mock_ctx)
        self.assertIn("Screenshot", reply1)

        # 2. Turn 2: do that again
        reply2 = run_command("do that again", self.mock_ctx)
        self.assertIn("Repeating screenshot", reply2)

    @patch("webbrowser.open")
    def test_end_to_end_disambiguation_dialogue(self, mock_web):
        # 1. User gives ambiguous input mentioning two sites
        reply1 = run_command("open youtube or google", self.mock_ctx)
        self.assertIn("Did you mean", reply1)
        self.assertTrue(get_disambiguation_engine().is_active())

        # 2. User selects "number two" (Google)
        reply2 = run_command("number two", self.mock_ctx)
        self.assertIn("Opening google", reply2)
        mock_web.assert_called_with("https://www.google.com")
        self.assertFalse(get_disambiguation_engine().is_active())

    @patch("webbrowser.open")
    def test_end_to_end_alternative_candidate(self, mock_web):
        # Turn 1: disambiguation prompt
        run_command("open youtube or google", self.mock_ctx)
        # Turn 2: user chooses "one" (YouTube)
        run_command("one", self.mock_ctx)
        mock_web.assert_called_with("https://www.youtube.com")

        # Turn 3: user changes mind: "no, the other one"
        reply3 = run_command("no, the other one", self.mock_ctx)
        self.assertIn("Switching to Open Google", reply3)
        mock_web.assert_called_with("https://www.google.com")


if __name__ == "__main__":
    unittest.main()
