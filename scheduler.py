"""
scheduler.py — Local Scheduler for J.A.R.V.I.S.

Provides:
- Persistent Timers, Alarms, and Reminders stored in a JSON file (survives restart).
- Background worker ticking active items and feeding active timer status to Dynamic Island and context overlay.
- Audio chime and toast notifications upon expiration.
- Voice actions:
    - "set timer for <X> minutes"
    - "cancel timer"
    - "time left on timer"
    - "set alarm for <time>"
    - "remind me to <task> in <X> minutes"
- 100% unit-testable behind interfaces; strictly NO Qt imports.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime
import json
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class ScheduledItem:
    id: str
    item_type: str  # "timer", "alarm", "reminder"
    label: str
    duration_sec: float
    target_time: float  # Unix epoch timestamp
    created_at: float  # Unix epoch timestamp
    is_active: bool = True
    has_finished: bool = False
    is_paused: bool = False
    text: str = ""  # Message/text for reminders

    def remaining_seconds(self, now: Optional[float] = None) -> float:
        current = time.time() if now is None else now
        if not self.is_active or self.has_finished:
            return 0.0
        return max(0.0, self.target_time - current)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScheduledItem":
        return cls(
            id=str(d.get("id", "")),
            item_type=str(d.get("item_type", "timer")),
            label=str(d.get("label", "Timer")),
            duration_sec=float(d.get("duration_sec", 0.0)),
            target_time=float(d.get("target_time", 0.0)),
            created_at=float(d.get("created_at", 0.0)),
            is_active=bool(d.get("is_active", True)),
            has_finished=bool(d.get("has_finished", False)),
            is_paused=bool(d.get("is_paused", False)),
            text=str(d.get("text", "")),
        )


def get_default_scheduler_path() -> str:
    """Returns default path for persistent scheduler storage."""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    jarvis_dir = os.path.join(appdata, "Jarvis")
    try:
        os.makedirs(jarvis_dir, exist_ok=True)
    except Exception:
        pass
    return os.path.join(jarvis_dir, "scheduler.json")


# Natural language numbers mapping
_WORD_NUMBERS: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "a": 1, "an": 1, "half": 30, "quarter": 15,
}


def parse_duration_seconds(text: str) -> Optional[float]:
    """
    Parses natural language duration into total seconds.
    Supports formats:
      - "5 minutes"
      - "10 minutes and 30 seconds"
      - "1 hour 15 minutes"
      - "half an hour" -> 1800s
      - "quarter hour" / "quarter of an hour" -> 900s
      - "45 seconds"
      - "2.5 minutes"
      - "two minutes"
    """
    clean = text.lower().strip()

    # Pre-replace fractions
    if "half an hour" in clean or "half hour" in clean:
        clean = clean.replace("half an hour", "30 minutes").replace("half hour", "30 minutes")
    if "quarter of an hour" in clean or "quarter hour" in clean:
        clean = clean.replace("quarter of an hour", "15 minutes").replace("quarter hour", "15 minutes")

    # Word numbers replacement
    for word, num in sorted(_WORD_NUMBERS.items(), key=lambda x: len(x[0]), reverse=True):
        clean = re.sub(rf"\b{re.escape(word)}\b", str(num), clean)

    total_sec = 0.0
    matched = False

    # Hours
    m_h = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", clean)
    if m_h:
        total_sec += float(m_h.group(1)) * 3600.0
        matched = True

    # Minutes
    m_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b", clean)
    if m_m:
        total_sec += float(m_m.group(1)) * 60.0
        matched = True

    # Seconds
    m_s = re.search(r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", clean)
    if m_s:
        total_sec += float(m_s.group(1))
        matched = True

    # Fallback: "timer for 10" -> defaults to minutes if unit omitted
    if not matched:
        m_bare = re.search(r"\b(?:for\s+)?(\d+(?:\.\d+)?)\b", clean)
        if m_bare:
            total_sec = float(m_bare.group(1)) * 60.0
            matched = True

    return total_sec if matched and total_sec > 0 else None


def format_duration(seconds: float) -> str:
    """Formats seconds into human-readable string (e.g. '5 minutes', '1 hour 20 minutes', '45 seconds')."""
    total = int(round(seconds))
    if total <= 0:
        return "0 seconds"

    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours} hour" if hours == 1 else f"{hours} hours")
    if minutes > 0:
        parts.append(f"{minutes} minute" if minutes == 1 else f"{minutes} minutes")
    if secs > 0 and hours == 0:
        parts.append(f"{secs} second" if secs == 1 else f"{secs} seconds")

    return " and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + ", and " + parts[-1]


class LocalScheduler:
    """
    Persistent local scheduler for timers, alarms, and reminders.
    Thread-safe, decoupled from UI, and fully unit-testable.
    """

    def __init__(
        self,
        storage_path: Optional[str] = None,
        clock_fn: Callable[[], float] = time.time,
        chime_fn: Optional[Callable[[], None]] = None,
        notify_fn: Optional[Callable[[ScheduledItem], None]] = None,
        ctx: Any = None,
    ):
        self._storage_path = storage_path or get_default_scheduler_path()
        self._clock = clock_fn
        self._chime_fn = chime_fn
        self._notify_fn = notify_fn
        self._ctx = ctx

        self._lock = threading.Lock()
        self._items: Dict[str, ScheduledItem] = {}
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._item_counter = 0

        self._load()

    @property
    def storage_path(self) -> str:
        return self._storage_path

    def _generate_id(self, item_type: str) -> str:
        self._item_counter += 1
        return f"{item_type}_{int(self._clock())}_{self._item_counter}"

    def _load(self) -> None:
        """Loads scheduled items from JSON storage."""
        if not os.path.exists(self._storage_path):
            return
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                with self._lock:
                    self._items.clear()
                    now = self._clock()
                    for entry in data:
                        item = ScheduledItem.from_dict(entry)
                        # Check expired while shut down
                        if item.is_active and not item.has_finished and item.target_time <= now:
                            item.has_finished = True
                            item.is_active = False
                        self._items[item.id] = item
        except Exception as exc:
            print(f"[scheduler] load note: {exc}")

    def _save(self) -> bool:
        """Saves active and recent scheduled items to JSON storage."""
        try:
            folder = os.path.dirname(self._storage_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with self._lock:
                # Keep active items plus finished items from the last 2 hours
                now = self._clock()
                cutoff = now - 7200.0
                to_save = [
                    item.to_dict()
                    for item in self._items.values()
                    if item.is_active or item.target_time >= cutoff
                ]
            temp_path = self._storage_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2)
            os.replace(temp_path, self._storage_path)
            return True
        except Exception as exc:
            print(f"[scheduler] save error: {exc}")
            return False

    def set_timer(
        self,
        duration_sec: float,
        label: str = "Timer",
        start_now: Optional[float] = None,
    ) -> ScheduledItem:
        """Sets a countdown timer for duration_sec seconds."""
        now = self._clock() if start_now is None else start_now
        duration_sec = max(1.0, float(duration_sec))
        item = ScheduledItem(
            id=self._generate_id("timer"),
            item_type="timer",
            label=label or "Timer",
            duration_sec=duration_sec,
            target_time=now + duration_sec,
            created_at=now,
            is_active=True,
            has_finished=False,
        )
        with self._lock:
            # If replacing an active timer with same label, cancel older
            self._items[item.id] = item
        self._save()
        self._feed_island()
        return item

    def cancel_timer(self, label_or_id: Optional[str] = None) -> bool:
        """Cancels active timer. If label_or_id is None, cancels the latest active timer."""
        found = False
        with self._lock:
            active_timers = [
                item for item in self._items.values()
                if item.item_type == "timer" and item.is_active and not item.has_finished
            ]
            if not active_timers:
                return False

            if label_or_id:
                clean = label_or_id.lower().strip()
                for t in active_timers:
                    if t.id == label_or_id or t.label.lower() == clean or clean in t.label.lower():
                        t.is_active = False
                        found = True
            else:
                # Cancel the most recently created or nearest expiration
                latest = active_timers[-1]
                latest.is_active = False
                found = True

        if found:
            self._save()
            self._feed_island()
        return found

    def get_active_timer(self) -> Optional[ScheduledItem]:
        """Returns the primary active countdown timer, if any."""
        with self._lock:
            active = [
                item for item in self._items.values()
                if item.item_type == "timer" and item.is_active and not item.has_finished
            ]
            if not active:
                return None
            # Return timer closest to finishing
            return min(active, key=lambda t: t.target_time)

    def get_time_left(self, label_or_id: Optional[str] = None, now: Optional[float] = None) -> Optional[float]:
        """Returns remaining seconds for the active timer, or None if none running."""
        current = self._clock() if now is None else now
        with self._lock:
            if label_or_id:
                clean = label_or_id.lower().strip()
                for item in self._items.values():
                    if item.is_active and not item.has_finished and (item.id == label_or_id or item.label.lower() == clean):
                        return max(0.0, item.target_time - current)
            else:
                active = [
                    item for item in self._items.values()
                    if item.item_type == "timer" and item.is_active and not item.has_finished
                ]
                if active:
                    nearest = min(active, key=lambda t: t.target_time)
                    return max(0.0, nearest.target_time - current)
        return None

    def set_alarm(
        self,
        target_time: float,
        label: str = "Alarm",
    ) -> ScheduledItem:
        """Sets an alarm for a specific unix timestamp."""
        now = self._clock()
        item = ScheduledItem(
            id=self._generate_id("alarm"),
            item_type="alarm",
            label=label or "Alarm",
            duration_sec=max(0.0, target_time - now),
            target_time=target_time,
            created_at=now,
            is_active=True,
            has_finished=False,
        )
        with self._lock:
            self._items[item.id] = item
        self._save()
        return item

    def set_reminder(
        self,
        target_time: float,
        text: str,
        label: str = "Reminder",
    ) -> ScheduledItem:
        """Sets a timed reminder for a specific unix timestamp."""
        now = self._clock()
        item = ScheduledItem(
            id=self._generate_id("reminder"),
            item_type="reminder",
            label=label or "Reminder",
            duration_sec=max(0.0, target_time - now),
            target_time=target_time,
            created_at=now,
            is_active=True,
            has_finished=False,
            text=text,
        )
        with self._lock:
            self._items[item.id] = item
        self._save()
        return item

    def list_items(self, active_only: bool = True) -> List[ScheduledItem]:
        """Returns all scheduled items."""
        with self._lock:
            if active_only:
                return [item for item in self._items.values() if item.is_active and not item.has_finished]
            return list(self._items.values())

    def tick(self, now: Optional[float] = None) -> List[ScheduledItem]:
        """
        Processes item expirations for current timestamp.
        Returns list of newly expired ScheduledItem objects.
        """
        current = self._clock() if now is None else now
        expired: List[ScheduledItem] = []

        with self._lock:
            for item in self._items.values():
                if item.is_active and not item.has_finished and item.target_time <= current:
                    item.is_active = False
                    item.has_finished = True
                    expired.append(item)

        if expired:
            self._save()
            for exp in expired:
                self._handle_expiration(exp)

        self._feed_island(current)
        return expired

    def _handle_expiration(self, item: ScheduledItem) -> None:
        """Triggers audio chime, toast notification, and spoken reply."""
        # 1. Chime
        if self._chime_fn:
            try:
                self._chime_fn()
            except Exception:
                pass
        else:
            try:
                from chime import play_chime
                play_chime()
            except Exception:
                pass

        # 2. Notification hook
        if self._notify_fn:
            try:
                self._notify_fn(item)
            except Exception:
                pass

        # 3. Context overlay notification / speech
        if self._ctx:
            title = f"{item.item_type.title()} Complete"
            msg = f"{item.label} ({format_duration(item.duration_sec)})" if item.item_type == "timer" else (item.text or item.label)
            if hasattr(self._ctx, "overlay") and self._ctx.overlay:
                try:
                    self._ctx.overlay.post("result", text=f"{title}: {msg}", icon="timer", duration=5.0)
                except Exception:
                    pass
            if hasattr(self._ctx, "say"):
                try:
                    if item.item_type == "timer":
                        self._ctx.say(f"Sir, your timer for {item.label} has completed.")
                    elif item.item_type == "alarm":
                        self._ctx.say(f"Sir, your alarm for {item.label} is ringing.")
                    elif item.item_type == "reminder":
                        self._ctx.say(f"Sir, reminder: {item.text or item.label}.")
                except Exception:
                    pass

    def _feed_island(self, now: Optional[float] = None) -> None:
        """Feeds active timer status to island arbitrator and overlay."""
        current = self._clock() if now is None else now
        active_timer = self.get_active_timer()

        # Update Island Arbitrator
        try:
            from island_arbitrator import get_island_arbitrator
            arbitrator = get_island_arbitrator()
            if active_timer:
                rem = max(0.0, active_timer.target_time - current)
                arbitrator.update_timer(
                    is_active=True,
                    remaining_seconds=rem,
                    total_seconds=active_timer.duration_sec,
                    label=active_timer.label,
                    is_paused=active_timer.is_paused,
                    has_finished=active_timer.has_finished,
                )
            else:
                arbitrator.update_timer(is_active=False)
        except Exception:
            pass

        # Update Overlay timer_state
        if self._ctx and hasattr(self._ctx, "overlay") and self._ctx.overlay:
            try:
                if active_timer:
                    self._ctx.overlay.post(
                        "timer_state",
                        active=True,
                        duration=active_timer.duration_sec,
                        end_time=active_timer.target_time,
                    )
                else:
                    self._ctx.overlay.post("timer_state", active=False)
            except Exception:
                pass

    def start(self, interval_sec: float = 0.5) -> None:
        """Starts background tick worker."""
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()

        def _worker():
            while not self._stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    print(f"[scheduler] worker tick error: {exc}")
                self._stop_event.wait(interval_sec)

        self._worker_thread = threading.Thread(target=_worker, name="SchedulerWorker", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        """Stops background tick worker cleanly."""
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
            self._worker_thread = None


# Module-level singleton scheduler
_SCHEDULER: Optional[LocalScheduler] = None


def get_scheduler(storage_path: Optional[str] = None, ctx: Any = None) -> LocalScheduler:
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = LocalScheduler(storage_path=storage_path, ctx=ctx)
        _SCHEDULER.start()
    elif ctx is not None and _SCHEDULER._ctx is None:
        _SCHEDULER._ctx = ctx
    return _SCHEDULER


def set_scheduler(scheduler: Optional[LocalScheduler]) -> None:
    global _SCHEDULER
    if _SCHEDULER and _SCHEDULER != scheduler:
        try:
            _SCHEDULER.stop()
        except Exception:
            pass
    _SCHEDULER = scheduler


def handle_scheduler_command(text: str, ctx: Any = None, scheduler: Optional[LocalScheduler] = None) -> Optional[str]:
    """
    Parses and executes voice commands related to timers, alarms, and reminders.
    Returns spoken reply string if matched, or None.
    """
    clean = text.lower().strip(" ,.!?")
    sched = scheduler or get_scheduler(ctx=ctx)

    # 1. Cancel / Stop Timer
    if re.search(r"\b(?:cancel|stop|clear|dismiss|delete)\s+(?:the\s+)?timer\b", clean) or clean in ("cancel timer", "stop timer", "clear timer"):
        ok = sched.cancel_timer()
        if ok:
            return "Timer cancelled, sir."
        return "There are no active timers to cancel, sir."

    # 2. Time left on timer / Timer status
    if any(q in clean for q in ("time left on timer", "how much time is left", "timer status", "check timer", "how much time left", "time remaining", "time on timer")):
        left = sched.get_time_left()
        if left is not None and left > 0:
            formatted = format_duration(left)
            active = sched.get_active_timer()
            label_suffix = f" on your {active.label} timer" if (active and active.label and active.label.lower() != "timer") else ""
            return f"There is {formatted} remaining{label_suffix}, sir."
        return "There are no active timers running, sir."

    # 3. Set Timer
    # Matches "set a timer for 5 minutes", "timer 10 minutes", "set 15 minute timer for pasta"
    m_timer = re.search(r"\b(?:set\s+(?:a\s+)?)?timer\s+(?:for\s+)?(.+)", clean)
    if not m_timer:
        m_timer = re.search(r"\bset\s+(?:a\s+)?(\d+.+?)\s+timer\b", clean)

    if m_timer and not any(k in clean for k in ("cancel", "stop", "time left", "status")):
        raw_dur = m_timer.group(1).strip()
        label = "Timer"

        # Check label specified: "for pasta", "for meeting", "called tea"
        m_label = re.search(r"(?:for|called|named)\s+([a-zA-Z\s]+)$", raw_dur)
        if m_label and not re.search(r"\b(?:seconds?|minutes?|hours?|mins?|secs?)\b", m_label.group(1)):
            label = m_label.group(1).strip().title()
            raw_dur = raw_dur[:m_label.start()].strip()

        duration = parse_duration_seconds(raw_dur)
        if duration and duration > 0:
            item = sched.set_timer(duration, label=label)
            formatted = format_duration(duration)
            label_part = f" for {label}" if label != "Timer" else ""
            return f"Timer set for {formatted}{label_part}, sir."

    # 4. Set Reminder
    # Matches "remind me to <task> in <duration>"
    m_remind = re.search(r"\bremind\s+(?:me\s+)?(?:to\s+)?(.+?)\s+in\s+(.+)$", clean)
    if m_remind:
        task = m_remind.group(1).strip()
        dur_str = m_remind.group(2).strip()
        dur = parse_duration_seconds(dur_str)
        if dur and dur > 0:
            target = time.time() + dur
            sched.set_reminder(target, text=task, label=task.capitalize())
            return f"I will remind you to {task} in {format_duration(dur)}, sir."

    # 5. Set Alarm
    m_alarm = re.search(r"\b(?:set\s+(?:an?\s+)?)?alarm\s+(?:for\s+)?(.+)", clean)
    if m_alarm:
        target_str = m_alarm.group(1).strip()
        # Parse alarm time like "7:30 am" or "15:00"
        m_time = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", target_str)
        if m_time:
            hour = int(m_time.group(1))
            minute = int(m_time.group(2)) if m_time.group(2) else 0
            meridiem = m_time.group(3)
            if meridiem:
                if meridiem == "pm" and hour < 12:
                    hour += 12
                elif meridiem == "am" and hour == 12:
                    hour = 0
            now_dt = datetime.datetime.now()
            alarm_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if alarm_dt <= now_dt:
                alarm_dt += datetime.timedelta(days=1)
            target_ts = alarm_dt.timestamp()
            sched.set_alarm(target_ts, label=f"Alarm {alarm_dt.strftime('%I:%M %p').lstrip('0')}")
            return f"Alarm set for {alarm_dt.strftime('%I:%M %p').lstrip('0')}, sir."

    return None
