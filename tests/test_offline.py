"""Offline tests: no network, no real credentials. Run against a Hermes checkout on sys.path."""
from __future__ import annotations

import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import clinepass_auth as auth  # noqa: E402
import clinepass_catalog as catalog  # noqa: E402
import clinepass_reasoning as reasoning  # noqa: E402
import clinepass_usage as usage  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import urllib.error

    def refuse(*_a, **_k):  # behaves like a machine with no network
        raise urllib.error.URLError("offline test")
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    catalog.invalidate()


# ── catalog ──────────────────────────────────────────────────────────────────────────────────

def test_normalize_accepts_bare_prefixed_and_alias_ids():
    assert catalog.normalize("glm-5.3") == "cline-pass/glm-5.3"
    assert catalog.normalize("clinepass/glm-5.3") == "cline-pass/glm-5.3"
    assert catalog.normalize("cline-pass/kimi-k3") == "cline-pass/kimi-k3"
    assert catalog.normalize("kimi") == "cline-pass/kimi-k3"


def test_native_id_prefixes_bare_names_and_leaves_other_vendors_alone():
    assert catalog.native_id("kimi") == "cline-pass/kimi-k3"
    assert catalog.native_id("glm-5.3") == "cline-pass/glm-5.3"
    assert catalog.native_id("cline-pass/qwen3.8-max") == "cline-pass/qwen3.8-max"
    assert catalog.native_id("z-ai/glm-5.3") is None
    assert catalog.native_id("") is None


def test_no_alias_named_like_the_model_prefix():
    # "cline-pass" as a provider alias would read as a provider prefix in `/model cline-pass/x`.
    assert "cline-pass" not in catalog.ALIASES
    assert all(not mid.startswith(catalog.ALIASES) for mid in catalog.MODELS)


def test_no_model_is_literally_auto_and_aux_default_is_not_contributor():
    assert "auto" not in catalog.MODELS and "auto" not in catalog.MODEL_ALIASES.values()
    assert catalog.DEFAULT_AUX_MODEL in catalog.MODELS
    assert "contributor" not in catalog.DEFAULT_AUX_MODEL and "contributor" not in catalog.DEFAULT_MODEL


def test_capabilities_cover_every_model():
    caps = catalog.capabilities()
    assert set(caps) == set(catalog.MODELS)
    assert caps["cline-pass/glm-5.3"]["supports_vision"] is False
    assert caps["cline-pass/qwen3.7-max"]["context_window"] == 262_144


def test_picker_keeps_pinned_ids_and_appends_new_feed_ids():
    assert catalog.picker_ids(None) == list(catalog.MODELS)
    feed = ("cline-pass/glm-5.3", "cline-pass/brand-new")
    ids = catalog.picker_ids(feed)
    assert ids[: len(catalog.MODELS)] == list(catalog.MODELS)  # feed omissions are not retirements
    assert ids[-1] == "cline-pass/brand-new"


def test_lineup_fetch_failure_returns_none():
    assert catalog.fetch_lineup(force=True) is None  # urlopen refuses -> fail open


# ── reasoning ────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def reasoning_extras():
    return reasoning.extras


def test_unset_reasoning_sends_nothing(reasoning_extras):
    assert reasoning_extras(None, "cline-pass/qwen3.7-plus") == {}


def test_disable_on_switchable_model(reasoning_extras):
    assert reasoning_extras({"enabled": False}, "cline-pass/qwen3.7-plus") == {"reasoning": {"enabled": False}}


def test_disable_on_always_on_model_sends_nothing(reasoning_extras):
    assert reasoning_extras({"enabled": False}, "cline-pass/glm-5.3") == {}


def test_effort_clamps_down_never_up(reasoning_extras):
    assert reasoning_extras({"enabled": True, "effort": "xhigh"}, "cline-pass/qwen3.7-plus") == \
        {"reasoning": {"enabled": True, "effort": "high"}}
    assert reasoning_extras({"enabled": True, "effort": "high"}, "cline-pass/qwen3.8-max") == \
        {"reasoning": {"enabled": True, "effort": "medium"}}
    # Nothing weaker than "low" on Kimi K3 (max only): the floor is the honest match.
    assert reasoning_extras({"enabled": True, "effort": "low"}, "cline-pass/kimi-k3") == \
        {"reasoning": {"enabled": True, "effort": "max"}}


def test_none_effort_is_a_disable(reasoning_extras):
    assert reasoning_extras({"enabled": True, "effort": "none"}, "cline-pass/deepseek-v4-pro") == \
        {"reasoning": {"enabled": False}}


def test_unknown_model_passes_effort_through(reasoning_extras):
    assert reasoning_extras({"enabled": True, "effort": "high"}, "cline-pass/brand-new") == \
        {"reasoning": {"enabled": True, "effort": "high"}}


# ── auth ─────────────────────────────────────────────────────────────────────────────────────

def _write_settings(tmp_path: Path, providers: dict) -> Path:
    path = tmp_path / "settings" / "providers.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "providers": providers}))
    return path


def _login(access="workos:aaa", refresh="rrr", expires=1_900_000_000_000, email="a@b.c"):
    return {"settings": {"auth": {"accessToken": access, "refreshToken": refresh, "expiresAt": expires,
                                  "accountId": "acct", "metadata": {"userInfo": {"email": email}}}}}


def test_settings_path_follows_cline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLINE_DATA_DIR", str(tmp_path / "d"))
    assert auth.cline_settings_path() == tmp_path / "d" / "settings" / "providers.json"
    monkeypatch.delenv("CLINE_DATA_DIR")
    monkeypatch.setenv("CLINE_DIR", str(tmp_path / "c"))
    assert auth.cline_settings_path() == tmp_path / "c" / "data" / "settings" / "providers.json"


def test_read_login_prefers_cline_pass_entry(tmp_path):
    path = _write_settings(tmp_path, {"cline": _login(access="workos:one"), "cline-pass": _login(access="workos:two")})
    login = auth.read_cline_login(path)
    assert login["source_entry"] == "cline-pass" and login["access_token"] == "workos:two"
    assert auth.read_cline_login(path, prefer="cline")["access_token"] == "workos:one"


def test_read_login_adds_workos_prefix(tmp_path):
    path = _write_settings(tmp_path, {"cline": _login(access="bare")})
    assert auth.read_cline_login(path)["access_token"] == "workos:bare"


def test_missing_login_raises_plain_hint(tmp_path, monkeypatch):
    errors = types.ModuleType("hermes_cli.auth_constants")

    class AuthError(RuntimeError):
        def __init__(self, msg, **kw):
            super().__init__(msg)
            self.kw = kw
    errors.AuthError = AuthError
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.auth_constants", errors)
    with pytest.raises(AuthError, match="cline auth"):
        auth.read_cline_login(tmp_path / "nope.json")


def test_handler_only_owns_oauth_add():
    ns = types.SimpleNamespace
    assert auth.auth_handler("status", ns(auth_type="oauth")) is False
    assert auth.auth_handler("add", ns(auth_type=None, api_key=None)) is False
    assert auth.auth_handler("add", ns(auth_type="api-key", api_key=None)) is False
    assert auth.auth_handler("add", ns(auth_type="oauth", api_key="k")) is False


def test_expiry_parsing():
    assert auth._parse_expiry("2026-11-06T00:00:00Z") == 1_793_923_200_000
    assert auth._parse_expiry(1_793_923_200) == 1_793_923_200_000
    assert auth._parse_expiry(None) is None


# ── usage ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def account_usage(monkeypatch):
    acct = types.ModuleType("agent.account_usage")

    class W:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    acct.AccountUsageWindow = W  # type: ignore[attr-defined]
    acct.AccountUsageSnapshot = W  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", acct)


LIMITS = {"limits": [  # shape captured live from /api/v1/users/me/plan/usage-limits
    {"type": "weekly", "percentUsed": 76, "resetsAt": "2026-11-08T22:44:30.65446644Z"},
    {"type": "five_hour", "percentUsed": 0, "resetsAt": "2026-11-06T09:15:00.652481431Z"},
    {"type": "monthly", "percentUsed": 47, "resetsAt": "2026-11-22T02:56:07.656690999Z"}]}
PLAN = {"currentPeriodEnd": "2026-11-17T15:58:04Z", "plan": {"displayName": "Cline Pass (Monthly)"}}


def test_usage_maps_limits_in_order_with_resets(account_usage):
    snap = usage.build_snapshot(LIMITS, PLAN, datetime(2026, 11, 6, 5, tzinfo=timezone.utc))
    assert [(w.label, w.used_percent) for w in snap.windows] == [("5-hour", 0.0), ("Weekly", 76.0), ("Monthly", 47.0)]
    assert snap.windows[1].reset_at == datetime(2026, 11, 8, 22, 44, 30, 654466, tzinfo=timezone.utc)
    assert snap.plan == "Cline Pass (Monthly)" and snap.details == ("Billing period renews 2026-11-17",)


def test_usage_without_plan_still_reports_limits(account_usage):
    snap = usage.build_snapshot(LIMITS, None, datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert snap.plan == "ClinePass" and len(snap.windows) == 3


def test_usage_ignores_malformed_rows_and_empty_payload(account_usage):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    assert usage.build_snapshot({"limits": []}, None, now) is None
    snap = usage.build_snapshot({"limits": [{"type": "weekly", "percentUsed": "lots"},
                                            {"type": "monthly", "percentUsed": 130, "resetsAt": "garbage"}]}, None, now)
    assert [(w.label, w.used_percent, w.reset_at) for w in snap.windows] == [("Monthly", 100.0, None)]


def test_usage_is_cached_per_token(monkeypatch):
    calls = []
    monkeypatch.setattr(usage, "_cache", {})
    monkeypatch.setattr(usage, "_fetch", lambda token: calls.append(token) or object())
    first = usage.fetch_account_usage("k1")
    assert usage.fetch_account_usage("k1") is first and calls == ["k1"]
    usage.fetch_account_usage("k2")
    assert calls == ["k1", "k2"]


def test_usage_without_token_is_none():
    assert usage.fetch_account_usage("") is None


def test_usage_fails_open_on_network_error():
    assert usage.fetch_account_usage("key") is None  # offline -> /usage stays empty


def test_manifest_and_package_files_present():
    text = (ROOT / "plugin.yaml").read_text()
    assert "kind: model-provider" in text and "name: clinepass" in text
    assert "auth.json" not in (ROOT / "__init__.py").read_text()
