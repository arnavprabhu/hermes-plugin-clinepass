"""ClinePass quota for Hermes' /usage.

One call: ``GET /api/v1/users/me/plan/usage-limits`` returns the three windows Cline enforces,
the same numbers the Cline dashboard (and CodexBar) show::

    {"data": {"limits": [
        {"type": "five_hour", "percentUsed": 0,  "resetsAt": "2026-11-06T09:15:00Z"},
        {"type": "weekly",    "percentUsed": 76, "resetsAt": "2026-11-08T22:44:30Z"},
        {"type": "monthly",   "percentUsed": 47, "resetsAt": "2026-11-22T02:56:07Z"}]},
     "success": true}

``/api/v1/users/me/plan`` adds the plan name and renewal date. Neither endpoint is documented,
so anything unexpected returns None and /usage stays empty rather than wrong.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

try:
    from . import clinepass_catalog as catalog
except ImportError:  # flat test import
    import clinepass_catalog as catalog

logger = logging.getLogger(__name__)

LIMITS_PATH = "users/me/plan/usage-limits"
PLAN_PATH = "users/me/plan"
LABELS = {"five_hour": "5-hour", "weekly": "Weekly", "monthly": "Monthly"}
ORDER = ("five_hour", "weekly", "monthly")


def _get(path: str, token: str, *, timeout: float = 4.0) -> dict[str, Any]:
    req = urllib.request.Request(f"{catalog.BASE_URL}/{path}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "hermes-clinepass"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        payload = json.loads(resp.read().decode() or "{}")
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise ValueError("unexpected response shape")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except (TypeError, ValueError):
        return None


def build_snapshot(limits_data: dict, plan_data: dict | None, now: datetime):
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    rows = {str(r.get("type")): r for r in limits_data.get("limits") or () if isinstance(r, dict)}
    windows = []
    for kind in (*ORDER, *(k for k in rows if k not in ORDER)):
        row = rows.get(kind)
        if not row or not isinstance(row.get("percentUsed"), (int, float)):
            continue
        windows.append(AccountUsageWindow(
            label=LABELS.get(kind, kind.replace("_", " ").title()),
            used_percent=max(0.0, min(100.0, float(row["percentUsed"]))),
            reset_at=_ts(row.get("resetsAt"))))
    if not windows:
        return None
    plan = (plan_data or {}).get("plan") or {}
    details = []
    period_end = _ts((plan_data or {}).get("currentPeriodEnd"))
    if period_end:
        details.append(f"Billing period renews {period_end.strftime('%Y-%m-%d')}")
    return AccountUsageSnapshot(
        provider=catalog.PROVIDER, source="cline_usage_limits", fetched_at=now, title="ClinePass limits",
        plan=str(plan.get("displayName") or "ClinePass"), windows=tuple(windows), details=tuple(details),
        raw=limits_data)


_CACHE_TTL_S = 60.0  # Hermes may ask on every turn
_cache: dict[str, tuple[float, Any]] = {}


def fetch_account_usage(token: str | None):
    token = (token or "").strip()
    if not token:
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    snapshot = _fetch(token)
    if snapshot is not None:
        _cache.clear()
        _cache[key] = (time.monotonic(), snapshot)
    return snapshot


def _fetch(token: str):
    try:
        limits = _get(LIMITS_PATH, token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("clinepass usage unavailable: %s", type(exc).__name__)
        return None
    try:
        plan = _get(PLAN_PATH, token, timeout=3.0)
    except (urllib.error.URLError, OSError, ValueError):
        plan = None  # plan name is decoration; the limits alone are enough
    try:
        return build_snapshot(limits, plan, datetime.now(timezone.utc))
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("clinepass usage parse failed: %s", type(exc).__name__)
        return None
