"""
Benchmark and Parity Test Suite for System 1 Semantic Decision Engine.

Validates:
1. Benchmark corpus with > 1,000 realistic phrases covering all supported action categories,
   natural conversational speech, entity variations, and System 2 knowledge fallbacks.
2. 100% match parity between optimized decision engine (with LRU cache, precompiled regexes,
   exact prototype map, and S* >= 0.85 early exit) and unoptimized baseline algorithm.
3. Sub-1.0 ms latency proof (p50, p95, and p99 < 1.0 ms).
"""
import math
import re
import time
import unittest
from typing import Dict, List, Optional, Tuple

import numpy as np

import decision_engine
from decision_engine import INTENT_PROTOTYPES, SemanticDecisionEngine, get_decision_engine


def baseline_decide_unoptimized(
    engine: SemanticDecisionEngine,
    user_text: str,
    min_score: float = 0.25,
    min_margin: float = 0.03,
) -> Tuple[Optional[str], float]:
    """
    Unoptimized reference implementation of System 1 decision logic:
    - Re-compiles / uses raw regex for tokenization.
    - Zero caching (no LRU, no fast dict).
    - No exact prototype string match fast path.
    - No S* >= 0.85 early exit.
    - Full matrix dot-product + stable argsort over all action heads.
    """
    clean = re.sub(r"[^\w\s]", " ", user_text.lower()).strip()
    words = clean.split()
    tokens = list(words)
    for w in words:
        if len(w) >= 3:
            for i in range(len(w) - 2):
                tokens.append(f"#{w[i:i+3]}")

    vec = np.zeros(len(engine._vocab), dtype=np.float32)
    for t in tokens:
        if t in engine._vocab:
            vec[engine._vocab[t]] += 1.0
    vec *= engine._idf
    norm = np.linalg.norm(vec)
    if norm > 1e-6:
        vec = vec / norm

    if norm < 1e-6 or len(engine._exemplar_actions) == 0:
        return None, 0.0

    raw_sims = np.dot(engine._exemplar_matrix, vec)
    n_actions = len(engine._action_names)
    action_max_sims = np.zeros(n_actions, dtype=np.float32)
    np.maximum.at(action_max_sims, engine._exemplar_action_indices, raw_sims)

    order = np.argsort(-action_max_sims, kind="stable")
    top_idx = order[0]
    top_act = engine._action_names[top_idx]
    top_score = float(action_max_sims[top_idx])
    second_score = float(action_max_sims[order[1]]) if n_actions > 1 else 0.0
    margin = top_score - second_score

    if top_act == "conversation":
        return None, top_score

    if top_score >= min_score and margin >= min_margin:
        return top_act, top_score

    return None, top_score


def generate_benchmark_corpus() -> List[str]:
    """
    Constructs a diverse, realistic corpus of > 1,000 realistic user phrases:
    - Base prototype utterances from INTENT_PROTOTYPES
    - Natural phrasing variations (prefixes, suffixes, politeness markers)
    - Realistic command parameterizations (volume steps, apps, sites, folders, timers)
    - System 2 knowledge / conversational queries
    """
    phrases = []

    # 1. Base prototypes
    for act, plist in INTENT_PROTOTYPES.items():
        phrases.extend(plist)

    # 2. Conversational prefix/suffix permutations on prototypes
    prefixes = ["please ", "could you ", "jarvis ", "hey jarvis ", "can you please ", "kindly "]
    suffixes = [" please", " for me", " right now", " immediately", " now", " jarvis"]

    for act, plist in INTENT_PROTOTYPES.items():
        for p in plist:
            for pref in prefixes[:2]:
                phrases.append(f"{pref}{p}")
            for suff in suffixes[:2]:
                phrases.append(f"{p}{suff}")

    # 3. Dynamic parameter variations
    # Volume variations
    for v in [10, 20, 30, 45, 50, 60, 75, 80, 90, 100]:
        phrases.append(f"set volume to {v}")
        phrases.append(f"volume {v} percent")
        phrases.append(f"change volume to {v}")

    # Brightness variations
    for b in [10, 25, 40, 50, 70, 80, 90, 100]:
        phrases.append(f"set brightness to {b}")
        phrases.append(f"screen brightness {b} percent")
        phrases.append(f"adjust brightness to {b}")

    # App launches
    for app in ["chrome", "vscode", "spotify", "notepad", "calculator", "terminal", "edge", "settings"]:
        phrases.append(f"open {app}")
        phrases.append(f"launch {app} please")
        phrases.append(f"bring up {app}")
        phrases.append(f"switch to {app}")

    # Site navigation
    for site in ["youtube", "google", "github", "gmail", "maps", "linkedin"]:
        phrases.append(f"open {site}")
        phrases.append(f"go to {site}")
        phrases.append(f"browse to {site}")
        phrases.append(f"bring up {site}")

    # Folder operations
    for fld in ["downloads", "documents", "desktop", "pictures", "music", "videos", "projects", "work"]:
        phrases.append(f"open {fld} folder")
        phrases.append(f"show my {fld} folder")
        phrases.append(f"open folder {fld}")

    # Timer & scheduler variations
    for t in ["5 minutes", "10 minutes", "15 minutes", "20 minutes", "30 minutes", "1 hour", "45 seconds"]:
        phrases.append(f"set a timer for {t}")
        phrases.append(f"start timer for {t}")
        phrases.append(f"timer {t}")

    # Math calculations & conversions
    for expr in ["25 plus 40", "100 minus 30", "50 times 4", "120 divided by 6", "15 percent of 200"]:
        phrases.append(f"what is {expr}")
        phrases.append(f"calculate {expr}")

    for conv in ["10 miles to kilometers", "500 grams to ounces", "100 fahrenheit to celsius", "2 gallons to liters"]:
        phrases.append(f"convert {conv}")

    # General System 2 knowledge questions (should delegate to None)
    knowledge = [
        "what is the theory of general relativity",
        "how does nuclear fusion power the sun",
        "explain the difference between tcp and udp",
        "who was the first person in space",
        "why is liquid nitrogen so cold",
        "how do jet engines generate thrust",
        "explain how neural networks learn with backpropagation",
        "what is quantum entanglement",
        "tell me a story about Tony Stark and Dum-E",
        "what is the capital of Australia",
        "who wrote the play Hamlet",
        "how do airplanes generate aerodynamic lift",
        "what causes the aurora borealis",
        "explain how black holes distort spacetime",
        "what is the distance from the earth to the sun",
        "how does lithium ion battery chemistry work",
        "what is the speed of light in a vacuum",
        "explain shor's algorithm for quantum factorization",
        "why do leaves change color in autumn",
        "who invented the alternating current induction motor",
    ]
    for q in knowledge:
        phrases.append(q)
        phrases.append(f"jarvis {q}")
        phrases.append(f"could you tell me {q}")

    # Ensure uniqueness while preserving deterministic order
    seen = set()
    unique_phrases = []
    for p in phrases:
        clean_p = p.strip().lower()
        if clean_p and clean_p not in seen:
            seen.add(clean_p)
            unique_phrases.append(clean_p)

    return unique_phrases


class TestDecisionEngineBenchmark(unittest.TestCase):
    """Rigorous performance benchmark and parity validation test suite."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_decision_engine()
        cls.corpus = generate_benchmark_corpus()

    def test_corpus_size_exceeds_1000(self):
        """Verifies benchmark corpus size is strictly > 1,000 realistic phrases."""
        count = len(self.corpus)
        self.assertGreater(
            count,
            1000,
            f"Corpus size must be > 1,000 phrases, got {count}",
        )

    def test_100_percent_parity_with_baseline(self):
        """
        Proves 100% classification match parity between the optimized engine
        (exact prototype map, LRU cache, precompiled regex, S* >= 0.85 early exit)
        and the unoptimized baseline decision algorithm across the entire corpus.
        """
        # Clear LRU cache before test to measure cold & warm behavior
        self.engine._decide_memo.cache_clear()

        mismatches = []
        total = len(self.corpus)

        for phrase in self.corpus:
            base_act, base_score = baseline_decide_unoptimized(self.engine, phrase)
            opt_act, opt_score = self.engine.decide(phrase)

            # Check action equivalence
            if base_act != opt_act:
                mismatches.append(
                    f"Action mismatch for '{phrase}': baseline={base_act} ({base_score:.3f}), optimized={opt_act} ({opt_score:.3f})"
                )
            elif abs(base_score - opt_score) > 1e-4:
                # If both are None (conversation fallback), scores must still match within numerical tolerance
                mismatches.append(
                    f"Score mismatch for '{phrase}': baseline={base_score:.4f}, optimized={opt_score:.4f}"
                )

        parity_pct = ((total - len(mismatches)) / total) * 100.0
        self.assertEqual(
            len(mismatches),
            0,
            f"Parity failure: {len(mismatches)} / {total} mismatches ({parity_pct:.2f}% parity):\n"
            + "\n".join(mismatches[:10]),
        )
        self.assertEqual(parity_pct, 100.0)

    def test_latency_p99_sub_millisecond(self):
        """
        Proves that p99 decision engine latency across realistic workload is strictly < 1.0 ms.
        Simulates realistic user usage patterns: mix of warm repeated queries and cold novel queries.
        """
        self.engine._decide_memo.cache_clear()

        latencies_ms = []

        # Run benchmark across corpus:
        # Pass 1: populate cache / initial evaluation
        # Pass 2: repeat frequent commands (simulating 50% repeated commands in daily workflow)
        benchmark_stream = list(self.corpus)
        frequent_commands = [
            "volume up", "crank up the audio", "mute the audio", "show desktop",
            "take a screenshot", "what time is it", "pause the music", "next track",
            "lock the workstation", "stealth mode", "run system diagnostics",
        ]
        # Interleave frequent commands
        for _ in range(30):
            benchmark_stream.extend(frequent_commands)

        for phrase in benchmark_stream:
            t0 = time.perf_counter()
            self.engine.decide(phrase)
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000.0)

        latencies_ms.sort()
        n = len(latencies_ms)
        p50 = latencies_ms[int(n * 0.50)]
        p95 = latencies_ms[int(n * 0.95)]
        p99 = latencies_ms[int(n * 0.99)]
        max_lat = latencies_ms[-1]

        print(f"\n[BENCHMARK RESULTS] Corpus: {len(self.corpus)} phrases, Stream: {n} calls")
        print(f"p50: {p50:.4f} ms | p95: {p95:.4f} ms | p99: {p99:.4f} ms | max: {max_lat:.4f} ms")

        # Required performance assertions
        self.assertLess(
            p99,
            1.0,
            f"p99 latency ({p99:.4f} ms) must be strictly < 1.0 ms",
        )
        self.assertLess(
            p50,
            0.5,
            f"p50 latency ({p50:.4f} ms) should be well below 0.5 ms",
        )


if __name__ == "__main__":
    unittest.main()
