"""
Screen Awareness module for J.A.R.V.I.S.
Provides:
- One-shot explicit screen analysis using Groq Multimodal Vision endpoint.
- Strictly in-memory JPEG capture (quality ~80, resized to max 1280px) with zero persistent disk storage.
- Hard privacy constraint: snapshots are only taken upon explicit user query.
- Overlay sending indicator notifications ("sending_indicator") before and after transmission.
- 100% unit-testable interface with mockable capture and vision client adapters.
Strictly NO Qt imports.
"""

import io
import re
from typing import Any, Optional

try:
    from PIL import Image, ImageGrab
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from ai import (
    get_api_key,
    GroqClient,
    DEFAULT_VISION_MODEL,
    get_conversation_manager,
    AuthenticationError,
    RateLimitError,
    OfflineError,
    TimeoutError,
    AIError,
)


SCREEN_SYSTEM_PROMPT = (
    "You are J.A.R.V.I.S., the witty, brilliant AI assistant from Iron Man. "
    "The user has explicitly shared a snapshot of their active computer screen and asked for your analysis. "
    "Carefully analyze what is visible on the screen—such as code, error messages, terminal output, "
    "webpages, or user interfaces—and address their question directly as 'sir'. "
    "Be concise, highly accurate, natural, and limit your spoken reply to 1-3 sentences maximum."
)

SCREEN_QUERY_PATTERNS = [
    r"\bwhat(?:'s| is) on my screen\b",
    r"\bwhat(?:'s| is) on the screen\b",
    r"\bwhat(?:'s| is) on screen\b",
    r"\bwhat does this screen say\b",
    r"\bwhat does my screen say\b",
    r"\bwhat does the screen say\b",
    r"\bexplain this error\b",
    r"\bexplain the error\b",
    r"\bexplain the error on (?:my |the )?screen\b",
    r"\bwhat is this error\b",
    r"\bwhat's this error\b",
    r"\bsummarize this page\b",
    r"\bsummarize the page\b",
    r"\bsummarize this screen\b",
    r"\bsummarize my screen\b",
    r"\binspect my screen\b",
    r"\binspect the screen\b",
    r"\binspect this screen\b",
    r"\bread my screen\b",
    r"\bread the screen\b",
    r"\bread what(?:'s| is) on my screen\b",
    r"\banalyze my screen\b",
    r"\banalyze this screen\b",
    r"\banalyze the screen\b",
    r"\bwhat is showing on my screen\b",
    r"\bcheck my screen\b",
]


def is_screen_query(text: str) -> bool:
    """
    Returns True if the text is an explicit visual query asking to analyze the computer screen.
    Enforces the privacy principle that screen snapshots are only taken upon explicit user request.
    """
    if not text:
        return False
    t = text.lower().strip()
    return any(re.search(pat, t) for pat in SCREEN_QUERY_PATTERNS)


class ScreenCaptureAdapter:
    """
    Adapter for capturing in-memory screen snapshots.
    Downscales to a maximum bounding dimension of 1280px and encodes to JPEG quality ~80.
    """

    def capture_screen_jpeg(self, max_dimension: int = 1280, quality: int = 80) -> bytes:
        if not PIL_AVAILABLE:
            raise RuntimeError("Pillow (PIL) is not available for screen capture.")

        # Single one-shot in-memory grab
        img = ImageGrab.grab()
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        w, h = img.size
        if max(w, h) > max_dimension:
            scale = max_dimension / float(max(w, h))
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            resample_mode = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BILINEAR)
            img = img.resize((new_w, new_h), resample=resample_mode)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


# Global capture adapter instance (mockable for unit tests)
_CAPTURE_ADAPTER: ScreenCaptureAdapter = ScreenCaptureAdapter()


def get_capture_adapter() -> ScreenCaptureAdapter:
    global _CAPTURE_ADAPTER
    return _CAPTURE_ADAPTER


def set_capture_adapter(adapter: ScreenCaptureAdapter) -> None:
    global _CAPTURE_ADAPTER
    _CAPTURE_ADAPTER = adapter


def analyze_screen(query: str, ctx: Any = None, client: Any = None, capture_adapter: Optional[ScreenCaptureAdapter] = None) -> str:
    """
    Executes explicit screen analysis:
    1. Notifies overlay before transmission: overlay.post("sending_indicator", active=True, label="Analyzing Screen")
    2. Takes a single in-memory screenshot JPEG (quality ~80, max 1280px).
    3. Dispatches image and query to Groq Multimodal Vision endpoint.
    4. Upon completion (or error), posts overlay.post("sending_indicator", active=False).
    5. Returns spoken response.
    """
    overlay = getattr(ctx, "overlay", None) if ctx else None
    cfg = getattr(ctx, "cfg", None) or (getattr(ctx, "engine", None) and getattr(ctx.engine, "cfg", None))

    api_key = get_api_key()
    if not api_key:
        if overlay and hasattr(overlay, "post"):
            try:
                overlay.post("ai_error", text="No Groq Key")
            except Exception:
                pass
        return "My Groq API key is not configured, sir. Please check your settings."

    # Notify overlay of transmission start
    if overlay and hasattr(overlay, "post"):
        try:
            overlay.post("sending_indicator", active=True, label="Analyzing Screen")
        except Exception:
            pass

    try:
        # Capture snapshot in-memory
        adapter = capture_adapter or get_capture_adapter()
        try:
            jpeg_bytes = adapter.capture_screen_jpeg(max_dimension=1280, quality=80)
        except Exception as exc:
            print(f"[screen_awareness] Capture error: {exc}")
            return "I was unable to capture your screen, sir."

        # Configure vision client
        vision_model = getattr(cfg, "ai_vision_model", DEFAULT_VISION_MODEL) if cfg else DEFAULT_VISION_MODEL
        timeout = min(max(int(getattr(cfg, "ai_timeout", 10)), 3), 15) if cfg else 10

        groq_client = client or GroqClient(api_key=api_key, timeout=timeout)

        # Dispatch one-shot image to Groq Vision endpoint
        prompt_text = query.strip() if query and query.strip() else "What is on my screen, sir?"
        reply = groq_client.describe_image(
            jpeg_bytes,
            prompt=prompt_text,
            model=vision_model,
            system_prompt=SCREEN_SYSTEM_PROMPT,
        )

        # Record into conversation context
        try:
            conv_mgr = get_conversation_manager()
            conv_mgr.add_turn(query, reply)
        except Exception:
            pass

        return reply

    except AuthenticationError:
        if overlay and hasattr(overlay, "post"):
            try:
                overlay.post("ai_error", text="Invalid Groq Key")
            except Exception:
                pass
        return "My Groq API key appears to be invalid, sir. Please check your settings."
    except RateLimitError:
        if overlay and hasattr(overlay, "post"):
            try:
                overlay.post("ai_error", text="Groq Rate Limited")
            except Exception:
                pass
        return "Groq rate limit reached, sir. Please stand by a moment."
    except (OfflineError, TimeoutError):
        if overlay and hasattr(overlay, "post"):
            try:
                overlay.post("ai_error", text="Vision Timed Out")
            except Exception:
                pass
        return "Screen analysis timed out, sir. Please try again."
    except AIError as exc:
        print(f"[screen_awareness] AIError: {exc}")
        return "I was unable to analyze your screen at this time, sir."
    except Exception as exc:
        print(f"[screen_awareness] Unexpected error: {exc}")
        return "I was unable to analyze your screen, sir."
    finally:
        # Ensure indicator is deactivated upon completion or failure
        if overlay and hasattr(overlay, "post"):
            try:
                overlay.post("sending_indicator", active=False)
            except Exception:
                pass
