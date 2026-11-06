"""Hermes reasoning config -> ClinePass request fields.

Cline's gateway takes the OpenRouter-style ``reasoning`` object in the request body. Verified
live on the ClinePass route: ``{"enabled": false}`` takes qwen3.7-plus from 438 reasoning tokens
to 0, and ``effort`` low vs high changes the spend. Kept stdlib-only (no Hermes-internal
imports) so it cannot break on a core refactor.
"""
from __future__ import annotations

from typing import Any

try:
    from . import clinepass_catalog as catalog
except ImportError:  # flat test import
    import clinepass_catalog as catalog

LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def requested_effort(cfg: Any) -> str | None:
    if not isinstance(cfg, dict) or cfg.get("enabled") is False:
        return None
    return str(cfg.get("effort") or "").strip().lower() or None


def clamp(effort: str, supported: tuple[str, ...]) -> str:
    """Nearest weaker supported level (a clamp never raises cost); the floor when none is weaker.
    Unknown names pass through."""
    levels = [lvl for lvl in supported if lvl in LADDER and lvl != "none"]
    if not levels or effort in levels or effort not in LADDER:
        return effort
    below = [lvl for lvl in levels if LADDER.index(lvl) < LADDER.index(effort)]
    return max(below, key=LADDER.index) if below else min(levels, key=LADDER.index)


def extras(reasoning_config: Any, model: str | None) -> dict[str, Any]:
    """Unset -> nothing (the model's own default). Disabled -> ``enabled: false``, except on
    always-on models, which reason regardless (nothing sent). An effort is clamped onto the
    model's vocabulary."""
    if not isinstance(reasoning_config, dict):
        return {}
    effort = requested_effort(reasoning_config)
    if reasoning_config.get("enabled") is False or effort == "none":
        return {} if catalog.always_on(model) else {"reasoning": {"enabled": False}}
    if not effort:
        return {"reasoning": {"enabled": True}}
    supported = catalog.efforts(model)
    return {"reasoning": {"enabled": True, "effort": clamp(effort, supported) if supported else effort}}
