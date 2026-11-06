"""ClinePass model catalog: ids, capabilities and per-model reasoning vocabulary.

The table is pinned on purpose. Cline's generic ``/api/v1/models`` lists vendor ids
(``z-ai/glm-5.3``), not the ``cline-pass/...`` ids the subscription accepts, and the public
lineup feed (``/api/v1/ai/cline/recommended-models``, ``clinePass`` bucket) carries ids only — no
context windows, modalities or reasoning levels. So the feed decides *which* ids the picker
shows, and this table supplies what Hermes needs to know about each one.

Sources: docs.cline.bot/getting-started/clinepass (ids), Cline's vendor catalog (context,
modalities), and live probes against the ClinePass route (reasoning object shape).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

PROVIDER = "clinepass"
# No "cline-pass" alias: model ids start with "cline-pass/", and an alias of that name would
# read as a provider prefix in `/model cline-pass/glm-5.3`.
ALIASES = ("cline_pass", "clinepass-subscription")
API_ROOT = "https://api.cline.bot"
BASE_URL = f"{API_ROOT}/api/v1"
LINEUP_URL = f"{BASE_URL}/ai/cline/recommended-models"

# Effort vocabularies on Hermes' ladder (none/minimal/low/medium/high/xhigh/max).
# ``always_on`` models reason no matter what; Hermes never sends a "disable" to them.
_OFF_HIGH = ("none", "high")
_OFF_LMH = ("none", "low", "medium", "high")

# id -> (label, context_window, vision, efforts, always_on)
MODELS: dict[str, tuple[str, int, bool, tuple[str, ...], bool]] = {
    "cline-pass/glm-5.3":                  ("GLM 5.3",                    1_048_576, False, ("low", "high", "max"), True),
    "cline-pass/glm-5.3-flash":            ("GLM 5.3 Flash",              1_048_576, True,  ("low", "high", "max"), True),
    "cline-pass/kimi-k3":                  ("Kimi K3",                    1_048_576, True,  ("max",), True),
    "cline-pass/kimi-k3.1-pro":            ("Kimi K3.1 Pro",              1_048_576, True,  ("none", "low", "medium", "high", "max"), False),
    "cline-pass/deepseek-v4-pro":          ("DeepSeek V4 Pro",            1_048_576, False, _OFF_HIGH, False),
    "cline-pass/deepseek-v4.1-flash":      ("DeepSeek V4.1 Flash",        1_048_576, True,  _OFF_HIGH, False),
    "cline-pass/deepseek-v4.1-flash-1017": ("DeepSeek V4.1 Flash 1017",   1_048_576, True,  _OFF_HIGH, False),
    "cline-pass/qwen3.8-max":              ("Qwen3.8 Max",                1_000_000, True,  ("low", "medium", "xhigh"), True),
    "cline-pass/qwen3.7-max":              ("Qwen3.7 Max",                  262_144, False, _OFF_LMH, False),
    "cline-pass/qwen3.7-plus":             ("Qwen3.7 Plus",               1_000_000, True,  _OFF_LMH, False),
    "cline-pass/minimax-m3":               ("MiniMax M3",                 1_048_576, True,  _OFF_LMH, False),
    "cline-pass/minimax-m3.1-flash":       ("MiniMax M3.1 Flash",         1_048_576, True,  _OFF_LMH, False),
    "cline-pass/mimo-v2.6-pro":            ("MiMo V2.6 Pro",                262_144, True,  _OFF_LMH, False),
    "cline-pass/mimo-v2.6-flash":          ("MiMo V2.6 Flash",            1_048_576, True,  _OFF_LMH, False),
    # Meta's Contributor tier may train on prompts and completions: kept last, never the aux default.
    "cline-pass/muse-spark-1.3-contributor": ("Muse Spark 1.3 Contributor", 1_048_576, True,
                                              ("minimal", "low", "medium", "high", "xhigh"), True),
}

DEFAULT_MODEL = "cline-pass/glm-5.3"
# Cheapest non-contributor model; compression/titles run here unless the user picks another.
DEFAULT_AUX_MODEL = "cline-pass/deepseek-v4.1-flash-1017"

# Short names for `/model <alias>`.
MODEL_ALIASES = {
    "glm": "cline-pass/glm-5.3",
    "glm-flash": "cline-pass/glm-5.3-flash",
    "kimi": "cline-pass/kimi-k3",
    "kimi-pro": "cline-pass/kimi-k3.1-pro",
    "deepseek": "cline-pass/deepseek-v4-pro",
    "deepseek-flash": "cline-pass/deepseek-v4.1-flash",
    "qwen": "cline-pass/qwen3.8-max",
    "minimax": "cline-pass/minimax-m3",
    "mimo": "cline-pass/mimo-v2.6-pro",
}


def normalize(model: str | None) -> str:
    """``glm-5.3`` / ``clinepass/glm-5.3`` / ``cline-pass/glm-5.3`` -> ``cline-pass/glm-5.3``."""
    m = (model or "").strip()
    low = m.lower()
    for prefix in ("clinepass/", "cline-pass/"):
        if low.startswith(prefix):
            m, low = m[len(prefix):], low[len(prefix):]
            break
    if low in MODEL_ALIASES:
        return MODEL_ALIASES[low]
    return f"cline-pass/{m}" if m else m


def model_ids() -> tuple[str, ...]:
    return tuple(MODELS)


def capabilities() -> dict[str, dict]:
    """Per-model metadata in Hermes' ``model_overrides`` schema."""
    return {
        mid: {"supports_tools": True, "supports_reasoning": True, "supports_vision": vision,
              "context_window": ctx, "model_family": mid.split("/", 1)[1].split("-")[0]}
        for mid, (_label, ctx, vision, _eff, _on) in MODELS.items()
    }


def efforts(model: str | None) -> tuple[str, ...] | None:
    row = MODELS.get(normalize(model))
    return row[3] if row else None


def always_on(model: str | None) -> bool:
    row = MODELS.get(normalize(model))
    return bool(row and row[4])


def context_window(model: str | None) -> int | None:
    row = MODELS.get(normalize(model))
    return row[1] if row else None


# ── live lineup (public, unauthenticated) ────────────────────────────────────────────────────

_LINEUP_TTL_S = 6 * 3600
_lineup_lock = threading.Lock()
_lineup_cache: tuple[float, tuple[str, ...]] | None = None


def fetch_lineup(timeout: float = 6.0, *, force: bool = False) -> tuple[str, ...] | None:
    """The ``clinePass`` ids Cline currently advertises, or None when the feed is unreachable.

    Not an entitlement check: it is the same feed the Cline SDK uses for its picker.
    """
    global _lineup_cache
    with _lineup_lock:
        if not force and _lineup_cache and time.monotonic() - _lineup_cache[0] < _LINEUP_TTL_S:
            return _lineup_cache[1]
    req = urllib.request.Request(LINEUP_URL, headers={"Accept": "application/json", "User-Agent": "hermes-clinepass"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            payload = json.loads(resp.read().decode())
        ids = tuple(str(row.get("id") or "").strip() for row in payload.get("clinePass") or ()
                    if isinstance(row, dict) and str(row.get("id") or "").startswith("cline-pass/"))
    except Exception as exc:  # noqa: BLE001 - offline / feed shape change: fall back to the pinned table
        logger.debug("clinepass lineup fetch failed: %s", exc)
        return None
    if not ids:
        return None
    with _lineup_lock:
        _lineup_cache = (time.monotonic(), ids)
    return ids


def picker_ids(lineup: tuple[str, ...] | None) -> list[str]:
    """The pinned table, then any id the feed advertises that the table does not know yet.

    A pinned id missing from the feed stays: the feed has omitted ids Cline still serves
    (``cline-pass/deepseek-v4.1-flash``), so retirement is a plugin update, not a feed guess.
    """
    ordered = list(MODELS)
    ordered += [mid for mid in (lineup or ()) if mid not in MODELS]
    return ordered


def invalidate() -> None:
    global _lineup_cache
    with _lineup_lock:
        _lineup_cache = None
