"""Local personal store: profile, corrections, voice clips and usage habits.

Everything lives under %LOCALAPPDATA%\\JARVIS\\profile\\ (override with the JARVIS_PROFILE_DIR environment
variable). Nothing in here is ever sent anywhere. Each writer is a no-op unless its learn_* flag is on
(configure(cfg) reads them; before configure() every flag is off so tests and tools never write by accident).

Pure Python, no Qt. Thread-safe (one re-entrant lock); JSON is written atomically (temp file + os.replace);
every collection and the clip store are size capped.
"""
from __future__ import annotations

import collections
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import threading
import time
import wave
from typing import Any, Dict, List, Optional

import jarvis_log as _jlog

_log = _jlog.get_logger("personal")

MAX_CLIPS = 2000
MAX_CLIP_BYTES = 500 * 1024 * 1024
MAX_CORRECTIONS = 500
MAX_EVENTS = 1000
MAX_LIST_ITEMS = 50
MAX_STR = 80
APPS_CACHE_TTL_S = 600.0

_DEFAULT_WAKE = ("jarvis", "hey jarvis", "ok jarvis", "okay jarvis", "javis", "jarvis please")
ADDRESS_CHOICES = ("sir", "ma'am", "name")
_LIST_FIELDS = ("uses", "favorite_apps", "games")
_STR_FIELDS = ("name", "address_as", "browser", "search_engine")

_lock = threading.RLock()
_flags = {"profile": False, "corrections": False, "voice_clips": False, "habits": False}
_wake_aliases = set(_DEFAULT_WAKE)

_profile_cache: Optional[dict] = None
_corr_cache: Optional[list] = None
_habits_cache: Optional[dict] = None
_clip_index: Optional[list] = None     # sorted [[stem, bytes], ...] oldest first
_clip_seq = 0
_apps_cache: Dict[str, Any] = {"at": 0.0, "apps": None}


# ------------------------------------------------------------------ paths / io
def data_dir() -> str:
    override = os.environ.get("JARVIS_PROFILE_DIR")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JARVIS", "profile")


def _path(name: str) -> str:
    return os.path.join(data_dir(), name)


def _clips_dir() -> str:
    return _path("clips")


def _atomic_write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        _log.error("could not read %s: %s", os.path.basename(path), exc)
        return default


def configure(cfg) -> None:
    """Read the learn_* flags and the wake aliases from the config."""
    global _wake_aliases
    with _lock:
        _flags["profile"] = bool(getattr(cfg, "learn_profile", False))
        _flags["corrections"] = bool(getattr(cfg, "learn_corrections", False))
        _flags["voice_clips"] = bool(getattr(cfg, "learn_voice_clips", False))
        _flags["habits"] = bool(getattr(cfg, "learn_habits", False))
        aliases = getattr(cfg, "wake_aliases", None) or []
        wake = getattr(cfg, "wake_word", None)
        _wake_aliases = {str(a).strip().lower() for a in list(aliases) + [wake] if a} | set(_DEFAULT_WAKE)
    _log.info("personal configured: %s", {k: v for k, v in _flags.items()})


def flag(name: str) -> bool:
    return bool(_flags.get(name))


def _reset_caches() -> None:
    global _profile_cache, _corr_cache, _habits_cache, _clip_index
    _profile_cache = _corr_cache = _habits_cache = _clip_index = None


def _wake_tokens() -> set:
    toks = set()
    for a in _wake_aliases:
        toks.update(a.split())
    return toks - {"hey", "ok", "okay", "please"}


def is_wake_phrase(text: str) -> bool:
    return " ".join(str(text or "").lower().split()) in _wake_aliases


def _clean_str(v, cap=MAX_STR) -> str:
    return " ".join(str(v or "").split())[:cap]


# ------------------------------------------------------------------ profile
def _blank_profile() -> dict:
    return {"name": "", "address_as": "sir", "uses": [], "favorite_apps": [], "games": [],
            "browser": "", "search_engine": "", "extra": {}}


def _load_profile() -> dict:
    global _profile_cache
    if _profile_cache is None:
        data = _read_json(_path("profile.json"), {})
        prof = _blank_profile()
        if isinstance(data, dict):
            for k in _STR_FIELDS:
                if isinstance(data.get(k), str):
                    prof[k] = data[k]
            for k in _LIST_FIELDS:
                if isinstance(data.get(k), list):
                    prof[k] = [str(x) for x in data[k]][:MAX_LIST_ITEMS]
            if isinstance(data.get("extra"), dict):
                prof["extra"] = dict(data["extra"])
        if prof["address_as"] not in ADDRESS_CHOICES:
            prof["address_as"] = "sir"
        _profile_cache = prof
    return _profile_cache


def get_profile() -> dict:
    with _lock:
        p = _load_profile()
        return {**p, "uses": list(p["uses"]), "favorite_apps": list(p["favorite_apps"]),
                "games": list(p["games"]), "extra": dict(p["extra"])}


def _dedupe(items) -> list:
    seen, out = set(), []
    for x in items:
        s = _clean_str(x)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:MAX_LIST_ITEMS]


def update_profile(**fields) -> bool:
    """Set profile fields. Lists replace; extra merges. Unknown keys are ignored. No-op when learn_profile is off."""
    if not _flags["profile"]:
        return False
    with _lock:
        p = _load_profile()
        for k, v in fields.items():
            if k in _STR_FIELDS:
                s = _clean_str(v)
                if k == "address_as":
                    s = s.lower()
                    if s not in ADDRESS_CHOICES:
                        _log.warning("ignored invalid address_as")
                        continue
                p[k] = s
            elif k in _LIST_FIELDS:
                p[k] = _dedupe(v if isinstance(v, (list, tuple)) else [v])
            elif k == "extra" and isinstance(v, dict):
                p["extra"].update({_clean_str(a, 40): b for a, b in v.items()})
            else:
                _log.warning("ignored unknown profile field %r", k)
        _atomic_write_json(_path("profile.json"), p)
    return True


def add_to_profile(field_name: str, value: str) -> bool:
    """Append one value to a list field (uses / favorite_apps / games)."""
    if field_name not in _LIST_FIELDS:
        return False
    with _lock:
        cur = _load_profile()[field_name]
        return update_profile(**{field_name: list(cur) + [value]})


def address_word() -> str:
    """The word to address the user by: sir, ma'am or their name. Defaults to 'sir'."""
    if not _flags["profile"]:
        return "sir"
    p = get_profile()
    if p["address_as"] == "name" and p["name"]:
        return p["name"]
    return "ma'am" if p["address_as"] == "ma'am" else "sir"


def profile_signature() -> str:
    """Short stable token for what changes spoken answers (name + address); '' when nothing is set."""
    if not _flags["profile"]:
        return ""
    p = get_profile()
    if p["address_as"] == "sir" and not p["name"]:
        return ""
    return hashlib.sha1(f"{p['name']}|{p['address_as']}".encode("utf-8")).hexdigest()[:8]


def profile_prompt() -> str:
    """At most 400 chars of context for the LLM system prompt ('' when nothing is known / learning is off)."""
    if not _flags["profile"]:
        return ""
    p = get_profile()
    parts = []
    if p["name"]:
        parts.append(f"The user is {p['name']}")
    addr = address_word()
    parts.append(f"address them as {addr}")
    if p["games"]:
        parts.append("plays " + ", ".join(p["games"][:5]))
    if p["favorite_apps"]:
        parts.append("favorite apps: " + ", ".join(p["favorite_apps"][:5]))
    if p["uses"]:
        parts.append("uses the PC for " + ", ".join(p["uses"][:4]))
    if p["browser"]:
        parts.append(f"browser {p['browser']}")
    if p["search_engine"]:
        parts.append(f"prefers {p['search_engine']} search")
    if len(parts) == 1 and not p["name"] and p["address_as"] == "sir":
        return ""
    text = "; ".join(parts) + "."
    if not p["name"]:
        text = text[0].upper() + text[1:]
    return text[:400]


# ------------------------------------------------------------------ corrections
def _load_corr() -> list:
    global _corr_cache
    if _corr_cache is None:
        data = _read_json(_path("corrections.json"), [])
        _corr_cache = [d for d in data if isinstance(d, dict) and d.get("heard") and d.get("meant")] \
            if isinstance(data, list) else []
    return _corr_cache


def add_correction(heard: str, meant: str) -> bool:
    heard, meant = _clean_str(heard).lower(), _clean_str(meant)
    if not _flags["corrections"] or not heard or not meant or heard == meant.lower():
        return False
    if is_wake_phrase(heard) or set(heard.split()) & _wake_tokens():
        _log.info("refused a correction that involves a wake word")
        return False
    with _lock:
        rows = _load_corr()
        for r in rows:
            if r["heard"] == heard:
                r["meant"], r["count"], r["ts"] = meant, int(r.get("count", 1)) + 1, time.time()
                break
        else:
            rows.append({"heard": heard, "meant": meant, "count": 1, "ts": time.time()})
            del rows[:-MAX_CORRECTIONS]
        _atomic_write_json(_path("corrections.json"), rows)
    _log.info("correction stored (%d total)", len(rows))
    return True


def list_corrections() -> List[dict]:
    with _lock:
        return [dict(r) for r in _load_corr()]


def remove_correction(heard: str) -> bool:
    heard = _clean_str(heard).lower()
    with _lock:
        rows = _load_corr()
        keep = [r for r in rows if r["heard"] != heard]
        if len(keep) == len(rows):
            return False
        rows[:] = keep
        _atomic_write_json(_path("corrections.json"), rows)
    return True


def apply_corrections(text: str) -> str:
    """Replace known misheard words/phrases (case-insensitive, whole-word) with what the user meant.
    Wake aliases are masked first so a correction can never alter them."""
    if not text or not _flags["corrections"]:
        return text
    with _lock:
        rows = sorted(_load_corr(), key=lambda r: -len(r["heard"]))
        wake = sorted(_wake_aliases | _wake_tokens(), key=len, reverse=True)
    if not rows:
        return text
    masks: List[str] = []

    def _mask(m):
        masks.append(m.group(0))
        return f"\x00{len(masks) - 1}\x00"

    out = text
    if wake:
        out = re.sub(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in wake) + r")(?!\w)", _mask, out, flags=re.I)
    for r in rows:
        out = re.sub(r"(?<!\w)" + re.escape(r["heard"]).replace(r"\ ", r"\s+") + r"(?!\w)",
                     lambda _m, _r=r: _r["meant"], out, flags=re.I)
    return re.sub(r"\x00(\d+)\x00", lambda m: masks[int(m.group(1))], out)


# ------------------------------------------------------------------ vocabulary
_STOP_CAPS = {"i", "i'm", "i'll", "i've", "the", "a", "an", "ok", "okay", "hey"}


def vocabulary(limit: int = 50) -> List[str]:
    """Words worth biasing speech recognition toward. Never includes wake aliases."""
    with _lock:
        p = _load_profile() if _flags["profile"] else _blank_profile()
        corr = _load_corr() if _flags["corrections"] else []
        hab = _load_habits() if _flags["habits"] else {"proper": {}}
        cands: List[str] = []
        cands += p["games"] + p["favorite_apps"]
        if p["name"]:
            cands.append(p["name"])
        cands += [r["meant"] for r in sorted(corr, key=lambda r: -r.get("count", 1))]
        proper = hab.get("proper", {})
        cands += [w for w, _n in sorted(proper.items(), key=lambda kv: -kv[1])]
        wake, toks = set(_wake_aliases), _wake_tokens()
    out, seen = [], set()
    for c in cands:
        s = _clean_str(c)
        k = s.lower()
        if not s or k in seen or k in wake or set(k.split()) & toks:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= max(0, int(limit)):
            break
    return out


# ------------------------------------------------------------------ habits
def _load_habits() -> dict:
    global _habits_cache
    if _habits_cache is None:
        data = _read_json(_path("habits.json"), {})
        h = {"events": [], "counts": {}, "proper": {}}
        if isinstance(data, dict):
            for k in h:
                if isinstance(data.get(k), type(h[k])):
                    h[k] = data[k]
        _habits_cache = h
    return _habits_cache


_OPEN_RE = re.compile(r"^(?:please\s+)?(?:open|launch|start|run|play)\s+(?:up\s+)?(?:the\s+)?(.+)$", re.I)


def _habit_target(text: str, intent: str) -> str:
    if intent not in ("open_app", "launch_app", "open_game", "launch_game", "play_game"):
        return ""
    m = _OPEN_RE.match(str(text or "").strip(" .!?"))
    return _clean_str(m.group(1), 40).lower() if m else ""


def log_command(text: str, intent: str, ts: Optional[float] = None) -> bool:
    """Record one dispatched command (intent + short text) for habits and vocabulary."""
    if not _flags["habits"] or not str(text or "").strip():
        return False
    ts = time.time() if ts is None else float(ts)
    intent = _clean_str(intent, 40) or "unknown"
    text = _clean_str(text, 80)
    with _lock:
        h = _load_habits()
        h["events"].append({"t": round(ts, 1), "i": intent, "x": _habit_target(text, intent)})
        del h["events"][:-MAX_EVENTS]
        h["counts"][intent] = h["counts"].get(intent, 0) + 1
        words = text.split()
        wake = _wake_tokens()
        for w in words[1:]:
            wn = w.strip(".,!?;:\"'")
            if len(wn) > 2 and wn[0].isupper() and wn.lower() not in _STOP_CAPS and wn.lower() not in wake:
                h["proper"][wn] = h["proper"].get(wn, 0) + 1
        if len(h["proper"]) > 300:
            h["proper"] = dict(sorted(h["proper"].items(), key=lambda kv: -kv[1])[:200])
        _atomic_write_json(_path("habits.json"), h)
    return True


def habits_summary() -> dict:
    with _lock:
        h = _load_habits()
        ev = list(h["events"])
        counts = dict(h["counts"])
    by_hour = collections.Counter(datetime.datetime.fromtimestamp(e["t"]).hour for e in ev)
    apps = collections.Counter(e["x"] for e in ev if e.get("x"))
    return {
        "total": sum(counts.values()),
        "events": len(ev),
        "top_intents": sorted(counts.items(), key=lambda kv: -kv[1])[:8],
        "top_apps": apps.most_common(8),
        "by_hour": dict(sorted(by_hour.items())),
    }


def suggestions(now: Optional[float] = None) -> List[str]:
    """Things the user usually does around this hour (same weekday/weekend class, on at least 3 distinct days)."""
    if not _flags["habits"]:
        return []
    now_dt = datetime.datetime.fromtimestamp(time.time() if now is None else float(now))
    weekend = now_dt.weekday() >= 5
    days: Dict[str, set] = collections.defaultdict(set)
    with _lock:
        events = list(_load_habits()["events"])
    for e in events:
        tgt = e.get("x")
        if not tgt:
            continue
        d = datetime.datetime.fromtimestamp(e["t"])
        if (d.weekday() >= 5) != weekend:
            continue
        if abs((d.hour * 60 + d.minute) - (now_dt.hour * 60 + now_dt.minute)) <= 60:
            days[tgt].add(d.date().isoformat())
    ranked = sorted(((len(v), k) for k, v in days.items() if len(v) >= 3), reverse=True)
    return [f"You usually open {k.title()} around now." for _n, k in ranked[:3]]


def clear_habits() -> None:
    global _habits_cache
    with _lock:
        _habits_cache = None
        try:
            os.remove(_path("habits.json"))
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.error("could not clear habits: %s", exc)


# ------------------------------------------------------------------ voice clips
def _load_clip_index() -> list:
    global _clip_index, _clip_seq
    if _clip_index is None:
        idx = []
        for p in glob.glob(os.path.join(_clips_dir(), "*.wav")):
            try:
                size = os.path.getsize(p)
                js = p[:-4] + ".json"
                if os.path.exists(js):
                    size += os.path.getsize(js)
                idx.append([os.path.basename(p)[:-4], size])
            except OSError as exc:
                _log.error("clip stat failed: %s", exc)
        idx.sort()
        _clip_index = idx
    return _clip_index


def save_voice_clip(pcm16: bytes, sample_rate: int, transcript: str, meta: Optional[dict] = None) -> Optional[str]:
    """Store one command clip as WAV plus a JSON sidecar; evicts the oldest past the caps. Returns the path."""
    global _clip_seq
    if not _flags["voice_clips"] or not pcm16:
        return None
    pcm16 = bytes(pcm16)[: len(pcm16) - (len(pcm16) % 2)]
    if not pcm16:
        return None
    with _lock:
        idx = _load_clip_index()
        _clip_seq += 1
        stem = f"{int(time.time() * 1000):013d}_{_clip_seq:04d}"
        os.makedirs(_clips_dir(), exist_ok=True)
        wav_path = os.path.join(_clips_dir(), stem + ".wav")
        js_path = os.path.join(_clips_dir(), stem + ".json")
        try:
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(int(sample_rate))
                w.writeframes(pcm16)
            side = {"transcript": str(transcript or "")[:500], "sample_rate": int(sample_rate),
                    "ts": time.time(), "meta": dict(meta or {})}
            _atomic_write_json(js_path, side)
        except (OSError, ValueError, wave.Error) as exc:
            _log.error("voice clip save failed: %s", exc)
            for p in (wav_path, js_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return None
        idx.append([stem, os.path.getsize(wav_path) + os.path.getsize(js_path)])
        _evict_clips_locked(idx)
    return wav_path


def _evict_clips_locked(idx: list) -> None:
    total = sum(b for _s, b in idx)
    removed = 0
    while idx and (len(idx) > MAX_CLIPS or total > MAX_CLIP_BYTES):
        stem, size = idx.pop(0)
        total -= size
        removed += 1
        for ext in (".wav", ".json"):
            try:
                os.remove(os.path.join(_clips_dir(), stem + ext))
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.error("clip evict failed: %s", exc)
    if removed:
        _log.info("evicted %d old voice clips", removed)


def clip_stats() -> dict:
    with _lock:
        idx = _load_clip_index()
        return {"count": len(idx), "bytes": sum(b for _s, b in idx)}


def delete_voice_clips() -> int:
    global _clip_index
    with _lock:
        n = len(_load_clip_index())
        shutil.rmtree(_clips_dir(), ignore_errors=True)
        _clip_index = []
    return n


# ------------------------------------------------------------------ installed apps
def _start_menu_dirs() -> List[str]:
    out = []
    for env, sub in (("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                     ("PROGRAMDATA", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base:
            out.append(os.path.join(base, sub))
    return out


def _steam_roots() -> List[str]:
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.append(winreg.QueryValueEx(k, "SteamPath")[0])
    except (ImportError, OSError):
        pass
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        base = os.environ.get(env)
        if base:
            roots.append(os.path.join(base, "Steam"))
    return [os.path.normpath(r) for r in dict.fromkeys(roots) if os.path.isdir(r)]


def _epic_manifest_dir() -> str:
    return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                        "Epic", "EpicGamesLauncher", "Data", "Manifests")


_VDF_KV = re.compile(r'"([^"]+)"\s+"([^"]*)"')
_SKIP_STEAM = ("steamworks common redistributables", "proton", "steam linux runtime", "steamvr")
_SKIP_LNK = ("uninstall", "readme", "help", "documentation", "release notes", "license", "website", "manual")


def _scan_start_menu() -> List[dict]:
    out = []
    for d in _start_menu_dirs():
        for dirpath, _dirs, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(".lnk"):
                    name = fn[:-4]
                    if not any(s in name.lower() for s in _SKIP_LNK):
                        out.append({"name": name, "path": os.path.join(dirpath, fn), "kind": "app"})
    return out


def _scan_steam() -> List[dict]:
    out = []
    for root in _steam_roots():
        libs = [root]
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                libs += [v.replace("\\\\", "\\") for k, v in _VDF_KV.findall(f.read()) if k == "path"]
        except OSError as exc:
            _log.info("steam libraryfolders unreadable: %s", exc)
        for lib in dict.fromkeys(os.path.normpath(x) for x in libs):
            for mf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
                try:
                    with open(mf, "r", encoding="utf-8", errors="replace") as f:
                        kv = dict(_VDF_KV.findall(f.read()))
                except OSError as exc:
                    _log.error("steam manifest unreadable: %s", exc)
                    continue
                name, appid = kv.get("name", ""), kv.get("appid", "")
                if name and appid and not any(s in name.lower() for s in _SKIP_STEAM):
                    out.append({"name": name, "path": f"steam://rungameid/{appid}", "kind": "game"})
    return out


def _scan_epic() -> List[dict]:
    out = []
    for mf in glob.glob(os.path.join(_epic_manifest_dir(), "*.item")):
        try:
            with open(mf, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as exc:
            _log.error("epic manifest unreadable: %s", exc)
            continue
        name = d.get("DisplayName")
        if name and d.get("InstallLocation") and d.get("LaunchExecutable"):
            out.append({"name": name, "kind": "game",
                        "path": os.path.join(d["InstallLocation"], d["LaunchExecutable"])})
    return out


def installed_apps(limit: int = 300, refresh: bool = False) -> List[dict]:
    """Installed games first, then Start Menu apps. Cached for 10 minutes; Xbox/Store apps are not scanned."""
    with _lock:
        if (refresh or _apps_cache["apps"] is None
                or time.monotonic() - _apps_cache["at"] > APPS_CACHE_TTL_S):
            t0 = time.perf_counter()
            seen, apps = set(), []
            for a in _scan_steam() + _scan_epic() + _scan_start_menu():
                k = a["name"].lower()
                if k not in seen:
                    seen.add(k)
                    apps.append(a)
            apps.sort(key=lambda a: (a["kind"] != "game", a["name"].lower()))
            _apps_cache["apps"], _apps_cache["at"] = apps, time.monotonic()
            _log.info("installed apps scanned: %d in %.0f ms", len(apps), (time.perf_counter() - t0) * 1000)
        return [dict(a) for a in _apps_cache["apps"][: max(0, int(limit))]]


# ------------------------------------------------------------------ summary / delete
def _dir_bytes(path: str) -> int:
    total = 0
    for dp, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def data_summary() -> dict:
    with _lock:
        hab = habits_summary()
        return {
            "dir": data_dir(),
            "flags": dict(_flags),
            "profile": get_profile(),
            "corrections": len(_load_corr()),
            "clips": clip_stats(),
            "habit_events": hab["events"],
            "bytes": _dir_bytes(data_dir()) if os.path.isdir(data_dir()) else 0,
        }


def delete_all() -> None:
    """Erase everything personal (profile, corrections, clips, habits)."""
    with _lock:
        shutil.rmtree(data_dir(), ignore_errors=True)
        _reset_caches()
        _apps_cache["apps"] = None
    _log.info("all personal data deleted")
