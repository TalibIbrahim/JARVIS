import queue
import re
import threading
import time

from commands import run_command


class Speaker(threading.Thread):
    """Runs pyttsx3 on its own thread (the engine is not thread-safe)."""

    def __init__(self, enabled=True):
        super().__init__(daemon=True)
        self.enabled = enabled
        self.q = queue.Queue()

    def say(self, text):
        if self.enabled:
            self.q.put(text)

    def run(self):
        if not self.enabled:
            return
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 180)
            for v in engine.getProperty("voices"):
                if any(n in v.name for n in ("David", "Mark", "George", "James")):
                    engine.setProperty("voice", v.id)
                    break
        except Exception as exc:
            print(f"[voice] text-to-speech unavailable: {exc}")
            return
        while True:
            text = self.q.get()
            if text is None:
                return
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass


class Voice(threading.Thread):
    """Say 'Jarvis, open Chrome' in one go, or say 'Jarvis' and wait for 'Yes, sir?'."""

    def __init__(self, cfg, ctx, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ctx = ctx
        self.stop = stop_event
        self.listening_until = 0.0
        self.audio_q = queue.Queue(maxsize=3)

    def run(self):
        try:
            import speech_recognition as sr
        except ImportError:
            self.ctx.say("Voice control needs the SpeechRecognition package.")
            return
        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = True
        recognizer.pause_threshold = 0.6
        try:
            mic = sr.Microphone()
            with mic as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.8)
        except Exception as exc:
            print(f"[voice] no microphone: {exc}")
            self.ctx.say("I can't find a microphone, sir.")
            return

        # Background worker for network recognition so microphone listening never stalls
        worker = threading.Thread(target=self._recognition_worker, args=(recognizer, sr), daemon=True)
        worker.start()

        while not self.stop.is_set():
            try:
                with mic as source:
                    audio = recognizer.listen(source, timeout=1, phrase_time_limit=6)
                try:
                    self.audio_q.put_nowait(audio)
                except queue.Full:
                    pass  # drop if too many pending to keep responses fresh
            except sr.WaitTimeoutError:
                continue
            except Exception:
                time.sleep(0.5)
                continue

    def _recognition_worker(self, recognizer, sr):
        while not self.stop.is_set():
            try:
                audio = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                text = recognizer.recognize_google(audio)
            except sr.UnknownValueError:
                continue
            except sr.RequestError:
                self.ctx.say("The speech service is unreachable, sir.")
                time.sleep(4)
                continue
            except Exception:
                continue
            self._process(text)

    def _process(self, text):
        t = text.lower().strip()
        now = time.time()
        command = None
        if now < self.listening_until:
            command = t
        else:
            for alias in self.cfg.wake_aliases:
                pattern = r"\b" + re.escape(alias) + r"\b"
                match = re.search(pattern, t)
                if match:
                    command = t[match.end():].strip(" ,.!?")
                    break
            if command is None:
                return                                   # no wake word: ignore
            if not command:
                self.listening_until = now + self.cfg.command_window
                try:
                    self.ctx.overlay.post("listening", until=self.listening_until)
                except Exception:
                    pass
                self.ctx.say("Yes, sir?")
                return
        self.listening_until = 0.0
        reply = run_command(command, self.ctx)
        if reply:
            self.ctx.say(reply)
