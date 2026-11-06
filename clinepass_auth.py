"""ClinePass credentials.

Default: a Cline API key (app.cline.bot -> Settings -> API Keys) through Hermes' normal api-key
path — ``CLINE_API_KEY`` in the environment / ``.env``, or ``hermes auth add clinepass``.

Opt-in: ``hermes auth add clinepass --type oauth`` copies the Cline CLI's own login (from
``cline auth``) into Hermes' credential pool once. After that Hermes owns the copy and refreshes
it through Cline's ``/api/v1/auth/refresh`` endpoint. Cline refresh tokens rotate, so after Hermes
refreshes, the Cline CLI's copy of that login may stop working and need ``cline auth`` again. This
module never writes to the Cline CLI's files.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    from . import clinepass_catalog as catalog
except ImportError:  # flat test import
    import clinepass_catalog as catalog

logger = logging.getLogger(__name__)

IMPORT_SOURCE = "manual:cline_cli_import"
REFRESH_URL = f"{catalog.BASE_URL}/auth/refresh"
TOKEN_PREFIX = "workos:"
# Which Cline CLI provider entry to copy, in preference order. Both are logins to the same Cline
# account; ``cline-pass`` is what `cline` writes when ClinePass is picked in /settings.
IMPORT_ORDER = ("cline-pass", "cline")
_HTTP_TIMEOUT = 20.0


def _err(message: str, code: str, *, relogin: bool = False):
    from hermes_cli.auth_constants import AuthError
    return AuthError(message, provider=catalog.PROVIDER, code=code, relogin_required=relogin)


def bearer(token: str) -> str:
    """Cline sends OAuth access tokens as ``workos:<jwt>``; API keys go through untouched."""
    t = (token or "").strip()
    return t if not t or t.lower().startswith(TOKEN_PREFIX) else f"{TOKEN_PREFIX}{t}"


# ── locating the Cline CLI login ─────────────────────────────────────────────────────────────

def cline_settings_path() -> Path:
    """Same resolution as the Cline CLI: CLINE_DATA_DIR, else CLINE_DIR/data, else ~/.cline/data."""
    data = (os.environ.get("CLINE_DATA_DIR") or "").strip()
    if not data:
        root = (os.environ.get("CLINE_DIR") or "").strip() or str(Path.home() / ".cline")
        data = str(Path(root) / "data")
    return Path(data) / "settings" / "providers.json"


def read_cline_login(path: Path | None = None, *, prefer: str | None = None) -> dict[str, Any]:
    """The newest usable Cline CLI login: ``{source_entry, access_token, refresh_token,
    expires_at_ms, email, account_id}``. Raises AuthError with a plain hint when there is none."""
    path = path or cline_settings_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _err(f"No Cline CLI login found at {path}. Run `cline auth` first, or use an API key: "
                   "`hermes auth add clinepass`.", "cline_login_missing") from None
    except (OSError, ValueError) as exc:
        raise _err(f"Could not read the Cline CLI settings at {path}: {type(exc).__name__}.",
                   "cline_login_unreadable") from None
    providers = (doc or {}).get("providers") or {}
    order = (prefer,) + IMPORT_ORDER if prefer else IMPORT_ORDER
    for name in dict.fromkeys(order):
        settings = ((providers.get(name) or {}).get("settings") or {})
        auth = settings.get("auth") or {}
        access, refresh = str(auth.get("accessToken") or "").strip(), str(auth.get("refreshToken") or "").strip()
        if access and refresh:
            info = ((auth.get("metadata") or {}).get("userInfo") or {})
            expires = auth.get("expiresAt")
            return {"source_entry": name, "access_token": bearer(access), "refresh_token": refresh,
                    "expires_at_ms": int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
                    "email": str(info.get("email") or "").strip(), "account_id": str(auth.get("accountId") or "")}
    raise _err(f"The Cline CLI settings at {path} hold no OAuth login. Run `cline auth`, or use an API key: "
               "`hermes auth add clinepass`.", "cline_login_missing")


# ── refresh ──────────────────────────────────────────────────────────────────────────────────

def _parse_expiry(value: Any) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value if value > 10**12 else value * 1000)
    if isinstance(value, str) and value.strip():
        from datetime import datetime
        try:
            return int(datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    """POST Cline's refresh endpoint; returns pool fields. Token values are never logged."""
    if not (refresh_token or "").strip():
        raise _err("The imported Cline login has no refresh token. Re-import it: "
                   "`hermes auth add clinepass --type oauth`.", "invalid_grant", relogin=True)
    body = json.dumps({"refreshToken": refresh_token.strip(), "grantType": "refresh_token"}).encode()
    req = urllib.request.Request(REFRESH_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "hermes-clinepass"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 - fixed https URL
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            raise _err(f"Cline rejected the refresh token (HTTP {exc.code}); the login was revoked or used "
                       "elsewhere. Run `cline auth`, then `hermes auth add clinepass --type oauth` again, or "
                       "switch to an API key.", "invalid_grant", relogin=True) from None
        raise _err(f"Cline token refresh failed with HTTP {exc.code}.", "refresh_failed") from None
    except (OSError, ValueError) as exc:
        raise _err(f"Cline token refresh failed: {type(exc).__name__}.", "refresh_failed") from None
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    access = str(data.get("accessToken") or "").strip()
    if not payload.get("success", True) or not access:
        raise _err("Cline token refresh returned no access token.", "refresh_failed")
    from datetime import datetime, timezone
    return {
        "access_token": bearer(access),
        "refresh_token": str(data.get("refreshToken") or refresh_token).strip(),
        "expires_at_ms": _parse_expiry(data.get("expiresAt")),
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def refresh_credential(entry: Any) -> dict[str, Any] | None:
    """``ProviderProfile.refresh_credential``: rotate an imported row. API-key rows never get here
    (the pool only refreshes oauth rows)."""
    return refresh_tokens(str(getattr(entry, "refresh_token", "") or ""))


# ── `hermes auth add clinepass --type oauth` ─────────────────────────────────────────────────

def _confirm(prompt: str) -> bool:
    if os.environ.get("CLINEPASS_IMPORT_ASSUME_YES", "").strip() in ("1", "true", "yes"):
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print("Not a terminal; set CLINEPASS_IMPORT_ASSUME_YES=1 to import without the prompt.")
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def import_cline_login(*, path: Path | None = None, label: str | None = None) -> Any:
    """Copy the Cline CLI login into Hermes' pool (refreshing it first when it is stale)."""
    from agent.credential_pool import AUTH_TYPE_OAUTH, PooledCredential, load_pool

    login = read_cline_login(path, prefer=(os.environ.get("CLINEPASS_IMPORT_FROM") or "").strip() or None)
    where = path or cline_settings_path()
    print(f"Found a Cline CLI login ({login['source_entry']}) in {where}.\n"
          "Hermes will copy it into its own credential pool and refresh it from now on. Cline rotates\n"
          "refresh tokens, so once Hermes refreshes, the Cline CLI may ask you to run `cline auth` again.\n"
          "Nothing under ~/.cline is modified. An API key avoids this entirely: `hermes auth add clinepass`.")
    if not _confirm("Import it? [y/N] "):
        print("Import cancelled.")
        return None
    fields = {k: login[k] for k in ("access_token", "refresh_token", "expires_at_ms")}
    if fields["expires_at_ms"] is None or fields["expires_at_ms"] - int(time.time() * 1000) < 5 * 60_000:
        print("The Cline CLI access token is expired; refreshing it for Hermes...")
        fields = refresh_tokens(fields["refresh_token"])
    who = f" ({login['email']})" if login["email"] else ""
    entry = load_pool(catalog.PROVIDER).add_entry(PooledCredential(
        provider=catalog.PROVIDER, id=uuid.uuid4().hex[:6], label=label or f"cline-login{who}",
        auth_type=AUTH_TYPE_OAUTH, priority=0, source=IMPORT_SOURCE, base_url=catalog.BASE_URL,
        **fields, extra={"cline_import": {"from": login["source_entry"]}}))
    print(f"Imported the Cline login{who} as credential {entry.id} for {catalog.PROVIDER}.")
    return entry


def auth_handler(action: str, args: Any) -> bool:
    """Own only ``hermes auth add clinepass --type oauth``; everything else (api-key add, status,
    logout, refresh) stays with Hermes' built-in credential-pool handling."""
    if action != "add":
        return False
    requested = str(getattr(args, "auth_type", "") or "").strip().lower()
    if requested != "oauth" or getattr(args, "api_key", None):
        return False
    import_cline_login(label=getattr(args, "label", None))
    return True
