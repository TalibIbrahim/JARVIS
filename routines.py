"""
routines.py — Routines Engine for J.A.R.V.I.S.

Provides:
- Ordered execution of whitelisted actions configured under 'routines' in jarvis_config.json.
- Default routines:
    - "work mode": launch code, set volume 30%
    - "focus mode": enable do not disturb, set volume 15%
    - "evening mode": toggle night light, screen brightness 30%, volume 20%
- Strict security validation: Every step MUST match WHITELIST_ACTIONS and pass argument schemas.
- Voice commands:
    - "start work mode", "activate focus mode", "evening mode", "run routine <name>"
- Decoupled from UI; 100% unit-testable behind interfaces; strictly NO Qt imports.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai import WHITELIST_ACTIONS, validate_action_arguments, execute_whitelisted_action


DEFAULT_ROUTINES: Dict[str, List[Dict[str, Any]]] = {
    "work mode": [
        {"action": "open_app", "args": {"app": "code"}},
        {"action": "set_volume", "args": {"level": 30}},
    ],
    "focus mode": [
        {"action": "toggle_do_not_disturb", "args": {}},
        {"action": "set_volume", "args": {"level": 15}},
    ],
    "evening mode": [
        {"action": "toggle_night_light", "args": {}},
        {"action": "set_brightness", "args": {"level": 30}},
        {"action": "set_volume", "args": {"level": 20}},
    ],
}


def get_routines(cfg: Any = None) -> Dict[str, List[Dict[str, Any]]]:
    """Retrieves all registered routines from config or defaults."""
    if cfg and hasattr(cfg, "routines") and isinstance(cfg.routines, dict) and cfg.routines:
        return dict(cfg.routines)
    try:
        from config import load_config
        c = load_config()
        if hasattr(c, "routines") and isinstance(c.routines, dict) and c.routines:
            return dict(c.routines)
    except Exception:
        pass
    return dict(DEFAULT_ROUTINES)


def get_routine(name: str, cfg: Any = None) -> Optional[List[Dict[str, Any]]]:
    """Retrieves a routine definition by name (case-insensitive)."""
    clean_name = name.lower().strip()
    routines = get_routines(cfg)
    # Direct match
    if clean_name in routines:
        return routines[clean_name]
    # Suffix match (e.g. "work" matching "work mode")
    for r_name, steps in routines.items():
        if clean_name == r_name or clean_name in r_name or r_name in clean_name:
            return steps
    return None


def save_routine(name: str, steps: List[Dict[str, Any]], cfg: Any = None) -> Tuple[bool, str]:
    """
    Saves a routine into config after strictly validating each action against WHITELIST_ACTIONS.
    """
    clean_name = name.lower().strip()
    if not clean_name:
        return False, "Routine name cannot be empty."

    # Validate all steps
    sanitized_steps = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict) or "action" not in step:
            return False, f"Step {idx + 1} is invalid: missing 'action'."
        action = str(step["action"]).strip()
        args = step.get("args", {})
        valid, err_msg, sanitized_args = validate_action_arguments(action, args)
        if not valid:
            return False, f"Step {idx + 1} validation failed: {err_msg}"
        sanitized_steps.append({"action": action, "args": sanitized_args})

    # Save to config
    try:
        from config import load_config, save_config
        active_cfg = cfg or load_config()
        if not hasattr(active_cfg, "routines") or not isinstance(active_cfg.routines, dict):
            active_cfg.routines = {}
        active_cfg.routines[clean_name] = sanitized_steps
        save_config(active_cfg)
        return True, f"Routine '{clean_name}' saved with {len(sanitized_steps)} steps."
    except Exception as exc:
        return False, f"Failed to save routine: {exc}"


def delete_routine(name: str, cfg: Any = None) -> bool:
    """Deletes a routine from config."""
    clean_name = name.lower().strip()
    try:
        from config import load_config, save_config
        active_cfg = cfg or load_config()
        if hasattr(active_cfg, "routines") and clean_name in active_cfg.routines:
            del active_cfg.routines[clean_name]
            save_config(active_cfg)
            return True
    except Exception:
        pass
    return False


def execute_routine(
    name: str,
    ctx: Any = None,
    cfg: Any = None,
    executor_fn: Optional[Callable[[str, Dict[str, Any], Any], Tuple[bool, str]]] = None,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """
    Executes all actions in the specified routine sequentially.
    Returns (success: bool, spoken_summary: str, step_results: List[Dict[str, Any]]).
    """
    clean_name = name.lower().strip()
    steps = get_routine(clean_name, cfg)
    if not steps:
        return False, f"Routine '{name}' not found, sir.", []

    exec_fn = executor_fn or execute_whitelisted_action
    step_results = []
    executed_count = 0

    for idx, step in enumerate(steps):
        action = step.get("action", "")
        args = step.get("args", {})
        # Re-validate security
        valid, val_msg, sanitized_args = validate_action_arguments(action, args)
        if not valid:
            step_results.append({
                "step": idx + 1,
                "action": action,
                "success": False,
                "message": f"Security check failed: {val_msg}",
            })
            continue

        try:
            ok, msg = exec_fn(action, sanitized_args, ctx)
            step_results.append({
                "step": idx + 1,
                "action": action,
                "success": ok,
                "message": msg,
            })
            if ok:
                executed_count += 1
        except Exception as exc:
            step_results.append({
                "step": idx + 1,
                "action": action,
                "success": False,
                "message": f"Execution error: {exc}",
            })

    # Post to overlay if present
    if ctx and hasattr(ctx, "overlay") and ctx.overlay:
        try:
            ctx.overlay.post("result", text=f"Routine '{name.title()}': {executed_count}/{len(steps)} steps done", icon="routine", duration=4.0)
        except Exception:
            pass

    overall_ok = executed_count == len(steps)
    summary = f"Routine '{name.title()}' initiated, sir. {executed_count} actions executed."
    return overall_ok, summary, step_results


def handle_routine_command(text: str, ctx: Any = None, cfg: Any = None) -> Optional[str]:
    """
    Detects routine execution commands from speech:
      - "start work mode", "activate work mode", "launch work mode", "work mode"
      - "start focus mode", "focus mode"
      - "start evening mode", "evening mode"
      - "run routine <name>", "start routine <name>", "execute routine <name>"
    Returns spoken reply if matched, or None.
    """
    clean = text.lower().strip(" ,.!?")

    # 1. Match explicit "routine <name>"
    m_rout = re.match(r"^(?:start|run|activate|execute|initiate)\s+(?:routine\s+)?(.+?)(?:\s+routine)?$", clean)
    candidate_name = None
    if m_rout:
        candidate_name = m_rout.group(1).strip()
    elif clean in ("work mode", "focus mode", "evening mode"):
        candidate_name = clean

    if candidate_name:
        # Check against available routines
        routines = get_routines(cfg)
        for r_name in routines:
            if candidate_name == r_name or candidate_name in r_name or r_name in candidate_name:
                ok, summary, _ = execute_routine(r_name, ctx=ctx, cfg=cfg)
                return summary

    return None
