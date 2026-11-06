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

`/api/v1/users/me/plan` gives the plan name, renewal date and caps (`plan.entitlements.cline_pass.inferenceCapThreshold`: `last5HoursUsageCostUSDPerUser`, `last7daysUsageCostUSDPerUser`, `last30daysUsageCostUSDPerUser`). `/api/v1/users/{id}/usages` returns per-request rows newest first, at most 200 per page, paged with `?cursor=<nextToken>`. Caps and `costUsd` are integers in units of 1e-8 USD. For example, a request the stream reported as `cost: 0.0000302` shows up in the log as 3020, and the 5-hour cap of 1e9 is $10.

A heavy account logs more than a thousand rows a day, and the endpoint returned 429 after about 40 pages in a few seconds. So rows are kept in `$HERMES_HOME/cache/clinepass/usage.sqlite` (account id, row id, timestamp, cost, whether it was a pass row). Each call:

1. reads new pages from the head until it reaches a stored row (if it can't reach one within budget, it drops the old rows instead of leaving a gap);
2. spends the rest of its budget (7 s, 25 pages) backfilling from a saved cursor;
3. sums the rows whose model is `cline-pass/...` inside each window.

A window that reaches further back than the ledger covers is labelled "at least". Results are cached in memory for 2 minutes per token, because Hermes may ask on every turn. Any unexpected shape, HTTP error or timeout returns `None`, and `/usage` stays empty.

## Verified live (2026-11-05)

- Streaming tool round trips (call, tool result, final answer) on glm-5.3, deepseek-v4-pro and kimi-k3.1-pro. A non-streaming auxiliary-style call on deepseek-v4.1-flash-1017.
- `hermes -z` with the terminal tool on glm-5.3. A two-tool write-then-read task on deepseek-v4.1-flash.
- Usage log rows for those requests show `aiModelTypeName: cline-pass`.
- Cline CLI import with no `CLINE_API_KEY` set: an expired login was refreshed on import, a completion succeeded, a forced pool refresh rotated the token, and a completion and `/usage` both worked on the new token.
- `hermes usage --provider clinepass` converging over four calls.

Not verified: whether the Cline CLI's own copy of an imported login actually stops working after Hermes refreshes it (testing that would have meant breaking a working login), and image input on each model.
