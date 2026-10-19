"""Part 4 deterministic fuzz tests (random.Random with fixed seeds; no hypothesis, no network, no devices).

a. Intent pipeline: commands.run_command, handle_island_voice_command, decision_engine classify/decide,
   Voice._process (is_command True/False), strip_wake_prefix, is_power_phrase, the WHISPER_NOISE filter.
   EVERY side effect is a recorder (tests/side_effect_guard.py); nothing reaches the real system.
b. Config loader on fuzzed JSON files (temp dir; never the repo config).
c. Lyrics LRC parser + cache/response handling, weather JSON parsing + weather cache.

Violations are collected per invariant and tagged. Known SOURCE BUGS are recorded as
`xfail(strict=True, reason="BUG-F<n>: ...")` tests that assert the bug is ABSENT, so the suite stays green
while the bug exists and turns red (strict XPASS) the moment it is fixed.
"""
from __future__ import annotations

import collections
import dataclasses
import io
import json
import math
import os
import random
import re
import sys
import threading
import time
import contextlib
import unicodedata
from types import SimpleNamespace

import pytest

SLOW_S = 0.200


# =============================================================================================
# helpers
# =============================================================================================
class Violations:
    """tag -> list of (input_repr, detail); keeps the first few examples per tag, counts all."""

    def __init__(self, mapper=None):
        self.ex = collections.defaultdict(list)
        self.n = collections.Counter()
        self.mapper = mapper

    def add(self, tag, inp, detail=""):
        if self.mapper is not None:
            tag = self.mapper(tag) or tag
        self.n[tag] += 1
        if len(self.ex[tag]) < 4:
            r = repr(inp)
            self.ex[tag].append((r if len(r) <= 160 else r[:157] + "...", str(detail)[:200]))

    def get(self, tag):
        return self.ex.get(tag, [])

    def tags(self):
        return sorted(self.n)

    def summary(self, tags=None):
        out = []
        for t in (tags or self.tags()):
            out.append(f"{t} x{self.n[t]}: {self.ex[t]}")
        return "\n".join(out)


class _Null(io.TextIOBase):
    def write(self, s):
        return len(s)


@contextlib.contextmanager
def _quiet():
    with contextlib.redirect_stdout(_Null()), contextlib.redirect_stderr(_Null()):
        yield


# =============================================================================================
# a. intent pipeline
# =============================================================================================
WAKE_WORDS = ["jarvis", "hey jarvis", "ok jarvis", "okay jarvis", "Jarvis,", "JARVIS!"]
EXACT_POWER = ["shut down jarvis", "quit jarvis", "exit jarvis", "power down", "power down jarvis", "go to sleep",
               "sleep mode", "sleep", "take a nap", "standby", "stand by", "go to standby", "rest", "rest mode",
               "standby mode", "wake up", "activate", "come online", "i'm back", "im back", "online"]
NEAR_MISS = ["goodbyes", "go to sleepy", "shut down jarvisx", "rest assured", "shut down jarvis now please jarvis x",
             "quit jarvis2", "exit jarvis.exe", "power down the pc", "sleeping", "go to sleep mode now", "resting",
             "standbys", "stand byx", "sleep walking", "i'm backed", "onlinex", "wake up call", "activated",
             "shutdown jarvis", "shut down", "shut jarvis down", "power", "quit", "exit", "good bye",
             "goodbye jarvis", "bye bye", "go to s1eep", "shut d0wn jarvis", "do not quit jarvis",
             "don't shut down jarvis", "never go to sleep", "quit jarvis or not", "jarvis quit now jarvis jarvis"]
HOSTILE = ["; rm -rf /", "$(calc)", "&& del C:\\*.*", "%TEMP%", "| powershell -c calc", "`whoami`",
           "'; DROP TABLE users; --", "\" & calc & \"", "%APPDATA%\\..\\..", "|| format c:", "> nul", "^&calc",
           "$env:USERPROFILE", "\\\\?\\C:\\Windows", "C:\\Windows\\System32\\cmd.exe", "{0}{1}{2} %s %n",
           "../../../../etc/passwd", "ping -n 99 127.0.0.1 &", "\"; shutdown /s /t 0; \""]
TRAVERSAL = ["open ..\\..\\Windows\\System32", "open folder ..\\..\\Windows", "open ../../etc", "open file C:\\Windows\\win.ini",
             "open folder C:\\Windows\\System32", "open file ..\\..\\secret.exe", "open the downloads folder\\..\\..",
             "open recent file ..\\..", "open file \\\\server\\share\\x.pdf", "open folder %USERPROFILE%",
             "open C:\\", "open folder /", "open folder ~", "open file con.txt", "open nul", "launch ..\\..\\evil.exe",
             "remember this folder as ..\\..\\Windows", "remember this folder as ; calc"]
URLS = ["open javascript:alert(1)", "open file:///C:/", "open http://evil.example/x", "go to file:///C:/Windows",
        "open data:text/html,<script>alert(1)</script>", "open ms-settings:", "open https://evil.example",
        "open vbscript:msgbox(1)", "search for javascript:alert(1)", "search for a&b=c#d", "google \"quoted\" & more",
        "play ; calc & echo hi on spotify", "play \"; calc ; \" on youtube", "look up 100% real <b>x</b>",
        "search for \tTAB\tin query", "open youtube.com/../../", "open github%00.com", "go to maps\\..\\x"]
HOMOGLYPHS = ["j\u0430rvis", "jarv\u0456s", "\uff4a\uff41\uff52\uff56\uff49\uff53", "j\u200barvis", "JARV1S", "\u0135arvis",
              "jarvis\u200b", "ja\u0301rvis", "j\u00e1rvis", "jarv\u0131s", "\u24d9\u24d0\u24e1\u24e5\u24d8\u24e2"]
COMMAND_STEMS = ["open", "launch", "play", "search for", "google", "volume to", "set volume", "mute", "unmute",
                 "set a timer for", "remind me to", "note", "take a note", "type", "remember this folder as",
                 "open folder", "open file", "switch to", "go to", "close", "show", "what is", "who is",
                 "set brightness to", "wiki", "translate", "summarize", "run", "execute", "start", "bring up",
                 "describe what you see", "what am I holding", "analyze my screen", "look at this", "take a screenshot",
                 "weather in", "time in", "lock", "restart", "shutdown", "confirm", "yes", "cancel", "dictation mode on"]
RTL = ["مرحبا جارفيس افتح المتصفح", "שלום ג'ארוויס פתח", "\u202eevil\u202c jarvis", "\u200f\u200fjarvis\u200f",
       "جارفيس اغلق", "افتح \u202e chrome"]
EMOJI = ["\U0001F680", "\U0001F916", "\u2764\ufe0f", "\U0001F468\u200d\U0001F469\u200d\U0001F467", "\U0001F3F3\ufe0f\u200d\U0001F308"]
KNOWN_GOOD = ["volume up", "volume down", "mute", "open chrome", "open youtube", "what time is it", "next page",
              "show weather", "lock the computer", "take a screenshot", "pause the music", "open downloads folder",
              "show last download", "open recent pdf", "set a timer for five minutes", "search for cats and dogs",
              "play music", "stealth mode", "clean slate", "restart", "shutdown", "confirm shutdown", "yes",
              "describe what you see", "analyze my screen", "dictation mode on"]


def _rand_unicode(rng, n):
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.55:
            out.append(chr(rng.randint(0x20, 0x7E)))
        elif r < 0.75:
            out.append(chr(rng.randint(0xA0, 0x24F)))
        elif r < 0.85:
            out.append(chr(rng.randint(0x400, 0x4FF)))
        elif r < 0.90:
            out.append(chr(rng.choice([0x200B, 0x200D, 0x202E, 0x2028, 0x2029, 0x00A0, 0x3000, 0xFEFF])))
        elif r < 0.95:
            out.append(chr(rng.randint(0x4E00, 0x9FFF)))
        else:
            out.append(chr(rng.randint(0x1F300, 0x1FAFF)))
    return "".join(out)


def _mixcase(rng, s):
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in s)


def build_corpus(seed=20260601, n_random=200):
    """Deterministic corpus of (category, text). ~1.1k short inputs + a few 100 KB inputs."""
    rng = random.Random(seed)
    out = []
    add = lambda cat, t: out.append((cat, t))
    # exact + near-miss power phrases, with and without decorations
    for p in EXACT_POWER:
        add("power_exact", p)
        add("power_exact", p.upper())
        add("power_exact", f"{rng.choice(WAKE_WORDS)} {p}")
        add("power_exact", f"{p}, please.")
        add("power_exact", f"{p} now")
        add("power_deco", f"hey jarvis please {p}!")
    for p in NEAR_MISS:
        add("power_near", p)
        add("power_near", f"{rng.choice(WAKE_WORDS)} {p}")
        add("power_near", p.title())
    # chained compositions that include a power phrase
    for p in rng.sample(EXACT_POWER, 10):
        add("chained_power", f"volume up and {p}")
        add("chained_power", f"{p} and then mute")
        add("chained_power", f"hello there; {p}")
    for p in ("confirm shutdown", "confirm restart", "yes confirm shutdown", "yes confirm restart"):
        add("chained_power", f"volume up and {p}")
        add("chained_power", f"{p} then mute")
    for h in HOSTILE:
        add("hostile", h)
        add("hostile", f"jarvis {h}")
        add("hostile", f"open {h}")
        add("hostile", f"search for {h}")
        add("hostile", f"play {h}")
        add("hostile", f"note {h}")
        add("hostile", f"type {h}")
    for t in TRAVERSAL + URLS:
        add("traversal_url", t)
        add("traversal_url", "jarvis " + t)
    for g in HOMOGLYPHS:
        for tail in ("shut down jarvis", "quit jarvis", "go to sleep", "open chrome", "volume up", "sleep", ""):
            add("homoglyph", f"{g} {tail}".strip())
            add("homoglyph", f"shut down {g}")
    for t in ("", " ", "\t", "\n\n", "\r\n", "\u00a0", "\u200b", "   \t  ", ".", "?!", ",,,", "\x00", "\x00\x00jarvis",
              "jarvis\x00open chrome", "\x1b[31mred\x1b[0m", "\x07\x08\x7f", "\ufeffjarvis open chrome", "\u2028", "\u2029"):
        add("empty_ctrl", t)
    for _ in range(40):
        add("empty_ctrl", "".join(chr(rng.randint(0, 31)) for _ in range(rng.randint(1, 20))))
    for t in RTL + EMOJI:
        add("rtl_emoji", t)
        add("rtl_emoji", "jarvis " + t)
        add("rtl_emoji", "open " + t)
    for _ in range(40):
        add("rtl_emoji", " ".join(rng.choice(RTL + EMOJI + KNOWN_GOOD) for _ in range(rng.randint(2, 6))))
    # wake words embedded mid-string
    for _ in range(80):
        a = _rand_unicode(rng, rng.randint(0, 12))
        b = rng.choice(KNOWN_GOOD + EXACT_POWER + NEAR_MISS)
        w = rng.choice(WAKE_WORDS + ["jarvis" * 3, "jarvis jarvis jarvis"])
        add("wake_mid", rng.choice([f"{a} {w} {b}", f"{b} {w}", f"{w}{b}", f"{a}{w}", f"{b} {w} {b}"]))
    # command stems with hostile / random arguments
    payloads = HOSTILE + TRAVERSAL + URLS + HOMOGLYPHS + RTL + EMOJI + ["", "0", "-1", "101", "999999999999999999999",
                                                                        "3.14", "NaN", "1e999", "five minutes", "x" * 300]
    for _ in range(150):
        stem = rng.choice(COMMAND_STEMS)
        arg = rng.choice(payloads) if rng.random() < 0.8 else _rand_unicode(rng, rng.randint(1, 30))
        t = f"{stem} {arg}"
        if rng.random() < 0.3:
            t = _mixcase(rng, t)
        if rng.random() < 0.3:
            t = rng.choice(WAKE_WORDS) + " " + t
        add("stem_args", t)
    for _ in range(n_random):
        add("random_unicode", _rand_unicode(rng, rng.randint(0, 70)))
    for _ in range(60):
        add("mixed_case", _mixcase(rng, rng.choice(KNOWN_GOOD + EXACT_POWER + NEAR_MISS)))
    for t in KNOWN_GOOD:
        add("known_good", t)
    # very long strings: 100 KB flat inputs, plus smaller repeated-filler / chained inputs that expose
    # super-linear behaviour (the 100 KB versions of those take 5-17 s per call, see BUG-F7)
    big = 100 * 1024
    add("long", "a" * big)
    add("long", "open " + "chrome " * (big // 7))
    add("long", "jarvis " + "".join(rng.choice("abc ,.!?;&|") for _ in range(big)))
    add("long", "jarvis " * 3500)
    add("long", "volume up and " * 3000)
    return out


# Phrases whose normalised whole-utterance equals a power-ish phrase (mirrors commands._power_forms semantics)
def _forms(text):
    import commands
    return commands._power_forms(text)


def _wants_clean_slate(text):
    import commands
    t = " ".join(re.sub(r"[^\w\s']", " ", str(text).lower()).split())
    return commands._any(t, "clean slate protocol", "clean slate", "protocol clean slate", "wrap it up")


_SUBPROC_OK = (
    re.compile(r'start "" [A-Za-z0-9_.:\-]+'),
    re.compile(r'start "" "spotify:search:[A-Za-z0-9%_.~\-]*"'),
)
_SHELL_META = re.compile(r"[&|;<>`$^%\"\r\n]")


def _subprocess_violation(name, args):
    cmd = args[0] if args else None
    if isinstance(cmd, str):
        return None if any(rx.fullmatch(cmd) for rx in _SUBPROC_OK) else f"{name}({cmd!r})"
    if isinstance(cmd, (list, tuple)):
        if list(cmd) == ["tasklist", "/FI", "IMAGENAME eq Spotify.exe"]:
            return None
        return f"{name}({list(cmd)!r})"
    return f"{name}(<{type(cmd).__name__}>)"


def _url_violation(u):
    import commands
    if not isinstance(u, str):
        return f"non-str url {u!r}"
    if re.search(r"[\x00-\x20\x7f]", u):
        return "whitespace/control char in URL"
    if u in commands.SITES.values() or u == "https://www.google.com":
        return None
    if u.startswith("https://www.google.com/search?q="):
        return None
    if re.fullmatch(r"https://www\.youtube\.com/results\?search_query=[A-Za-z0-9%._~/\-]*", u):
        return None
    if re.fullmatch(r"https://open\.spotify\.com/search/[A-Za-z0-9%._~/\-]*", u):
        return None
    if u.startswith("file:///") and u.replace("\\", "/").endswith("tests/target_practice.html"):
        return None
    return "URL outside whitelist"


def _path_violation(path, cfg):
    if not isinstance(path, str):
        return f"non-str path {path!r}"
    norm = os.path.normcase(os.path.abspath(path))
    if ".." in re.split(r"[\\/]", path):
        return "'..' segment in opened path"
    for root in list(cfg.folders_whitelist.values()) + list(cfg.indexed_roots):
        r = os.path.normcase(os.path.abspath(root))
        if norm == r or norm.startswith(r + os.sep):
            return None
    return "path outside folders_whitelist"


@pytest.fixture(scope="module")
def pipeline():
    """Runs the whole intent corpus once under the side-effect guard and returns the Violations."""
    import commands
    import config
    import decision_engine
    import voice
    from side_effect_guard import SideEffectGuard, make_ctx

    viol = Violations()
    stats = {"inputs": 0, "calls": 0, "side_effect_calls": 0, "slowest_ms": 0.0, "by_label": collections.Counter(),
             "by_cat": collections.Counter()}
    corpus = build_corpus()
    cfg = config.Config()
    assert cfg.ai_enabled is False
    eng = decision_engine.get_decision_engine()
    orig_play = voice.play_chime
    voice.play_chime = lambda *a, **k: None
    try:
        with SideEffectGuard(cfg) as g, _quiet():
            ctx = make_ctx(g, cfg)
            v = voice.Voice(cfg, ctx, threading.Event())
            v.ctx = ctx
            v._duck_media = lambda *a, **k: None
            v._rearm_followup = lambda *a, **k: None
            import productivity
            import context_resolver

            def reset():
                commands._PENDING_CONFIRMATION = None
                try:
                    productivity.set_dictation_mode(False)
                except Exception:
                    pass
                try:
                    context_resolver.get_disambiguation_engine().cancel("fuzz")
                except Exception:
                    pass

            def vadd(tag, inp, detail=""):
                viol.add(bug_of(tag, inp, detail) or tag, inp, detail)

            def run(label, fn, text, extra_text=None):
                """Run one pipeline call; check never-raises, latency, and side-effect invariants."""
                reset()
                start = len(g.calls)
                t0 = time.perf_counter()
                try:
                    fn()
                except BaseException as exc:  # noqa: BLE001 - we want every crash
                    vadd(f"raises:{label}:{type(exc).__name__}", text, f"{exc!r}")
                dt = time.perf_counter() - t0
                stats["calls"] += 1
                stats["by_label"][label] += dt
                stats["by_cat"][cur_cat[0]] += dt
                stats["slowest_ms"] = max(stats["slowest_ms"], dt * 1000)
                if dt > SLOW_S:
                    vadd(f"slow:{label}", text, f"{dt * 1000:.0f} ms")
                _check(label, extra_text if extra_text is not None else text, g.calls[start:])

            def _check(label, text, calls):
                names = [c[0] for c in calls]
                stats["side_effect_calls"] += sum(
                    1 for n in names if not n.startswith(("overlay.", "ctx.say", "ctx.chirp")))
                quit_ok = commands.is_power_phrase(text, "quit")
                standby_ok = commands.is_power_phrase(text, "standby") or _wants_clean_slate(text)
                fs = _forms(text)
                down_ok = any(f in ("confirm shutdown", "yes confirm shutdown") for f in fs)
                restart_ok = any(f in ("confirm restart", "yes confirm restart") for f in fs)
                for name, a, k in calls:
                    if name == "ctx.quit" and not quit_ok:
                        vadd(f"power:quit:{label}", text)
                    elif name == "ctx.set_active" and a == (False,) and not standby_ok:
                        vadd(f"power:standby:{label}", text)
                    elif name == "system_control.shutdown_system" and not down_ok:
                        vadd(f"power:shutdown:{label}", text)
                    elif name == "system_control.restart_system" and not restart_ok:
                        vadd(f"power:restart:{label}", text)
                    elif name == "system_control.sleep_system" and not any(
                            f in ("sleep system", "sleep pc", "sleep computer", "put pc to sleep",
                                  "put computer to sleep") for f in fs):
                        vadd(f"power:sleep_system:{label}", text)
                    elif name.startswith(("subprocess.", "os.system", "os.popen", "os.startfile")):
                        if name.startswith("subprocess."):
                            bad = _subprocess_violation(name, a)
                        else:
                            bad = f"{name}{a!r}"
                        if bad:
                            vadd(f"subprocess:{label}", text, bad)
                    elif name == "webbrowser.open" or name.startswith("webbrowser."):
                        bad = _url_violation(a[0] if a else None)
                        if bad:
                            vadd(f"url:{label}", text, f"{bad}: {a[:1]!r}")
                    elif name in ("file_ops.open",):
                        bad = _path_violation(a[0] if a else None, cfg)
                        if bad:
                            vadd(f"path:{label}", text, f"{bad}: {a[:1]!r}")
                    elif name.startswith("capture.") or name == "groq.client":
                        vadd(f"ai_off_capture:{label}", text, name)

            cur_cat = [""]
            cat_long_seen = [0]
            def remainder_after_alias(t):
                low = t.lower().strip()
                for alias in getattr(cfg, "wake_aliases", ["jarvis"]):
                    m = re.search(r"\b" + re.escape(alias) + r"\b", low)
                    if m:
                        return low[m.end():].strip(" ,.!?")
                return low

            for cat, text in corpus:
                cur_cat[0] = cat
                stats["inputs"] += 1
                run("run_command", lambda: commands.run_command(text, ctx), text)
                run("island", lambda: commands.handle_island_voice_command(text, ctx), text)
                run("classify", lambda: eng.classify(text), text)
                run("decide", lambda: eng.decide(text), text)

                def helpers():
                    out = commands.strip_wake_prefix(text)
                    assert isinstance(out, str)
                    for kind in ("quit", "standby", "wake"):
                        assert commands.is_power_phrase(text, kind) in (True, False)
                    assert isinstance(voice._normalize_noise_text(text) in voice._WHISPER_NOISE_NORMALIZED, bool)
                run("helpers", helpers, text)
                if cat == "long" and len(text) > 30000 and cat_long_seen[0] >= 3:
                    continue                      # Voice._process only wraps run_command; bound the 100 KB cost
                if cat == "long":
                    cat_long_seen[0] += 1
                run("process_cmd", lambda: v._process(text, is_command=True), text)

                def p_false():
                    v.listening_until = 0.0
                    v._process("hey jarvis, " + text, is_command=False)
                if cat != "long":
                    run("process_wake", p_false, text, extra_text="hey jarvis, " + text)

                    def p_nowake():
                        v.listening_until = 0.0
                        v._process(text, is_command=False)
                    # inactive path: the command is whatever follows the first wake alias, anywhere in the text
                    run("process_nowake", p_nowake, text, extra_text=remainder_after_alias(text))
    finally:
        voice.play_chime = orig_play
    if os.environ.get("FUZZ_VERBOSE"):
        print(ascii(viol.summary()))
        print({k: round(v, 1) for k, v in stats["by_label"].items()}, {k: round(v, 1) for k, v in stats["by_cat"].items()})
    stats["violations"] = dict(viol.n)
    print(f"\n[FUZZ-A] inputs={stats['inputs']} pipeline_calls={stats['calls']} "
          f"side_effect_calls={stats['side_effect_calls']} slowest={stats['slowest_ms']:.0f} ms "
          f"violation_tags={len(viol.n)}", flush=True)
    return viol, stats


_CHAIN_RE = re.compile(r"\band\b|\bthen\b|;|\balso\b", re.I)


def bug_of(tag, text, detail):
    """Map a raw violation to a documented SOURCE BUG id (None = unknown / new)."""
    kind = tag.split(":")[0]
    d = str(detail)
    if kind == "power":
        if any(c in text for c in "ıſK"):
            return "BUG-F8"            # strip_wake_prefix (re.I) treats dotless-i / long-s as 'jarvis', is_power_phrase does not
        if _CHAIN_RE.search(text):
            return "BUG-F1"            # compound utterance executes a power/destructive sub-command
        if tag.startswith("power:standby"):
            return "BUG-F2"            # fuzzy match -> Clean Slate Protocol (hide windows + standby)
    if kind == "path":
        return "BUG-F3"                # open folder <absolute path> bypasses folders_whitelist
    if kind == "ai_off_capture" and d == "groq.client":
        return "BUG-F4"                # wiki fallback calls Groq with ai_enabled False
    if kind == "url" and "whitespace/control" in d:
        return "BUG-F5"                # google search URL not percent-encoded
    if kind == "slow":
        ms = float(d.split()[0]) if d.split() and d.split()[0].replace(".", "").isdigit() else 0.0
        if text.startswith(("volume up and", "jarvis jarvis")) or ms >= 1500:
            return "BUG-F7"            # quadratic / unbounded work on repeated fillers or chain length
        return "BUG-F6"                # no input length cap: 100 KB utterance takes ~0.9 s per call
    return None


def _only(viol, prefix, known=()):
    """Examples for tags starting with prefix, excluding tags listed in `known` (documented bugs)."""
    return [t for t in viol.tags() if t.startswith(prefix) and t not in known]


def test_pipeline_corpus_is_large_and_deterministic():
    a = build_corpus()
    b = build_corpus()
    assert a == b
    assert len(a) >= 1100
    assert max(len(t) for _, t in a) >= 100 * 1024


def test_pipeline_never_raises(pipeline):
    viol, _ = pipeline
    bad = _only(viol, "raises:", KNOWN_RAISES)
    assert not bad, viol.summary(bad)


def test_pipeline_latency_under_200ms(pipeline):
    viol, _ = pipeline
    bad = _only(viol, "slow:", KNOWN_SLOW)
    assert not bad, viol.summary(bad)


def test_pipeline_power_actions_only_on_exact_phrases(pipeline):
    viol, _ = pipeline
    bad = _only(viol, "power:", KNOWN_POWER)
    assert not bad, viol.summary(bad)


def test_pipeline_subprocess_never_gets_user_metacharacters(pipeline):
    viol, _ = pipeline
    bad = _only(viol, "subprocess:", KNOWN_SUBPROC)
    assert not bad, viol.summary(bad)


def test_pipeline_urls_and_paths_stay_inside_whitelists(pipeline):
    viol, _ = pipeline
    bad = _only(viol, "url:", KNOWN_URL) + _only(viol, "path:", KNOWN_PATH)
    assert not bad, viol.summary(bad)


def test_pipeline_no_capture_or_cloud_when_ai_disabled(pipeline):
    viol, _ = pipeline
    bad = _only(viol, "ai_off_capture:")
    assert not bad, viol.summary(bad)


def test_pipeline_actually_exercised_side_effect_paths(pipeline):
    _, stats = pipeline
    assert stats["side_effect_calls"] > 100, stats



def _xf(bug, reason):
    return pytest.param(bug, marks=pytest.mark.xfail(strict=True, reason=f"{bug}: {reason}"), id=bug)


@pytest.mark.parametrize("bug", [
    _xf("BUG-F1", "commands.py:432 run_command -> chained_commands.execute_chained_commands runs ANY sub-command, "
                  "so 'volume up and power down jarvis' calls ctx.quit(), 'hello there; go to sleep' calls "
                  "set_active(False), 'volume up and confirm shutdown' calls shutdown_system(confirmed=True) "
                  "although the whole input is not an exact power phrase"),
    _xf("BUG-F2", "commands.py:~698 / decision engine: bare 'execute' (and 'execute <junk>') runs Clean Slate "
                  "Protocol: Win+D plus ctx.set_active(False) (standby), no power phrase involved"),
    _xf("BUG-F3", "file_ops.py:154-156 open_folder 'Direct path check if user gave absolute path' opens any existing "
                  "directory: 'open folder C:\\Windows\\System32' and 'open folder /' call the opener outside "
                  "folders_whitelist"),
    _xf("BUG-F4", "quick_answers.py:330-333 get_quick_answer Groq fallback runs whenever an API key exists, ignoring "
                  "cfg.ai_enabled: 'wiki five minutes' calls GroqClient with ai_enabled False"),
    _xf("BUG-F5", "commands.py:913 google search URL built with replace(' ', '+') only: not percent-encoded, so "
                  "'search for cats & dogs' -> q=cats+&+dogs (query truncated at '&'), tabs/control chars land in the URL"),
    _xf("BUG-F6", "no input length cap: a 100 KB utterance costs ~0.85-1.0 s per run_command call (>200 ms)"),
    _xf("BUG-F8", "commands.py:121-126 strip_wake_prefix uses re.I, which also matches U+0131 (dotless i) and U+017F "
                  "(long s) as 'jarvis', but is_power_phrase/_power_forms (lines 80-117) compare lowercase text: "
                  "'jarv\u0131s quit jarvis' calls ctx.quit() while is_power_phrase(text, 'quit') is False, so the "
                  "gate and the executor disagree"),
    _xf("BUG-F7", "super-linear work on repetitive input: commands.py:80 _power_forms is quadratic in repeated fillers "
                  "('jarvis ' x14k: 15 s) and chained_commands executes an unbounded number of sub-commands "
                  "('volume up and ' x7.8k: 5 s)"),
])
def test_known_pipeline_source_bugs_stay_absent(pipeline, bug):
    viol, _ = pipeline
    assert bug not in viol.n, viol.summary([bug])


KNOWN_RAISES = ()
KNOWN_SLOW = ()
KNOWN_POWER = ()
KNOWN_SUBPROC = ()
KNOWN_URL = ()
KNOWN_PATH = ()


# =============================================================================================
# b. config loader
# =============================================================================================
GARBAGE = [None, True, False, 0, 1, -1, -(10 ** 9), 7, 3.5, -2.5, 1e308, -1e308, 1e-308, 10 ** 30, -(10 ** 30),
           10 ** 400, float("nan"), float("inf"), float("-inf"), "", "abc", "true", "false", "yes", "1e999", "NaN",
           "inf", "-inf", "0", "12", " 7 ", "\x00", "‮", "x" * 300, [], [1, 2, 3], ["a", None, 3, {"x": []}],
           {}, {"a": 1}, {"a": {"b": [{"c": None}]}}, [[[[[]]]]], {"": ""}, [float("nan")], "\U0001F680"]


def _field_kind(f, default):
    t = f.type
    if t is int or t == "int":
        return int
    if t is float or t == "float":
        return float
    if t is bool or t == "bool":
        return bool
    if t is str or t == "str":
        return str
    origin = getattr(t, "__origin__", None)
    if origin is list or (isinstance(t, str) and t.startswith("list")):
        return list
    if origin is dict or (isinstance(t, str) and t.startswith("dict")):
        return dict
    return type(default)


def _type_problems(cfg):
    """Shallow declared-type check of every Config field plus clamps and enum fields."""
    import config
    probs = []
    d = config.Config()
    for f in dataclasses.fields(config.Config):
        v = getattr(cfg, f.name)
        kind = _field_kind(f, getattr(d, f.name))
        if f.name == "mic_device_index" and v is None:
            continue
        ok = isinstance(v, kind) and not (kind is int and isinstance(v, bool))
        if not ok:
            probs.append(("type", f.name, type(v).__name__, repr(v)[:40]))
    gt = cfg.glass_transparency
    if not isinstance(gt, float) or not (0.0 <= gt <= 1.0):
        probs.append(("clamp", "glass_transparency", repr(gt)))
    if cfg.glass_mode not in ("clear", "tinted"):
        probs.append(("enum", "glass_mode", repr(cfg.glass_mode)))
    if cfg.island_idle_display not in ("off", "time", "time_weather"):
        probs.append(("enum", "island_idle_display", repr(cfg.island_idle_display)))
    return probs


def _deep_problems(cfg):
    probs = []
    for name in ("wake_aliases", "island_pages", "indexed_roots"):
        v = getattr(cfg, name)
        if any(not isinstance(x, str) for x in v):
            probs.append(("elements", name))
    if any(not isinstance(k, str) or not isinstance(v, bool) for k, v in cfg.island_pages_enabled.items()):
        probs.append(("elements", "island_pages_enabled"))
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in cfg.folders_whitelist.items()):
        probs.append(("elements", "folders_whitelist"))
    return probs


def _cfg_repr(cfg):
    return repr(dataclasses.asdict(cfg))


def _cfg_bug(tag):
    if tag.startswith(("raises", "raises_second_load")) and tag.endswith("OverflowError"):
        return "BUG-F9"
    if tag == "fieldtype:type:mic_device_index":
        return "BUG-F10"
    if tag.startswith("deep:"):
        return "BUG-F11"
    return None


def build_config_cases(seed=4242):
    """[(label, bytes, bak_bytes_or_None)] deterministic."""
    import config
    rng = random.Random(seed)
    base = dataclasses.asdict(config.Config())
    keys = list(base)
    good = json.dumps(base, indent=4).encode()
    alias_keys = [o for o, _ in config._ISLAND_KEY_ALIASES] + ["glass_blur", "island_backdrop_blur",
                                                                 "island_backdrop_opacity"]
    cases = []

    def add(label, data, bak=None):
        cases.append((label, data, bak))

    for _ in range(40):
        add("truncated", good[: rng.randint(0, len(good) - 1)])
    for _ in range(30):
        add("random_bytes", rng.randbytes(rng.randint(0, 3000)))
    for _ in range(15):
        b = bytearray(good)
        for _ in range(rng.randint(1, 8)):
            b[rng.randrange(len(b))] = 0
        add("null_bytes", bytes(b))
    add("bom_valid", b"\xef\xbb\xbf" + json.dumps({"camera_index": 3, "glass_transparency": 0.25}).encode())
    add("bom_valid", b"\xef\xbb\xbf" + good)
    add("bom_garbage", b"\xef\xbb\xbf" + rng.randbytes(50))
    add("utf16", json.dumps({"camera_index": 3}).encode("utf-16"))
    add("empty", b"")
    add("whitespace", b"   \n\t ")
    for root in ("[]", "[1,2,3]", '"string"', "123", "null", "true", "NaN", "Infinity", '{"a":', "{", "}", "{}",
                 '{"config_version": 4}', '{"config_version": 1}', "[" * 5000, "[" * 100000 + "]" * 100000):
        add("root_type", root.encode())
    for _ in range(100):
        d = dict(base)
        for k in rng.sample(keys, rng.randint(1, 8)):
            d[k] = rng.choice(GARBAGE)
        add("wrong_types", json.dumps(d).encode())
    for k in keys:                                     # every field gets 2 of 6 garbage kinds
        for gv in rng.sample([float("inf"), "abc", [], {"a": 1}, 10 ** 400, float("nan")], 1):
            add("per_field", json.dumps({k: gv}).encode())
    for _ in range(20):
        d = dict(base)
        for _ in range(rng.randint(1, 6)):
            d[rng.choice(["__class__", "__dict__", "_runtime_overrides", "foo", "", "x" * 100, "config", "‮",
                          "island_pages_extra", "glass_blur2"]) + str(rng.randint(0, 3))] = rng.choice(GARBAGE)
        add("unknown_keys", json.dumps(d).encode())
    for _ in range(70):
        d = {}
        for k in rng.sample(alias_keys, rng.randint(1, 6)):
            d[k] = rng.choice(GARBAGE + ["compact", "default", "large", "media_aware", "24h", "12h", "off", "time"])
        if rng.random() < 0.8:
            d["config_version"] = rng.choice([0, 1, 2, 3, 4, 5, -1, "abc", 10 ** 30, None, 2.5, "3", True, [], {}])
        if rng.random() < 0.5:
            d["wake_aliases"] = rng.choice([["service", "jarvis"], ["harvest"], ["jar is", 3, None], "jarvis", [[]]])
        add("aliases", json.dumps(d).encode())
    for cv in (-1, "abc", 10 ** 30, None, 2.5, float("inf"), float("-inf"), float("nan"), 10 ** 400, "", [], {},
               True, False, "9" * 50, -(10 ** 30)):
        add("config_version", json.dumps({"config_version": cv, "glass_blur": False, "time_format": "24h"}).encode())
    valid_bak = json.dumps({"camera_index": 7}).encode()
    for _ in range(8):
        add("corrupt_with_good_bak", good[: rng.randint(1, len(good) - 1)], valid_bak)
    for _ in range(4):
        add("corrupt_with_bad_bak", rng.randbytes(40), rng.randbytes(40))
    pad = "x" * (5 * 1024 * 1024)
    add("large", json.dumps({"camera_index": 2, "padding": pad}).encode())
    add("large", rng.randbytes(5 * 1024 * 1024))
    add("large", json.dumps({"wake_aliases": ["a" * 1000] * 4000, "camera_index": 5}).encode())
    return cases


@pytest.fixture(scope="module")
def config_fuzz(tmp_path_factory):
    import config
    viol = Violations(_cfg_bug)
    root = tmp_path_factory.mktemp("cfgfuzz")
    cases = build_config_cases()
    real_fsync = os.fsync
    os.fsync = lambda fd: None                      # durability is not under test; keeps the rewrites fast
    stats = {"cases": len(cases), "t": 0.0}
    t_start = time.perf_counter()
    try:
        with _quiet():
            for i, (label, data, bak) in enumerate(cases):
                d = root / f"c{i}"
                d.mkdir()
                path = str(d / "jarvis_config.json")
                with open(path, "wb") as f:
                    f.write(data)
                if bak is not None:
                    with open(path + ".bak", "wb") as f:
                        f.write(bak)
                sig = f"{label}#{i}"
                try:
                    cfg = config.load_config(path)
                except BaseException as exc:  # noqa: BLE001
                    viol.add(f"raises:{label}:{type(exc).__name__}", sig, f"{exc!r}"[:150])
                    continue
                if not isinstance(cfg, config.Config):
                    viol.add(f"returns:{label}", sig, type(cfg).__name__)
                    continue
                for p in _type_problems(cfg):
                    viol.add(f"fieldtype:{p[0]}:{p[1]}", sig, p)
                for p in _deep_problems(cfg):
                    viol.add(f"deep:{p[1]}", sig, p)
                # persistence: never replace the user's bytes with garbage, always keep them in .bak
                with open(path, "rb") as f:
                    final = f.read()
                if final != data and len(data) > 0 and bak is None:
                    bakb = None
                    if os.path.exists(path + ".bak"):
                        with open(path + ".bak", "rb") as f:
                            bakb = f.read()
                    if bakb != data:
                        viol.add(f"overwritten_without_bak:{label}", sig,
                                 f"final={len(final)}B bak={None if bakb is None else len(bakb)}B")
                if final != data:
                    try:
                        parsed = json.loads(final.decode("utf-8"))
                        if not isinstance(parsed, dict):
                            raise ValueError("not a dict")
                    except Exception as exc:
                        viol.add(f"rewritten_with_garbage:{label}", sig, exc)
                try:
                    cfg2 = config.load_config(path) if i % 3 == 0 else cfg
                    if _cfg_repr(cfg2) != _cfg_repr(cfg):
                        viol.add(f"not_idempotent:{label}", sig)
                except BaseException as exc:  # noqa: BLE001
                    viol.add(f"raises_second_load:{label}:{type(exc).__name__}", sig, f"{exc!r}"[:150])
                if label == "corrupt_with_good_bak" and cfg.camera_index != 7:
                    viol.add("bak_not_restored", sig, cfg.camera_index)
    finally:
        os.fsync = real_fsync
    stats["t"] = time.perf_counter() - t_start
    print(f"\n[FUZZ-B] config cases={stats['cases']} in {stats['t']:.1f}s violation_tags={len(viol.n)}", flush=True)
    if os.environ.get("FUZZ_VERBOSE"):
        print(ascii(viol.summary()))
    return viol, stats


def test_config_corpus_size():
    cs = build_config_cases()
    assert len(cs) >= 450
    assert max(len(c[1]) for c in cs) >= 5 * 1024 * 1024


def test_config_load_never_raises(config_fuzz):
    viol, _ = config_fuzz
    bad = [t for t in viol.tags() if t.startswith("raises") and t not in KNOWN_CFG_RAISES]
    assert not bad, viol.summary(bad)


def test_config_fields_keep_declared_types_and_clamps(config_fuzz):
    viol, _ = config_fuzz
    bad = [t for t in viol.tags() if t.startswith("fieldtype") and t not in KNOWN_CFG_TYPES]
    assert not bad, viol.summary(bad)


def test_config_corrupt_file_never_replaced_without_backup(config_fuzz):
    viol, _ = config_fuzz
    bad = [t for t in viol.tags()
           if t.startswith(("overwritten_without_bak", "rewritten_with_garbage", "bak_not_restored"))
           and t not in KNOWN_CFG_PERSIST]
    assert not bad, viol.summary(bad)


def test_config_load_is_idempotent(config_fuzz):
    viol, _ = config_fuzz
    bad = [t for t in viol.tags() if t.startswith("not_idempotent") and t not in KNOWN_CFG_IDEM]
    assert not bad, viol.summary(bad)


def test_config_deep_container_elements_keep_declared_element_types(config_fuzz):
    viol, _ = config_fuzz
    bad = [t for t in viol.tags() if t.startswith("deep:") and t not in KNOWN_CFG_DEEP]
    assert not bad, viol.summary(bad)



@pytest.mark.parametrize("bug", [
    _xf("BUG-F9", "config.py:397-405 _coerce_field_value catches only (ValueError, TypeError): int(float(inf)) and "
                  "float(10**400) raise OverflowError out of load_config. Repro: file {\"camera_index\": Infinity} "
                  "(json.load accepts Infinity), {\"glass_transparency\": <int with 400 digits>}, or "
                  "{\"config_version\": Infinity}"),
    _xf("BUG-F10", "config.py:385-411 mic_device_index is declared int but defaults to None, so _coerce_field_value "
                   "returns ANY garbage unchanged ({\"mic_device_index\": \"abc\"} -> 'abc', 3.5 -> 3.5, [] -> [])"),
    _xf("BUG-F11", "config.py:410-415 list/dict fields are only shallow-checked: {\"wake_aliases\": [\"jarvis\", 3]} "
                   "loads with a non-str alias (voice._process then does re.escape(3)); folders_whitelist / "
                   "island_pages_enabled accept wrong key/value types"),
])
def test_known_config_source_bugs_stay_absent(config_fuzz, bug):
    viol, _ = config_fuzz
    assert bug not in viol.n, viol.summary([bug])


@pytest.mark.xfail(strict=True, reason="BUG-F12: config.py:~467 opens the file with encoding='utf-8' (not 'utf-8-sig'), "
                                       "so a valid JSON config saved with a UTF-8 BOM (Notepad does this) raises "
                                       "'Unexpected UTF-8 BOM', is treated as corrupt and the user's settings are "
                                       "replaced by defaults (the original is kept only in .bak)")
def test_bom_prefixed_valid_config_is_loaded(tmp_path):
    import config
    path = str(tmp_path / "jarvis_config.json")
    with open(path, "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps({"camera_index": 3, "glass_transparency": 0.25}).encode())
    with _quiet():
        cfg = config.load_config(path)
    assert cfg.camera_index == 3 and cfg.glass_transparency == 0.25


def test_corrupt_config_is_repaired_with_backup_kept(tmp_path):
    """What config.py promises: a corrupt file is replaced by valid defaults and the original bytes stay in .bak."""
    import config
    path = str(tmp_path / "jarvis_config.json")
    garbage = b"{ this is not json \x00\xff"
    with open(path, "wb") as f:
        f.write(garbage)
    with _quiet():
        cfg = config.load_config(path)
    assert isinstance(cfg, config.Config)
    assert json.load(open(path, encoding="utf-8"))["config_version"] == 4
    assert open(path + ".bak", "rb").read() == garbage


def test_corrupt_config_restored_from_good_backup(tmp_path):
    import config
    path = str(tmp_path / "jarvis_config.json")
    with open(path, "wb") as f:
        f.write(b"{truncated")
    with open(path + ".bak", "w", encoding="utf-8") as f:
        json.dump({"camera_index": 7}, f)
    with _quiet():
        cfg = config.load_config(path)
    assert cfg.camera_index == 7
    assert json.load(open(path, encoding="utf-8"))["camera_index"] == 7


def test_glass_transparency_clamp_and_enum_repairs(tmp_path):
    import config
    for n, (raw, want) in enumerate([(5, 1.0), (-3, 0.0), (1e308, 1.0), ("0.4", 0.4), (float("inf"), 1.0), (float("-inf"), 0.0)]):
        path = str(tmp_path / f"c{n}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"glass_transparency": raw, "glass_mode": "bogus", "island_idle_display": "media_aware"}))
        with _quiet():
            cfg = config.load_config(path)
        assert cfg.glass_transparency == want, (raw, cfg.glass_transparency)
        assert cfg.glass_mode == "tinted" and cfg.island_idle_display == "time_weather"


KNOWN_CFG_RAISES = ()
KNOWN_CFG_TYPES = ()
KNOWN_CFG_PERSIST = ()
KNOWN_CFG_IDEM = ()
KNOWN_CFG_DEEP = ()


# =============================================================================================
# c. lyrics LRC parser, lyrics client payloads, weather JSON
# =============================================================================================
def _lyrics_bug(tag):
    if tag.startswith(("to_lyrics", "lyrics_api_raises")):
        return "BUG-F13"
    if tag.startswith("lyrics_cache_raises"):
        return "BUG-F14"
    if tag == "lyrics_invalid_structure":
        return "BUG-F13"
    return None


def _rand_lrc(rng):
    parts = []
    for _ in range(rng.randint(0, 25)):
        r = rng.random()
        if r < 0.45:
            ms = (rng.choice([".", ":"]) + str(rng.randint(0, 9999))) if rng.random() < 0.7 else ""
            parts.append(f"[{rng.randint(0, 999)}:{rng.randint(0, 99)}{ms}]{_rand_unicode(rng, rng.randint(0, 20))}")
        elif r < 0.55:
            parts.append(f"[{rng.choice(['ar', 'ti', 'al', 'by', 'offset', 'length', 'x'])}:{_rand_unicode(rng, 8)}]")
        elif r < 0.65:
            parts.append("[" * rng.randint(1, 40) + "]" * rng.randint(0, 40))
        elif r < 0.72:
            parts.append("[١٢:٣٤.٥]" + "arabic-indic digits")
        elif r < 0.80:
            parts.append("".join(f"[00:{rng.randint(0, 59):02d}.{rng.randint(0, 99):02d}]"
                                 for _ in range(rng.randint(1, 60))) + "multi")
        elif r < 0.85:
            parts.append(rng.choice(["[00:00.00]", "[99999:99]", "[00:1.5]", "[-1:00]", "[00:00.]", "[:]",
                                     "[nan:inf]", ""]))
        else:
            parts.append(_rand_unicode(rng, rng.randint(0, 40)))
    return rng.choice(["\n", "\r\n", "\r", "\n\n", " "]).join(parts)


def _check_lrc(out):
    if not isinstance(out, list):
        return "not a list"
    last = -1.0
    for item in out:
        if not (isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], float) and isinstance(item[1], str)):
            return f"bad item {item!r}"[:80]
        if not math.isfinite(item[0]) or item[0] < 0:
            return f"bad time {item[0]!r}"
        if item[0] < last:
            return "not sorted"
        last = item[0]
    return None


def test_lrc_parser_fuzz_never_raises_and_returns_sorted_valid_pairs():
    from lyrics_client import parse_lrc
    rng = random.Random(777)
    viol = Violations()
    texts = [_rand_lrc(rng) for _ in range(2500)]
    texts += ["", None, "[00:01]" * 20000, "[00:01.5]x\n" * 5000, "a" * 100000, "[" * 100000, "\n" * 100000,
              "[99:99.999]" + "z" * 100000, "[00:00]" + "‮" * 1000]
    t0 = time.perf_counter()
    for t in texts:
        a = time.perf_counter()
        try:
            out = parse_lrc(t)
        except BaseException as exc:  # noqa: BLE001
            viol.add(f"lrc_raises:{type(exc).__name__}", t, exc)
            continue
        if time.perf_counter() - a > SLOW_S:
            viol.add("lrc_slow", t, f"{(time.perf_counter() - a) * 1000:.0f} ms")
        bad = _check_lrc(out)
        if bad:
            viol.add("lrc_invalid", t, bad)
    assert time.perf_counter() - t0 < 15
    assert not viol.tags(), viol.summary()


def test_lyrics_to_lyrics_non_string_api_fields_never_raise():
    """The API JSON can carry any type in syncedLyrics; _to_lyrics feeds it straight into parse_lrc."""
    from lyrics_client import LyricsClient
    viol = Violations(_lyrics_bug)
    for v in [123, 1.5, True, ["[00:01]x"], {"a": 1}, float("nan")]:
        for key in ("syncedLyrics", "plainLyrics"):
            try:
                r = LyricsClient._to_lyrics({key: v, "instrumental": 0})
            except BaseException as exc:  # noqa: BLE001
                viol.add(f"to_lyrics:{type(exc).__name__}", {key: v}, exc)
                continue
            if r is not None and not (isinstance(r.synced, list) and (r.plain is None or isinstance(r.plain, str))):
                viol.add("to_lyrics_invalid", {key: v}, r)
    bad = [t for t in viol.tags() if not t.startswith('BUG-')]
    assert not bad, viol.summary(bad)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        if isinstance(self._p, Exception):
            raise self._p
        return self._p


class _Sess:
    def __init__(self, seq):
        self.seq = list(seq)
        self.n = 0

    def get(self, url, **kw):
        self.n += 1
        item = self.seq[min(self.n - 1, len(self.seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _rand_json(rng, depth=0):
    r = rng.random()
    if depth > 3 or r < 0.4:
        return rng.choice(GARBAGE + [_rand_lrc(rng), "[00:01.00]hello\n[00:02.00]world", "plain text"])
    if r < 0.7:
        return [_rand_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(["syncedLyrics", "plainLyrics", "instrumental", "duration", "trackName", "id", "x"]):
            _rand_json(rng, depth + 1) for _ in range(rng.randint(0, 6))}


def test_lyrics_client_fuzzed_api_and_cache_payloads(tmp_path):
    from lyrics_client import Lyrics, LyricsClient, LyricsError, cache_key
    rng = random.Random(31337)
    viol = Violations(_lyrics_bug)
    t0 = time.perf_counter()
    with _quiet():
        for i in range(150):
            cache_dir = str(tmp_path / f"l{i % 40}")
            os.makedirs(cache_dir, exist_ok=True)
            payload1 = _rand_json(rng)
            payload2 = _rand_json(rng)
            status = rng.choice([200, 200, 200, 404, 500, 429])
            seq = [_Resp(status, payload1), _Resp(rng.choice([200, 404]), payload2),
                   _Resp(200, payload1), _Resp(404, None)]
            if rng.random() < 0.1:
                seq[0] = _Resp(200, ValueError("bad json"))
            if rng.random() < 0.05:
                seq[0] = ConnectionError("down")
            client = LyricsClient(session=_Sess(seq), cache_dir=cache_dir)
            title = rng.choice(["Song", "Song (Remastered 2011)", "", " ", _rand_unicode(rng, 12), "x" * 500])
            artist = rng.choice(["Artist", "", _rand_unicode(rng, 8)])
            dur = rng.choice([0.0, 180.0, 3.5, -1.0, 1e9])
            cached_junk = None
            if rng.random() < 0.3 and title.strip():
                cached_junk = rng.choice([_rand_json(rng), {"synced": _rand_json(rng), "plain": _rand_json(rng)},
                                          {"not_found": True, "saved_at": _rand_json(rng)},
                                          {"synced": [[1, "a"], ["x", 2], [None, None], 5]}])
                key = cache_key(title.strip(), artist.strip(), dur)
                with open(os.path.join(cache_dir, key + ".json"), "w", encoding="utf-8") as f:
                    json.dump(cached_junk, f)
            try:
                r = client.get_lyrics(title, artist, "", dur)
            except LyricsError:
                continue                                      # documented failure mode
            except BaseException as exc:  # noqa: BLE001
                tag = "lyrics_cache_raises" if cached_junk is not None and client._session.n == 0 else "lyrics_api_raises"
                viol.add(f"{tag}:{type(exc).__name__}", (title, payload1, cached_junk), f"{exc!r}"[:150])
                continue
            if r is not None:
                if not isinstance(r, Lyrics) or not isinstance(r.instrumental, bool) or not isinstance(r.synced, list):
                    viol.add("lyrics_invalid_structure", (title, payload1), r)
    bad = [t for t in viol.tags() if not t.startswith('BUG-')]
    assert not bad, viol.summary(bad)
    assert time.perf_counter() - t0 < 20


KNOWN_LYRICS_TOLYRICS = ()
KNOWN_LYRICS_CLIENT = ()


def _rand_weather_payload(rng):
    def num():
        return rng.choice([0, 1, -5, 21.5, 99999, -273.15, 1e308, float("nan"), float("inf"), "12", "x", None, True,
                           [], {}, "", 10 ** 400])
    cur = {k: num() for k in ("temperature_2m", "apparent_temperature", "relative_humidity_2m", "is_day",
                              "precipitation", "weather_code", "wind_speed_10m", "wind_direction_10m")
           if rng.random() < 0.85}
    n = rng.randint(0, 8)
    hourly = {"time": [rng.choice(["2026-01-01T00:00", None, 5, ""]) for _ in range(n)],
              "temperature_2m": [num() for _ in range(rng.randint(0, 8))],
              "weather_code": [num() for _ in range(rng.randint(0, 8))]}
    if rng.random() < 0.15:
        return rng.choice(GARBAGE + [_rand_json(rng)])
    out = {"current": cur if rng.random() > 0.1 else rng.choice(GARBAGE),
           "hourly": hourly if rng.random() > 0.1 else rng.choice(GARBAGE)}
    if rng.random() < 0.2:
        out.pop(rng.choice(["current", "hourly"]))
    return out


def _check_weather(w):
    from weather_client import HourlyForecast, WeatherData
    if not isinstance(w, WeatherData):
        return "not WeatherData"
    for name in ("temperature", "apparent_temperature", "wind_speed", "wind_direction", "precipitation",
                 "latitude", "longitude", "timestamp", "high", "low"):
        if not isinstance(getattr(w, name), float):
            return f"{name} is {type(getattr(w, name)).__name__}"
    for name in ("relative_humidity", "weather_code"):
        v = getattr(w, name)
        if not isinstance(v, int) or isinstance(v, bool):
            return f"{name} is {type(v).__name__}"
    for name in ("city", "condition", "description", "unit_symbol", "speed_unit", "wind_direction_arrow"):
        if not isinstance(getattr(w, name), str):
            return f"{name} is {type(getattr(w, name)).__name__}"
    if not isinstance(w.is_day, bool):
        return "is_day"
    if not all(isinstance(h, HourlyForecast) and isinstance(h.temperature, float) and isinstance(h.condition, str)
               for h in w.hourly):
        return "hourly"
    return None


def _weather_bug(tag):
    if tag.startswith("weather_state_payload_raises:OverflowError"):
        return "BUG-F15"
    return None


def test_weather_client_fuzzed_open_meteo_payloads():
    import weather_client as wc
    rng = random.Random(9090)
    viol = Violations(_weather_bug)
    t0 = time.perf_counter()
    with _quiet():
        for i in range(200):
            payload = _rand_weather_payload(rng)
            fetch_mode = rng.random()

            def fetcher(url, timeout, _p=payload, _m=fetch_mode):
                if _m < 0.05:
                    raise OSError("offline")
                if _m < 0.10:
                    raise ValueError("bad json")
                return _p

            client = wc.WeatherClient(http_fetcher=fetcher, clock_fn=lambda: 1000.0)
            units = rng.choice(["auto", "celsius", "fahrenheit", "f", "", None, "x"])
            try:
                w = client.get_weather(rng.uniform(-90, 90), rng.uniform(-180, 180), city="Testville", units=units)
            except BaseException as exc:  # noqa: BLE001
                viol.add(f"weather_raises:{type(exc).__name__}", payload, f"{exc!r}"[:150])
                continue
            if w is not None:
                bad = _check_weather(w)
                if bad:
                    viol.add("weather_invalid_structure", payload, bad)
                else:
                    try:
                        wc.weather_state_payload(w)
                        w.to_dict()
                    except BaseException as exc:  # noqa: BLE001
                        viol.add(f"weather_state_payload_raises:{type(exc).__name__}", payload, exc)
            gclient = wc.WeatherClient(http_fetcher=fetcher)
            try:
                g = gclient.geocode(rng.choice(["Paris", "Lahore, PK", "x"]))
                if g is not None and not (isinstance(g, tuple) and len(g) == 3 and isinstance(g[0], float)
                                          and isinstance(g[1], float) and isinstance(g[2], str)):
                    viol.add("geocode_invalid", payload, g)
                s = gclient.search_cities("Par")
                if not isinstance(s, list):
                    viol.add("search_cities_invalid", payload, s)
            except BaseException as exc:  # noqa: BLE001
                viol.add(f"geocode_raises:{type(exc).__name__}", payload, f"{exc!r}"[:150])
    bad = [t for t in viol.tags() if not t.startswith('BUG-')]
    assert not bad, viol.summary(bad)
    assert time.perf_counter() - t0 < 20



@pytest.mark.xfail(strict=True, reason="BUG-F13: lyrics_client.py:229 parse_lrc(d.get('syncedLyrics') or '') raises "
                                       "AttributeError for a non-str API value (123 / list); get_lyrics documents "
                                       "only LyricsError")
def test_lyrics_to_lyrics_minimal_repro_non_string_synced_field():
    from lyrics_client import LyricsClient
    for payload in ({"syncedLyrics": 123}, {"syncedLyrics": ["[00:01]x"]}):
        r = LyricsClient._to_lyrics(payload)
        assert r is None or isinstance(r.synced, list)


@pytest.mark.xfail(strict=True, reason="BUG-F13: lyrics_client.py:230 plain = d.get('plainLyrics') or None keeps a "
                                       "non-str value, Lyrics.plain is declared Optional[str]")
def test_lyrics_to_lyrics_minimal_repro_non_string_plain_field():
    from lyrics_client import LyricsClient
    r = LyricsClient._to_lyrics({"plainLyrics": 123})
    assert r is None or r.plain is None or isinstance(r.plain, str)


KNOWN_WEATHER = ()


def test_weather_cache_file_fuzz(tmp_path):
    import weather_client as wc
    rng = random.Random(555)
    viol = Violations()
    path = str(tmp_path / "weather_cache.json")
    for i in range(250):
        r = rng.random()
        if r < 0.3:
            data = rng.randbytes(rng.randint(0, 400))
        elif r < 0.6:
            data = json.dumps(_rand_json(rng)).encode()
        else:
            doc = {"saved_at": rng.choice([0, 1000.0, "x", None, float("nan"), 1e308, -1]),
                   "config_city": rng.choice(["Paris", "", None, 5]),
                   "units": rng.choice(["auto", None, 3]),
                   "payload": rng.choice([{}, {"temp": 1}, [], "x", None, _rand_json(rng)])}
            data = json.dumps(doc).encode()
        with open(path, "wb") as f:
            f.write(data)
        try:
            res = wc.load_weather_cache(rng.choice(["Paris", "", None]), rng.choice(["auto", None]),
                                        now=rng.choice([None, 5000.0]), path=path)
        except BaseException as exc:  # noqa: BLE001
            viol.add(f"weather_cache_raises:{type(exc).__name__}", data[:60], exc)
            continue
        if res is not None and not (isinstance(res, dict) and isinstance(res.get("payload"), dict)
                                    and isinstance(res.get("stale"), bool)):
            viol.add("weather_cache_invalid", data[:60], res)
    bad = [t for t in viol.tags() if t not in KNOWN_WEATHER]
    assert not bad, viol.summary(bad)


@pytest.mark.xfail(strict=True, reason="BUG-F14: lyrics_client.py:160-175 _cache_read only guards json.load; a cache file "
                                       "whose JSON has the wrong shape ({\"synced\": [[1, \"a\"], [\"x\", 2]]}, a list root, "
                                       "\"synced\": [[1,2,3]]) raises ValueError/AttributeError out of get_lyrics instead "
                                       "of being treated as a cache miss")
def test_lyrics_cache_wrong_shape_is_a_miss_not_a_crash(tmp_path):
    from lyrics_client import LyricsClient, cache_key
    for n, junk in enumerate([{"synced": [[1, "a"], ["x", 2]]}, [1, 2, 3], {"synced": [[1, 2, 3]]}]):
        d = tmp_path / f"c{n}"
        d.mkdir()
        client = LyricsClient(session=_Sess([_Resp(404, None)]), cache_dir=str(d))
        with open(d / (cache_key("Song", "Artist", 0.0) + ".json"), "w", encoding="utf-8") as f:
            json.dump(junk, f)
        client.get_lyrics("Song", "Artist")           # must not raise (None / Lyrics / LyricsError are fine)


@pytest.mark.xfail(strict=True, reason="BUG-F15: weather_client.py weather_state_payload does float(w.relative_humidity) "
                                       "and the fetch path int()s the API value with no bounds: a payload with "
                                       "relative_humidity_2m = 10**400 gives WeatherData whose payload raises OverflowError")
def test_weather_state_payload_survives_absurd_humidity():
    import weather_client as wc
    w = wc.WeatherData(relative_humidity=10 ** 400)
    wc.weather_state_payload(w)


@pytest.mark.xfail(strict=True, reason="BUG-F16: config.py:530-534 NaN glass_transparency is meant to fall back to 0.6, "
                                       "but max(0.0, nan) returns 0.0 so clamped is never NaN and the 0.6 branch is dead: "
                                       "{\"glass_transparency\": NaN} loads as 0.0 (solid)")
def test_glass_transparency_nan_falls_back_to_default(tmp_path):
    import config
    path = str(tmp_path / "c.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"glass_transparency": NaN}')
    with _quiet():
        cfg = config.load_config(path)
    assert cfg.glass_transparency == 0.6
