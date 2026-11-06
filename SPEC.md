# How the ClinePass plugin works

## Provider

- Id `clinepass`, aliases `cline_pass` and `clinepass-subscription`. There's deliberately no `cline-pass` alias: every model id starts with `cline-pass/`, and an alias with that name would read as a provider prefix in `/model cline-pass/glm-5.3`.
- `auth_type: api_key`, env `CLINE_API_KEY`, base URL override `CLINEPASS_BASE_URL`.
- Endpoint `https://api.cline.bot/api/v1/chat/completions` through Hermes' normal chat-completions transport, so streaming, tool calls and usage come from core.
- Every request carries `X-Response-Format: openai`. Without it, a non-streaming reply nests the completion under `data`, and auxiliary calls (titles, compression) are non-streaming.
- Auxiliary default: `cline-pass/deepseek-v4.1-flash-1017`, the cheapest model that isn't on the Contributor tier.

## Catalog

`clinepass_catalog.py` pins 15 ids with context windows, image support and reasoning vocabulary. Cline's `/api/v1/models` lists vendor ids (`z-ai/glm-5.3`), not pass ids, so the picker can't come from there. The public lineup feed (`/api/v1/ai/cline/recommended-models`, `clinePass` bucket) only carries ids. The picker is the pinned table plus any id the feed adds. An id the feed leaves out stays in the picker: in November 2026 the feed omitted `cline-pass/deepseek-v4.1-flash` while the API still served it. Retiring a model means updating the plugin.

Context windows and modalities come from Cline's vendor catalog, reduced to 262K where third-party ClinePass testing reported a smaller route (MiMo V2.6 Pro, Qwen3.7 Max).

## Reasoning

`clinepass_reasoning.py` maps Hermes' reasoning config onto the OpenRouter-style `reasoning` body object, which the ClinePass route accepts:

| Hermes config | Sent |
| --- | --- |
| unset | nothing (the model's default) |
| disabled, or effort `none` | `{"reasoning": {"enabled": false}}`; nothing for always-on models |
| effort X | `{"reasoning": {"enabled": true, "effort": clamp(X)}}` |

The clamp picks the nearest weaker level the model supports, or the weakest one if nothing is weaker. It never escalates. Measured on qwen3.7-plus: default 438 reasoning tokens, `enabled:false` 0, `effort:low` 179, `effort:high` 324. The top-level `reasoning_effort` field gave the same counts.

## Credentials

- API key: Hermes' built-in api-key path. `auth_handler` declines every action except `add --type oauth`.
- Cline CLI import (`hermes auth add clinepass --type oauth`): reads `$CLINE_DATA_DIR`, else `$CLINE_DIR/data`, else `~/.cline/data`, then `settings/providers.json`, preferring the `cline-pass` entry over `cline` (`CLINEPASS_IMPORT_FROM` overrides that). It asks before importing (`CLINEPASS_IMPORT_ASSUME_YES=1` skips the prompt), refreshes the token if it expires within 5 minutes, and stores an `oauth` pool row with source `manual:cline_cli_import`. Bearer tokens carry the `workos:` prefix the Cline CLI uses.
- Refresh: `POST /api/v1/auth/refresh` with `{"refreshToken", "grantType": "refresh_token"}`, the same call the Cline CLI makes. Hermes' pool calls it 2 minutes before expiry and on a 401. A 400, 401 or 403 is reported as a dead grant (`relogin_required`), which takes the row out of rotation and names the re-import command.
- The plugin never writes to `~/.cline`.

## /usage

`GET /api/v1/users/me/plan/usage-limits` (bearer: API key or `workos:` token) returns
`{"data": {"limits": [{"type": "five_hour"|"weekly"|"monthly", "percentUsed": <0-100>, "resetsAt": <ISO>}]}}`.
Those become Hermes usage windows (5-hour, Weekly, Monthly) with reset times, and they are the numbers the Cline
dashboard and CodexBar show. `GET /api/v1/users/me/plan` adds the plan name and renewal date and is optional.
Snapshots are cached for 60 s per token because Hermes may ask on every turn. Any HTTP error or unexpected shape
returns `None`.

The first version summed `costUsd` from `/api/v1/users/{id}/usages` over rolling windows. That was wrong: Cline's
windows are anchored and reset at fixed times, so the rolling 30-day sum showed 96% used while the real monthly
window was at 47%.

## Verified live (2026-11-05)

- Streaming tool round trips (call, tool result, final answer) on glm-5.3, deepseek-v4-pro and kimi-k3.1-pro. A non-streaming auxiliary-style call on deepseek-v4.1-flash-1017.
- `hermes -z` with the terminal tool on glm-5.3. A two-tool write-then-read task on deepseek-v4.1-flash.
- Usage log rows for those requests show `aiModelTypeName: cline-pass`.
- Cline CLI import with no `CLINE_API_KEY` set: an expired login was refreshed on import, a completion succeeded, a forced pool refresh rotated the token, and a completion and `/usage` both worked on the new token.
- `hermes usage --provider clinepass` output matching CodexBar and the Cline dashboard (24% weekly, 53% monthly left).

Not verified: whether the Cline CLI's own copy of an imported login actually stops working after Hermes refreshes it (testing that would have meant breaking a working login), and image input on each model.
