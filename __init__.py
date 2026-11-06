"""ClinePass — Hermes model-provider plugin.

Runs Hermes turns on Cline's $9.99/month ClinePass subscription (GLM, Kimi, DeepSeek, Qwen,
MiniMax, MiMo...). ClinePass speaks OpenAI-compatible Chat Completions at api.cline.bot, so
Hermes' own chat-completions transport does the streaming and tool calls; this plugin declares
the endpoint, the model table, the reasoning shape, a /usage readout and the credential paths.

Credentials (all through Hermes):
  * Cline API key — CLINE_API_KEY, or `hermes auth add clinepass` (prompts for it)
  * opt-in: `hermes auth add clinepass --type oauth` copies the Cline CLI's login once
"""
from __future__ import annotations

import logging
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

try:
    from . import clinepass_auth as auth
    from . import clinepass_catalog as catalog
    from . import clinepass_reasoning as reasoning
    from . import clinepass_usage as usage
except ImportError:  # flat test import
    import clinepass_auth as auth
    import clinepass_catalog as catalog
    import clinepass_reasoning as reasoning
    import clinepass_usage as usage

logger = logging.getLogger(__name__)


def _current_token() -> str | None:
    """The credential Hermes would use: its pool for this provider, else CLINE_API_KEY."""
    try:
        from agent.credential_pool import load_pool
        entry = load_pool(catalog.PROVIDER).select()
        if entry is not None and entry.runtime_api_key:
            return entry.runtime_api_key
    except Exception:  # noqa: BLE001 - no pool: try the env var
        logger.debug("clinepass: credential pool lookup failed", exc_info=True)
    try:
        from agent.secret_scope import get_secret
        return (get_secret("CLINE_API_KEY") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


class ClinePassProfile(ProviderProfile):
    """OpenAI-compatible ClinePass route with per-model reasoning control."""

    def build_api_kwargs_extras(self, *, reasoning_config: dict | None = None, model: str | None = None,
                                **context: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return reasoning.extras(reasoning_config, model), {}

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...] | None:
        return catalog.efforts(model)

    def get_model_context_length(self, model: str) -> int | None:
        return catalog.context_window(model)

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None,
                     timeout: float = 8.0) -> list[str] | None:
        # /api/v1/models lists vendor ids, not pass ids: the lineup feed is the picker source.
        return catalog.picker_ids(catalog.fetch_lineup(timeout=min(timeout, 6.0)))

    def invalidate_catalog_cache(self) -> None:
        catalog.invalidate()

    def fetch_account_usage(self, *, base_url: str | None = None, api_key: str | None = None):
        return usage.fetch_account_usage(api_key or _current_token())

    def missing_credentials_hint(self) -> str | None:
        return ("Set CLINE_API_KEY (app.cline.bot -> Settings -> API Keys) or run `hermes auth add clinepass`; "
                "`hermes auth add clinepass --type oauth` imports an existing `cline auth` login instead.")


def _setup_efforts(model: str) -> tuple[str, ...] | None:
    return catalog.efforts(model)


clinepass = ClinePassProfile(
    name=catalog.PROVIDER,
    aliases=catalog.ALIASES,
    display_name="ClinePass",
    description="ClinePass subscription — GLM, Kimi, DeepSeek, Qwen, MiniMax, MiMo via Cline",
    signup_url="https://app.cline.bot/dashboard/subscription?personal=true",
    env_vars=("CLINE_API_KEY", "CLINEPASS_BASE_URL"),
    base_url=catalog.BASE_URL,
    auth_type="api_key",
    fallback_models=catalog.model_ids(),
    model_aliases=dict(catalog.MODEL_ALIASES),
    default_aux_model=catalog.DEFAULT_AUX_MODEL,
    model_capabilities=catalog.capabilities(),
    # Without X-Response-Format, non-streaming replies nest the completion under "data" and
    # OpenAI clients fail to parse them (auxiliary calls are non-streaming).
    default_headers={"X-Response-Format": "openai", "X-Title": "Hermes Agent",
                     "HTTP-Referer": "https://hermes-agent.nousresearch.com"},
    auth_handler=auth.auth_handler,
    refresh_credential=auth.refresh_credential,
    setup_reasoning_efforts=_setup_efforts,
    picker_catalog=lambda force_refresh: catalog.picker_ids(catalog.fetch_lineup(force=bool(force_refresh))),
    static_models=catalog.model_ids(),
)

register_provider(clinepass)
