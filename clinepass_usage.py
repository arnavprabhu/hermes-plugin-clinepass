"""ClinePass quota for Hermes' /usage.

Cline publishes no quota endpoint. The dashboard numbers come from two account endpoints the
Cline CLI also calls:

  GET /api/v1/users/me/plan      plan name, billing period, and the pass caps under
                                 plan.entitlements.cline_pass.inferenceCapThreshold
                                 (last5Hours / last7days / last30days, per user)
  GET /api/v1/users/{id}/usages  per-request rows, newest first, 200 per page, `cursor` paging

Caps and per-row ``costUsd`` are integers in units of 1e-8 USD at ClinePass reference prices
(a $0.0000302 request is stored as 3020; the 5-hour cap of 1_000_000_000 is $10).

An agent-heavy account logs well over a thousand rows a day, and the endpoint answers 429 after
a few dozen quick pages, so one /usage call cannot walk 30 days. Rows go into a small local
ledger instead (``$HERMES_HOME/cache/clinepass/usage.sqlite``: row id, time, cost, nothing else).
Each call reads the new rows at the head of the log and spends what is left of its budget
backfilling older pages; until the backfill reaches 30 days back, the longer windows say
"at least". All of this is undocumented, so every step fails open: /usage stays empty rather
than wrong.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from . import clinepass_catalog as catalog
except ImportError:  # flat test import
    import clinepass_catalog as catalog

logger = logging.getLogger(__name__)

UNIT_USD = 1e-8
PAGE_LIMIT = 200
BUDGET_S = 7.0            # Hermes gives the hook 10 s in total
MAX_PAGES_PER_CALL = 25   # under the rate limit seen live (~40 quick pages -> 429)
WINDOWS = (               # (cap key, label, window)
    ("last5HoursUsageCostUSDPerUser", "5-hour", timedelta(hours=5)),
    ("last7daysUsageCostUSDPerUser", "7-day", timedelta(days=7)),
    ("last30daysUsageCostUSDPerUser", "30-day", timedelta(days=30)),
)
HORIZON = WINDOWS[-1][2]


class RateLimited(Exception):
    pass


def _get(path: str, token: str, *, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(f"{catalog.BASE_URL}/{path}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "hermes-clinepass"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited() from None
        raise
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise ValueError("unexpected response shape")
    return payload.get("data") or {}


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def is_pass_row(row: dict[str, Any]) -> bool:
    return (str(row.get("aiModelName") or "").startswith("cline-pass/")
            or str(row.get("aiModelTypeName") or "") == "cline-pass")


# ── ledger ───────────────────────────────────────────────────────────────────────────────────

def ledger_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "cache" / "clinepass" / "usage.sqlite"


class Ledger:
    """Per-account rows (id, epoch seconds, cost units, is_pass) and the backfill cursor."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=5.0)
        self.db.execute("CREATE TABLE IF NOT EXISTS rows (account TEXT, id TEXT, at REAL, cost INTEGER, "
                        "pass INTEGER, PRIMARY KEY (account, id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (account TEXT PRIMARY KEY, backfill_cursor TEXT, "
                        "complete INTEGER)")

    def close(self) -> None:
        self.db.close()

    def empty(self, account: str) -> bool:
        return self.db.execute("SELECT 1 FROM rows WHERE account=? LIMIT 1", (account,)).fetchone() is None

    def known(self, account: str, ids: list[str]) -> bool:
        ids = [i for i in ids if i]
        if not ids:
            return False
        marks = ",".join("?" * len(ids))
        return self.db.execute(f"SELECT 1 FROM rows WHERE account=? AND id IN ({marks}) LIMIT 1",  # noqa: S608
                               (account, *ids)).fetchone() is not None

    def add(self, account: str, items: list[dict]) -> None:
        values = []
        for row in items:
            at = _ts(row.get("createdAt"))
            cost = row.get("costUsd")
            if row.get("id") and at is not None and isinstance(cost, (int, float)):
                values.append((account, str(row["id"]), at.timestamp(), int(cost), int(is_pass_row(row))))
        self.db.executemany("INSERT OR IGNORE INTO rows VALUES (?,?,?,?,?)", values)

    def state(self, account: str) -> tuple[str | None, bool]:
        hit = self.db.execute("SELECT backfill_cursor, complete FROM state WHERE account=?", (account,)).fetchone()
        return (hit[0], bool(hit[1])) if hit else (None, False)

    def set_state(self, account: str, cursor: str | None, complete: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (account, cursor, int(complete)))

    def reset(self, account: str) -> None:
        self.db.execute("DELETE FROM rows WHERE account=?", (account,))
        self.db.execute("DELETE FROM state WHERE account=?", (account,))

    def prune(self, account: str, before: datetime) -> None:
        self.db.execute("DELETE FROM rows WHERE account=? AND at < ?", (account, before.timestamp()))

    def oldest(self, account: str) -> datetime | None:
        hit = self.db.execute("SELECT MIN(at) FROM rows WHERE account=?", (account,)).fetchone()
        return datetime.fromtimestamp(hit[0], timezone.utc) if hit and hit[0] is not None else None

    def totals(self, account: str, now: datetime) -> dict[str, int]:
        out = {}
        for key, _label, span in WINDOWS:
            hit = self.db.execute("SELECT COALESCE(SUM(cost),0) FROM rows WHERE account=? AND pass=1 AND at >= ?",
                                  (account, (now - span).timestamp())).fetchone()
            out[key] = int(hit[0])
        return out


PageFn = Callable[[str], "tuple[list[dict], str]"]


def sync(ledger: Ledger, account: str, fetch_page: PageFn, *, now: datetime, deadline: float,
         max_pages: int = MAX_PAGES_PER_CALL) -> datetime | None:
    """Bring the ledger up to date. ``fetch_page(cursor) -> (items, next_cursor)``. Returns None
    when the ledger covers the whole 30-day horizon, else the oldest instant it is sure about."""
    since = now - HORIZON
    pages = 0

    def budget_left() -> bool:
        return pages < max_pages and deadline - time.monotonic() > 0.6

    def ends(items: list[dict], nxt: str) -> bool:
        oldest = _ts(items[-1].get("createdAt")) if items else None
        return not items or not nxt or (oldest is not None and oldest < since)

    backfill_cursor, complete = ledger.state(account)
    was_empty = ledger.empty(account)
    try:
        # 1. head: newest rows until we meet stored ones (or the log ends / passes the horizon)
        cursor, head = "", []
        met = reached_end = False
        while budget_left():
            try:
                items, nxt = fetch_page(cursor)
            except RateLimited:
                if not was_empty:
                    raise  # a partial head over stored rows would leave a gap: keep the old state
                break
            pages += 1
            cursor = nxt
            met = not was_empty and ledger.known(account, [str(r.get("id") or "") for r in items])
            head.extend(items)
            if met or ends(items, cursor):
                reached_end = not met
                break
        if not met and not reached_end and not was_empty:
            # More new rows than one call can read: keeping the old rows would leave a gap and
            # undercount, so coverage restarts from this head.
            ledger.reset(account)
            was_empty = True
        ledger.add(account, head)
        if reached_end:
            backfill_cursor, complete = None, True
        elif was_empty:
            backfill_cursor, complete = cursor or None, not cursor
        # 2. backfill: older pages with whatever budget is left
        while not complete and backfill_cursor and budget_left():
            items, nxt = fetch_page(backfill_cursor)
            pages += 1
            ledger.add(account, items)
            if ends(items, nxt):
                backfill_cursor, complete = None, True
            else:
                backfill_cursor = nxt
    except RateLimited:
        logger.debug("clinepass usage: rate limited; keeping what the ledger has")
    finally:
        ledger.set_state(account, backfill_cursor, complete)
        ledger.prune(account, since - timedelta(days=1))
        ledger.db.commit()
    # Nothing stored yet still means "incomplete": every window is a lower bound.
    return None if complete else (ledger.oldest(account) or now)


# ── snapshot ─────────────────────────────────────────────────────────────────────────────────

def _usd(units: int | float) -> str:
    return f"${units * UNIT_USD:,.2f}"


def build_snapshot(plan_data: dict, totals: dict[str, int], partial_from: datetime | None, now: datetime):
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    plan = plan_data.get("plan") or {}
    caps = (((plan.get("entitlements") or {}).get("cline_pass") or {}).get("inferenceCapThreshold") or {})
    if not caps:
        return None
    windows = []
    for key, label, span in WINDOWS:
        cap = caps.get(key)
        if not isinstance(cap, (int, float)) or cap <= 0:
            continue
        used = totals.get(key, 0)
        lower_bound = "at least " if partial_from is not None and partial_from > now - span else ""
        windows.append(AccountUsageWindow(
            label=label, used_percent=min(100.0, round(used / cap * 100, 1)),
            detail=f"{lower_bound}{_usd(used)} of {_usd(cap)} at reference prices"))
    details = []
    period_end = _ts(plan_data.get("currentPeriodEnd"))
    if period_end:
        details.append(f"Billing period renews {period_end.strftime('%Y-%m-%d')}")
    if partial_from is not None:
        details.append(f"Usage log read back to {partial_from.strftime('%Y-%m-%d %H:%M')} UTC so far; "
                       "older rows fill in over the next few /usage calls")
    details.append("Rolling windows from your Cline usage log, at reference prices (not a bill)")
    return AccountUsageSnapshot(
        provider=catalog.PROVIDER, source="cline_usage_log", fetched_at=now, title="ClinePass limits",
        plan=str(plan.get("displayName") or "ClinePass"), windows=tuple(windows), details=tuple(details))


_CACHE_TTL_S = 120.0  # Hermes may ask on every turn
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


def _fetch(token: str, *, path: Path | None = None):
    deadline = time.monotonic() + BUDGET_S
    now = datetime.now(timezone.utc)
    try:
        plan_data = _get("users/me/plan", token, timeout=4.0)
        if not ((((plan_data.get("plan") or {}).get("entitlements") or {}).get("cline_pass") or {}).get("enabled")):
            return None
        account = str(_get("users/me", token, timeout=4.0).get("id") or "")
        if not account:
            return None
        base = f"users/{urllib.parse.quote(account)}/usages"

        def fetch_page(cursor: str) -> tuple[list[dict], str]:
            query = {"limit": str(PAGE_LIMIT), **({"cursor": cursor} if cursor else {})}
            data = _get(f"{base}?{urllib.parse.urlencode(query)}", token,
                        timeout=min(4.0, max(0.5, deadline - time.monotonic())))
            return [r for r in data.get("items") or () if isinstance(r, dict)], str(data.get("nextToken") or "")

        ledger = Ledger(path or ledger_path())
        try:
            partial_from = sync(ledger, account, fetch_page, now=now, deadline=deadline)
            totals = ledger.totals(account, now)
        finally:
            ledger.close()
        return build_snapshot(plan_data, totals, partial_from, now)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, RateLimited, sqlite3.Error) as exc:
        logger.debug("clinepass usage unavailable: %s", type(exc).__name__)
        return None
