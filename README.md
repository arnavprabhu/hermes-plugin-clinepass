# ClinePass for Hermes

I pay for ClinePass ($9.99 a month), Cline's subscription for open coding models: GLM 5.3, Kimi K3, DeepSeek V4, Qwen 3.8, MiniMax M3, MiMo. I wanted to use that plan from Hermes the way the Claude subscription plugin lets me use my Claude plan, with Hermes running its own loop, tools and approvals and the plan supplying the model.

ClinePass turned out to be simpler than Claude. It's an OpenAI-compatible API at `api.cline.bot`, so no CLI or subprocess is involved. This plugin tells Hermes where the endpoint is, which `cline-pass/...` models exist and what each one can do, how to switch reasoning on and off for each model, and how far through your quota you are.

## Setup

1. Subscribe at [app.cline.bot](https://app.cline.bot/dashboard/subscription?personal=true), then make an API key under Settings > API Keys.
2. Install the plugin and give Hermes the key:

   ```sh
   hermes plugins install arnavprabhu/hermes-plugin-clinepass
   hermes auth add clinepass          # paste the key, or put CLINE_API_KEY in ~/.hermes/.env
   ```

3. Pick it:

   ```sh
   hermes model                       # choose "ClinePass", then a model
   # or for one run:
   hermes --provider clinepass -m cline-pass/glm-5.3
   ```

If you already use the Cline CLI and don't want to make a key, `hermes auth add clinepass --type oauth` copies your `cline auth` login into Hermes instead. Read the caveat below before you do.

## What a run looks like

From my machine, right after installing:

```
$ hermes -z "Use your terminal tool to run: uname -sr — then reply with only its output." \
    --provider clinepass -m cline-pass/glm-5.3
Linux 7.0.0-38-generic
```

And `/usage` (or `hermes usage --provider clinepass`), which matches the Cline dashboard and CodexBar:

```
📈 ClinePass limits
Provider: clinepass (Cline Pass (Monthly))
5-hour: 100% remaining (0% used) • resets in 4h 28m (2026-11-06 03:15 CST)
Weekly: 24% remaining (76% used) • resets in 2d 17h (2026-11-08 16:44 CST)
Monthly: 53% remaining (47% used) • resets in 15d 22h (2026-11-21 20:56 CST)
Billing period renews 2026-11-17
```

Cline caps ClinePass with a 5-hour, a weekly and a monthly window, each with its own reset time. The plugin reads them from the same endpoint CodexBar uses (`/api/v1/users/me/plan/usage-limits`). It isn't documented, so if Cline changes it, /usage goes blank instead of showing something wrong.

## Models

All 15 ClinePass models are in the picker. If Cline's lineup feed lists a model this plugin doesn't know yet, it shows up too.

| Model | Context | Images | Reasoning control |
| --- | --- | --- | --- |
| `cline-pass/glm-5.3` (default) | 1M | no | low / high / max, always on |
| `cline-pass/glm-5.3-flash` | 1M | yes | low / high / max, always on |
| `cline-pass/kimi-k3` | 1M | yes | always on, max |
| `cline-pass/kimi-k3.1-pro` | 1M | yes | off, low, medium, high, max |
| `cline-pass/deepseek-v4-pro` | 1M | no | off or high |
| `cline-pass/deepseek-v4.1-flash` | 1M | yes | off or high |
| `cline-pass/deepseek-v4.1-flash-1017` | 1M | yes | off or high (used for compression and titles) |
| `cline-pass/qwen3.8-max` | 1M | yes | low / medium / xhigh, always on |
| `cline-pass/qwen3.7-max` | 262K | no | off, low, medium, high |
| `cline-pass/qwen3.7-plus` | 1M | yes | off, low, medium, high |
| `cline-pass/minimax-m3` | 1M | yes | off, low, medium, high |
| `cline-pass/minimax-m3.1-flash` | 1M | yes | off, low, medium, high |
| `cline-pass/mimo-v2.6-pro` | 262K | yes | off, low, medium, high |
| `cline-pass/mimo-v2.6-flash` | 1M | yes | off, low, medium, high |
| `cline-pass/muse-spark-1.3-contributor` | 1M | yes | minimal to xhigh, always on |

Hermes' reasoning setting (`/reasoning`, `agent.reasoning_effort`) maps onto these. If you ask for a level a model doesn't have, you get the next one down, so a request never costs more than you asked for. Short names work in `/model` too: `glm`, `kimi`, `deepseek`, `qwen`, `minimax`, `mimo`.

## Things worth knowing

Muse Spark Contributor is on Meta's Contributor tier, which may train on your prompts and completions. I left it in the picker, but it's never a default. Don't point it at private code.

DeepSeek V4 Pro can be slow. One first turn took 77 seconds in my testing, while the other models answered in a second or two.

GLM 5.3 barely cached Hermes' prompt in my runs (about 50 of 19,600 prompt tokens came back cached), so a long GLM session burns through the 5-hour window faster than you'd expect. Kimi cached fine.

The `--type oauth` import takes that login over. Cline rotates refresh tokens, so once Hermes refreshes the copy, the Cline CLI may ask you to run `cline auth` again. The plugin reads `~/.cline` once and never writes to it. If you want both tools working independently, use an API key here. `CLINEPASS_IMPORT_FROM=cline` imports the CLI's pay-as-you-go entry instead of its ClinePass one.

How the pieces work (endpoints, the reasoning mapping, credentials, what I verified live) is in [SPEC.md](SPEC.md).

## Tests

```sh
python -m pytest -c tests/pytest.ini tests
```

They run offline and don't need a Hermes checkout.

MIT licensed. Not affiliated with Cline.
