# buddy-proxy

> A local multi-provider model gateway: it turns the subscription quotas of AI IDEs / coding clients (CodeBuddy, Trae, Qoder, Gemini, Antigravity…) into standard **OpenAI Chat Completions**, **Responses**, and **Anthropic Messages** protocols — so you can plug those models into Codex CLI, Claude Code / CC Switch, OpenCode, Grok, Oh My Pi and any OpenAI-compatible client. One endpoint, routed by model name.

> **中文文档见 [README_zh.md](README_zh.md).**

---

## Features

- **Multi-provider** — besides CodeBuddy, built-in **Trae** (decrypts the Trae IDE login, connects straight to the underlying models), **ZCode** (Zhipu GLM), **GLM official** (BigModel Coding Plan key — same upstream as ZCode, independent credentials), **Doubao** (pure-stdlib CDP into the Doubao desktop app), **DuMate** (Baidu 千帆 desktop app's local proxy — GLM / Qwen / Kimi), **Xiaomi MiMo** (API key, or reuses the MiMo Desktop Xiaomi-account login), **Qoder** (COSY signing reimplemented in pure Python — Qwen3.8 / GLM / Kimi), **Gemini** (Google OAuth, Code Assist free quota — its login state is kept in sync with the local `gemini` CLI) and **Antigravity** (Google Antigravity free quota — Gemini 3.x / Claude / GPT-OSS via one OAuth login, imports the local `agy` CLI login state); all listed by `/v1/models` and routed by model name
- **Multi-account failover** — most subscription channels (codebuddy / trae / qoder / kimi / codex / antigravity…) support multiple accounts: rotated in login order as primary/backup; account-level errors (401 expired credentials / 429 quota exhausted) cool the current account down and switch to the next automatically. Per-account quota, check-in, rename / reorder / delete are all managed in the admin UI
- **Protocol conversion** — `/v1/chat/completions` (OpenAI), `/v1/responses` (Codex CLI), `/v1/messages` (Anthropic / Claude Code)
- **Admin UI** — built-in web console at `/ui`: browse models grouped by provider, one-click "set as default model", one-click test per model (sends a "hi"), and per-model request stats with charts. Providers backed by a reliable upstream catalog (Qoder / QoderIntl / Kimi / Antigravity) can refresh their model list in place. Any model can be hidden from catalogs and selectors without disabling direct requests, then restored from the “Hidden” manager; disabling remains the separate hard-rejection control. Loads lazily per tab, so the first paint never waits on the slowest endpoint (the request log)
- **Model list** — `/v1/models` returns an OpenAI-compatible model list plus rich per-model metadata (context window, credits, input modalities / image support); locally hidden models are omitted consistently from this endpoint, the admin catalog, and model-order candidates
- **Desensitization** (`--desensitize`) — inserts zero-width spaces into compliance terms inside system messages to avoid backend false-blocking by keyword review
- **Message compression** (`--optimize-context`) — compresses long histories / large schemas / oversized tool output for `/v1/responses`, cutting token usage dramatically
- **Tool calls** — full function calling support with automatic filtering of invalid tool definitions; `tool_choice` is normalized across both OpenAI and Anthropic shapes so it never reaches the upstream as an object
- **DSML parsing** — detects and converts DeepSeek Markup Language tool calls
- **Streaming** — SSE output with idle / total-duration timeout protection
- **Both wire protocols** — OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`, i.e. Claude Code) over the same models; each provider converts responses back to whichever protocol the client asked for

---

## Install & run

The proxy is a plain Python package under `src/`. Install its dependencies with [uv](https://docs.astral.sh/uv/), then use the `buddy` shell command for daily operation:

```bash
uv sync
./buddy install            # one-time: install the buddy command on PATH
buddy start                # start the proxy and open the admin UI
```

If you have not installed the wrapper yet, run it from the repository as `./buddy start`.
Use `buddy login <provider>` for upstream sign-in; `buddy` is a repository script, not a
Python console command supplied by `uv sync`. `buddy start` uses the repository's configured default providers; do not append provider flags as if `buddy` were the Python module CLI.

The first run creates the state directory `~/.buddy-proxy/` (mode `0700`; override with
`BUDDY_PROXY_STATE_DIR`). Everything machine-local lives there: `settings.json` (default
model, disabled/hidden models, disabled providers, model time windows), the Trae Work credential `trae_work.json`,
the PAT token cache `trae_pat_token.json`, the cached MiMo token/serviceToken state, and
the client-name map `buddy_client_names.json`. It holds credentials — keep it out of
backups and version control. Startup prints the resolved path as `[State] ...`.

### The `buddy` command (recommended)

`buddy` is the day-to-day entry point: one command starts the proxy and opens the admin UI. It can also register the proxy as a macOS launchd service (auto-start at login, automatic restart on crash).

```bash
# Before installation, use ./buddy from the repository directory.
./buddy install            # one-time: put buddy on PATH

# After installation, use buddy from any directory.
buddy start                # start (if not running) and open http://127.0.0.1:8787/ui
buddy stop / restart / status / logs
buddy login [provider]     # upstream login (codebuddy(=workbuddy, =codebuddyintl global)/trae(=traeintl global)/zcode/glm/doubao/mimo/qoder(=qoderintl global)/gemini/antigravity/kimi/codex)
                           # trae/qoder/codebuddy accept --region cn|global (accounts differ per region); other providers ignore it
                           # traeintl/qoderintl/codebuddyintl are login aliases meaning --region global; logging in a global account also enables the
                           # traeintl/qoderintl/codebuddyintl channels — separate quota cards in the UI
buddy ui                   # just open the admin UI (starts the proxy if needed)
buddy update               # update to the latest code (git pull -> uv sync -> restart)

# register as a system service (launchd)
buddy service install      # start/stop/restart now route through launchctl
buddy service status
buddy service uninstall
```

- When the service is installed, `buddy start/stop/restart` automatically use `launchctl`; otherwise they fall back to `proxy.sh`'s pid management.
- Env vars `PROXY_HOST` (default `0.0.0.0`), `PROXY_PORT` (default `8787`) and `PROXY_EXTRA_ARGS` apply to both `start` and `service install`.
- `buddy update` runs `git pull --ff-only` in the repo it was installed from, then `uv sync`, then restarts the service. It **refuses to run when the working tree has uncommitted changes** — it will not stash, merge or otherwise touch work in progress, so a half-finished edit can never be silently overwritten. The refusal prints the offending files and a hint tailored to what is actually dirty: if any untracked file is present it suggests `git stash -u` (plain `git stash` leaves untracked files behind, so following that advice would land you right back on the same refusal), otherwise plain `git stash`. `--ff-only` means a diverged branch fails loudly instead of creating a surprise merge commit — the failure message quotes git's own wording (`Not possible to fast-forward` vs `Could not read from remote`) so you can tell a diverged branch from an unreachable remote. If `uv sync` fails the service is left running rather than restarted onto broken dependencies. Running it on an already-current checkout is harmless: it just re-syncs dependencies and restarts.

### The `proxy.sh` management script

`buddy` calls `proxy.sh` internally; you can drive it directly for finer control:

```bash
./proxy.sh start          # background start, returns immediately
./proxy.sh stop           # stop the running instance
./proxy.sh restart        # restart (with the same args)
./proxy.sh status         # show PID and listening address
./proxy.sh logs           # tail -F the log file
./proxy.sh ui             # ensure it's running, then open the admin UI
./proxy.sh login codebuddy  # upstream login (also: trae / zcode / doubao / mimo / qoder)

# customize host / port / args
./proxy.sh start -p 9000 -H 0.0.0.0
PROXY_PORT=9000 PROXY_EXTRA_ARGS="--desensitize --optimize-context" ./proxy.sh start
```

The script:
- Detects `.venv/bin/python` automatically (preferring the project venv).
- Uses `nohup ... &` so `start` returns immediately — terminal is **not** blocked.
- Writes the PID to `logs/proxy.pid` and startup output to `logs/proxy.sh.log`; app logs rotate daily (`logs/proxy.log` + `logs/buddy-proxy.jsonl`, 30 days kept).
- Stops cleanly with `kill`; falls back to `kill -9` after 10s if the process doesn't exit.

---

## Models

The static catalog in `src/buddy_proxy/web/models_config.json` contains **51 CodeBuddy and Trae PAT entries** and is served without a remote dependency. Other provider catalogs, including Trae Work, are supplied by their providers. The tables below list CodeBuddy and Trae Work models; the Trae Work domestic catalog is defined in `src/buddy_proxy/trae/config.py`. `GET /v1/models` → `data[].credits` / `models[].credits` exposes the multiplier:

**CodeBuddy channel** (19) — bare model ids, no prefix:

| id | name | credits |
|---|---|---|
| `auto` | Auto (fast / balanced / ultimate → 0.21 / 0.65 / 1.20) | dynamic |
| `default` | Default | x2.20 |
| `glm-5.3` | GLM-5.3 | x0.79 |
| `glm-5.3-flash` | GLM-5.3-Flash | x0.06 |
| `glm-5.3-flashx` | GLM-5.3-FlashX | x0.14 |
| `glm-5.2` | GLM-5.2 (夜间折扣) | x0.79 |
| `glm-5.1` | GLM-5.1 | x0.79 |
| `glm-5v-turbo` | GLM-5v-Turbo (vision) | x0.71 |
| `hy3` | Hy3 (限时免费) | x0.00 |
| `hy4-preview` | Hy4 preview | x0.29 |
| `minimax-m3` | MiniMax-M3 | x0.25 |
| `kimi-k3` | Kimi-K3 | x1.62 |
| `kimi-k2.8-preview` | Kimi-K2.8-Preview | x0.77 |
| `kimi-k2.7` | Kimi-K2.7-Code | x0.57 |
| `kimi-k2.6` | Kimi-K2.6 | x0.52 |
| `deepseek-v4.1-flash` | Deepseek-V4.1-Flash | x0.11 |
| `space-bunny` | Space-Bunny | x0.03 |
| `deepseek-v4-flash` | Deepseek-V4-Flash | x0.17 |
| `deepseek-v4-pro` | Deepseek-V4-Pro | x0.51 |

> Note on `glm-*`: the bare name resolves to whichever channel claims it first in registration order (**zcode**, which serves `glm-5.3` / `glm-5.3-flash`). For `glm-5.3-flashx` the zcode subscription reports `1311 当前订阅套餐暂未开放GLM-5.3-FlashX权限` while **CodeBuddy serves it fine** — so use the explicit `codebuddy/glm-5.3-flashx` prefix for that one. The official **`glm/`** channel (below) shares the same upstream and model table; use its explicit prefix to route a request over the official plan's key.

**Trae Work channel** (15 domestic models; IDs are lowercase). Credit multipliers are the current catalog values; `—` means no multiplier is published:

| id | credits |
|---|---|
| `glm-5.3` | x0.40 |
| `glm-5.3-flash` | x0.06 |
| `glm-5.3-flashx` | x0.31 |
| `deepseek-v4.1-flash` | x0.08 |
| `doubao-seed-evolving` | x0.08 |
| `kimi-k3` | x1.83 |
| `doubao-seed-2.1-pro` | x0.08 |
| `deepseek-v4-pro` | x0.72 |
| `qwen3.8-max` | x1.50 |
| `doubao-seed-2.1-turbo` | x0.20 |
| `minimax-m3` | x0.26 |
| `kimi-k2.6` | — |
| `glm-5.1` | — |
| `step-5-preview` | x0.48 |
| `doubao-seed-code` | x0.06 |

The catalog order mirrors the upstream client's display order. The PAT provider remains available at `traepat/<model>` but its model table is intentionally omitted here. Edit `src/buddy_proxy/web/models_config.json` to add or tweak the static CodeBuddy / Trae PAT entries — changes take effect on restart.

First-time setup: install the `buddy` wrapper as described above, then start the proxy and sign in as needed:

```bash
buddy start
buddy login codebuddy
```

The proxy listens on `http://127.0.0.1:8787` by default; the admin UI lives at **http://127.0.0.1:8787/ui** — see [Admin UI](#admin-ui-ui) below.

## Quick check

```bash
curl http://127.0.0.1:8787/health      # service + auth status
curl http://127.0.0.1:8787/v1/models    # model list
```

## Admin UI (`/ui`)

Open <http://127.0.0.1:8787/ui> in a browser (or just run `buddy start` / `buddy ui`):

- **Default model** — models are grouped by provider; click "Set as default" (设为默认) on any model to make it the proxy default. Client requests **without a `model` field** are routed to it automatically. Settings persist in `~/.buddy-proxy/settings.json` (override via `BUDDY_PROXY_SETTINGS`) and survive restarts; `--default-model zcode/glm-5.3` seeds the initial value (an existing settings file wins).
- **Model search** — a live search box sits above the models list: typing instantly filters providers and models by provider id/name or model id/name (case-insensitive, whitespace-trimmed, purely client-side — no request is made). A provider hit keeps that provider's whole table, a model hit keeps only the matching rows, and providers with no hits drop out entirely. While a query is active, matched groups are force-expanded and the expand/collapse-all buttons are disabled; clearing the box (✕ or Esc) restores your exact expand/collapse layout. Searching never touches the persisted collapse preference (`bp-collapsed` in localStorage), and the query survives the 30s auto-refresh re-render.
- **Provider on/off switch** — each provider group header on the models tab has an "启用" checkbox (default on). Turning a provider off: its calls are rejected with 403 (`provider_disabled`), its models disappear from `/v1/models`, its quota card/panel and alert-banner entries are hidden, and its rows on the model-order tab are greyed out. The groups data stays so the switch can simply be turned back on. `codebuddy` is the default fallback channel and cannot be disabled. Persisted in `settings.json` as `disabled_providers`.
- **Catalog refresh and hiding** — providers with a live catalog capability (currently Qoder, Kimi and Antigravity) expose “↻ 重新拉取” in the group header. A refresh queries every account (including accounts temporarily excluded from chat failover by cooldown), unions successful results and atomically updates that provider's in-memory catalog. A partially successful round may add models but retains the previous ids because a failed account could be their sole source; removals are confirmed only after every account responds successfully. If every account fails, the last good catalog stays active. For Antigravity the upstream `fetchAvailableModels` list is treated as untrusted input, not ground truth: it still advertises deprecated families (`gemini-2.5-*`, `gemini-3-flash*`, `gemini-3.1-flash*`, `gemini-3.5-*`), so the `blocked_prefixes` list in `models.json` drops them on discovery (gross prefixes stay gross; `blocked_prefix_exceptions` exempts exact base names such as the image model `gemini-3.1-flash-image`), while genuinely new models (not on the list) are still picked up; and it omits premium curated models (`gemini-3.1-pro` / `gemini-3.6-flash`, visible in the IDE and routable but never returned by the catalog endpoint), so every `verified: true` entry is pinned and can never be dropped by a refresh. “隐藏” is deliberately weaker than disable: it removes a model from the models tab, selectors and both arrays returned by `/v1/models`, but an explicit `provider/model` request remains callable. Hidden keys persist in `settings.json` as `hidden_models` and can be restored from the “已隐藏” dialog even after the upstream removes the model.
- **One-click test** — every model row has a "Test" (测试) button that sends a real `hi` upstream and shows latency, token usage, the reply preview, and the actual responding provider/model (for example, `zcode/glm-5.3`, including when model-order routing selects the provider). Non-streaming, `max_tokens=256` — a real, billable upstream call.
- **Stats & charts** — per-provider/per-model request counts, errors, average latency and token usage: a 14-day stacked daily chart, a top-models bar list, and the latest 50 requests. Multi-account channels (qoder / kimi / antigravity / trae / traepat) tag each request-log row with the account that actually served it in parentheses (after failover, the one that succeeded). Each completed request appends one line to `logs/metrics.jsonl`; the tail is reloaded on startup so history survives restarts (30 days kept).
- **Provider health** — login/config status at a glance (CodeBuddy session, zcode key, mimo auth mode, ...).
- **Auto check-in & calendar** — CodeBuddy / Trae / Qoder expose daily sign-in: tick "Auto check-in" (自动打卡) and the proxy claims them every day at the configured time (default 09:30; if the proxy starts later it catches up immediately). Qoder's Global account uses the same inherited campaign check-in path and appears as its own row when `qoderintl` is registered. The last 35 days are shown as a calendar; "Check in now" (立即打卡) claims manually. Campaigns can be seasonal — when CodeBuddy's is closed the UI shows "no sign-in activity today" and skips it. History is appended to `logs/checkin.jsonl`. ZCode (GLM Coding Plan) / Doubao have no sign-in API; DuMate does (auto-claimed).
- **Next check-in time per channel** — each row shows when the next claim opens, with a per-second countdown (computed locally — no upstream calls; it stops when you leave the tab or the page is hidden). The three channels **rotate on different schedules**, so the source of the timestamp differs: Qoder's window is `10:00 → 09:59` next day (the upstream returns `startAt`/`endAt`, marked `upstream`), while CodeBuddy and Trae rotate at **local midnight** — neither upstream reports a daily rotation field at all (CodeBuddy only gives the whole campaign season, Trae no time fields whatsoever), so midnight is inferred from the real claim timestamps in `logs/checkin.jsonl`, marked `inferred` and given a dashed border plus an italic timestamp in the UI (a marker on the value itself reads as a typo). While a Qoder claim is still unclaimed the timestamp shown is the **deadline** for this round (miss it and it's gone), not the next round's start. When no next time can be computed (campaign over, season ended, nothing today) it is simply not shown rather than showing a stale timestamp. Once the countdown reaches "即将刷新" the next poll picks up the new state **immediately** — that same moment doubles as the expiry condition for the cached status snapshot (otherwise, with the rotation falling inside the cache's 5-minute TTL, the UI would keep showing "已签到" and a disabled button, and those five minutes are enough for Qoder to lose a whole round). To keep an upstream that keeps returning a long-past value from wedging the cache, this early expiry only applies within a one-hour grace window.
- **"Already checked in today" defers to upstream** — local history dedupes by **calendar day**, but Qoder rotates on a 10:00 window, so the two disagree: an early-morning poll records the *previous* round's `checked_in` as a claim for today, and when the new campaign opens at 10:00 and the upstream says `claimable`, the old logic still skipped it for the whole day while the UI showed "已签到" with the button disabled. Now anything the upstream reports as claimable is claimed, and the button is no longer wrongly disabled; the midnight-rotating CodeBuddy / Trae are unaffected.
- **Expiry warnings** — allowances that come with a shelf life — plans, resource packs, check-in credits, top-up packs — now report their expiry as a structured field and are labelled "· MM-DD 到期" (expires) in the UI, kept distinct from "· MM-DD 重置" (resets). **Expiring means the allowance is voided; resetting means it refills on a cycle** — the two used to share one `reset_ts` field, so Qoder's `expiresAt` was rendered as "10-30 重置" and looked like the expiry was never recorded at all. They are now separate fields (`expire_ts` vs `reset_ts`) and only the former drives the warning banner, so ZCode's 5-hour window and antigravity's weekly pool are never mistaken for "about to expire". When something expires within **7 days**, a banner appears at the top of the "打卡 & 额度" tab: one summary line saying how many items and which is soonest, expandable into a detail list (channel / name / expiry date / days left) with already-expired rows in red. The filtering lives in one place on the backend (`benefits._expiring`) and the frontend only renders it: besides the 7-day window, credit-class items (`unit == "credit"`) must also have **more than 300 remaining** to qualify — the 200-credit check-in packs would otherwise fill the banner the moment they enter the window and drown out the plan that actually matters; non-credit units (MiMo's days-left, ZCode's counts, antigravity's permille) are not comparable to 300 and are filtered by days alone. This also backfilled Trae's per-pack `used/total/remaining`: the upstream `usage.credits_amount` is **consumed**, not remaining (cross-checked on 2026-10-03 against the account total: `Σlimit − Σamount` matches the reported remaining to 0.00, the opposite assumption is off by 3014), and a missing `usage` means **genuinely unspent**, not unknown (the packs that do report usage sum exactly to `usage_summary.consumed_amount`, consumed strictly FIFO by expiry date) — the reverse of the Trae PAT precedent where a missing value must not be read as zero; both sites carry comments explaining the difference. All 27 entitlement packs are now listed (previously deduped by name and capped at 3, hiding 12 unspent allowances).
- **Low-balance warnings** — the other half of the same banner, complementary to the expiry warnings (2026-10-03): a channel whose entitlements are far from expiring but whose balance is nearly drained would otherwise only surface on the day it runs dry. The threshold **splits by unit** (clarified by the user the same day: the "or 8%" clause is for channels not billed in Credits): credit channels (CodeBuddy / Qoder / Trae) judge on the **absolute value only** — total remaining `< 300 credits` fires, and 950 left is 950 left no matter what the percentage says; channels not billed in Credits (ZCode / MiMo / antigravity) have no "credits" to count and judge on the **remaining percentage** instead — `< 8%` fires. The summation basis matches the quota card headline — what you see as "剩 X / Y" is exactly what gets judged: channels with the `sum_items` flag are summed before judging (Qoder's coexisting allowances), those without take the first item only (Trae's packs are a breakdown of the total it already reports — adding double-counts; CodeBuddy puts its own pre-computed total first, so the same rule holds). The rule lives in one place on the backend (`benefits._quota_low`) and the frontend only renders it; sitting exactly on a threshold (=300 / =8%) does not count as "below".
- **Quota** — remaining allowance per provider at a glance: CodeBuddy credit packs (total remaining + per-pack detail), Trae total allowance + entitlement pack expiry, ZCode 5-hour / monthly windows with reset times, MiMo weekly quota + plan validity. Every dedicated provider panel keeps one channel-wide ↻ button in its title (not one duplicate per account); it invalidates and re-queries the whole provider and applies the returned snapshot directly. Trae Global has its own quota panel and an explicit “海外版无每日签到” row with no claim button. The operational Trae PAT panel stays last. Each card headline shows "剩 X / Y · 已用 N%" next to the total. CodeBuddy used to emit its own "积分余额合计" summary *row* as the first item; that row is gone (no other channel had one) — the card now declares `sum_items` instead and the headline sums every pack itself. CodeBuddy's "total remaining" figures are always accumulated over **every** pack, and its pack detail is no longer truncated either — the backend used to return only the first 4 rows, and since the frontend never learns what was dropped, those packs were invisible for good; row counts are now handled entirely by the frontend fold described below, so everything is reachable. **Items that are used up (remaining ≤ 0) are folded away**: they go into a "已用完 N 项" block at the bottom, hidden by default and shown on expand, sitting *after* the items still in use (they are history, not current state — separated by a dashed rule and dimmed one notch). However many items there are, the block never fills the screen: past **208 px** (the `--qfold-max` CSS variable, which the JS reads so the cap lives in one place) it is clipped with a bottom fade and a "展开全部 N 项 ▾" button appears; expanding shows the natural height and flips the button to "收起 ▴". The decision is made on **measured height**, not an item count — in the two-column layout a narrow card has a different usable height than a full-width one, so a count-based rule clips the wrong thing. The expanded state lives in `QUOTA_FOLD` rather than on the DOM, because the whole page is rebuilt from `innerHTML` every 30 seconds and DOM-held state would be wiped (the user opens it, and 30 seconds later it closes itself). **Items whose remaining is unknown are never treated as used up** — the PAT failure notice (`remaining` holds text like "2/9 个账号查询失败") and `reset_pending`'s "用量待确认" are *unknown*, not *unspent*; folding them on a zero reading would hide exactly the failure the user most needs to see. The main quota list, the Trae PAT panel and the antigravity panel all share this one implementation. Each card's headline is driven by an explicit `sum_items` flag from the provider (the panel headline adds the buckets up when set, otherwise takes the first item), because the per-item rows are not comparable across channels: Qoder's subscription quota + add-on pack + dedicated credits are genuinely coexisting allowances whose sum is the account total (the upstream's own `totalUsagePercentage` is computed the same way), whereas Trae's entitlement packs are a *breakdown* of the total allowance it already reports (adding them double-counts) and ZCode / MiMo report different units (5-hour window vs monthly window; percent vs days). Providers that can be summed must therefore opt in; the default is off so a new channel is never silently mis-added. Quota responses are cached for 5 minutes. The Trae PAT standard pool has no active query API — its usage is collected **passively** from the `4031` (quota exhausted) error body, and because that error only fires when the pool is already full, a snapshot that is past its `reset_ts` is shown as "reset · pending confirmation" rather than as a stale 100%. The quota page is the only network-touching endpoint in the admin UI, so its queries are bounded four ways: the gateway is probed for reachability first (DNS+TCP precheck — if unreachable the whole round is skipped and per-account caches are served instead), each account request times out after 6 seconds, accounts are queried concurrently on a shared thread pool, and the whole round is capped at 8 seconds (accounts that miss it fall back to their caches while their requests finish in the background) so latency does not grow with the account count. When the gateway is unreachable or some accounts fail, the page shows an explicit "n/m accounts got no fresh data" notice in a warning colour instead of spinning indefinitely; such failures are cached for only 30 seconds (successes keep the 5-minute cache) so a brief network blip self-heals on the next round.
- **Trae PAT accounts** — each quota card is titled `PAT #N（profile id）` and carries that account's local token/cooldown state on the same title row (read purely locally, never touching the network). Token refresh sits in the panel header and only fills in missing/expiring tokens; the per-model load section follows the account cards in a two-column grid (cached for 10 minutes), sorted by load descending with models that report no load sinking to the bottom. The operational Trae PAT panel is intentionally the final quota panel.
- **Account rename (alias)** — every qoder / kimi / antigravity quota card (and the trae per-account check-in rows) has an **✎** button next to the account title that opens a modal to edit the **display name** only (`alias` field via `POST /ui/api/{ch}/accounts/rename`, `{id, alias}`, empty string = back to the default name): credentials and failover priority are untouched, and the name is shared across the whole chain (quota card title and check-in rows show the same name). The alias lives in the account index, separate from credentials — re-login (upsert only rewrites the named `nickname`/`name`/`email` keys) and the index self-heal rewrite both preserve it.
- **Account subtitle rework** — the "token 剩 Xh" bit is gone from the qoder / kimi / antigravity account subtitles: access tokens auto-refresh within hours, so that number says nothing about how long the account remains usable (qoder even computed 497759890.1h once by subtracting an ms-epoch from seconds). Subtitles keep only the meaningful bits — region / cooldown (quota / account / likely-blacklisted) / missing project. Kimi's quota rows now report the upstream's **absolute credits** (`limits[]` / `usage` limit/used/remaining come first, `used_ratio` only as a fallback) — the old "剩 93.31/100" was really a percent wearing a credits costume. Qoder account cards gain a **total line** "剩 N / M · 已用 x%" under the title (subscription quota + add-on pack + dedicated credits summed — the three coexist as separate allowances; declared by the backend's `sum_items`).
- **Model toggle & schedule** — disable/enable an individual `(provider, model)` pair (a disabled pair fails fast), and restrict one to time windows such as `22:00–08:00` or `12:00–14:00`. Both persist to the settings file.
- **Model order (candidate failover)** — its own **Model order** tab shows **one card per key in `model_order`** — whatever the config lists is what the page shows, nothing expanded from the channel catalog. Each card expands to an interactive list of candidate upstreams you can add to, delete from, or **drag / ▲▼** to reorder. Requests try them top-down: when a channel fails **before anything was written to the client**, the proxy moves to the next one and marks the failed target with a 5-minute cooldown (extended to 1 hour on repeated failures), skipping it while it lasts. One failure is deliberately **not** marked: a **local DNS resolution failure**. That is your machine's problem, not the upstream's — it hits every candidate at once, so cooling them all would turn a one-second blip into five minutes of "model unavailable" with not even a re-probe, which is exactly what happened on 2026-10-02. Such a failure still moves on to the next candidate, it just leaves no mark. Not every machine-level failure is classifiable per-request, though: an hour after the DNS incident the same evening, the local proxy tunnel died and all three channels failed within 50 seconds — with genuine `502 Bad Gateway` bodies that are indistinguishable from a real upstream 502. So the cooldown layer also watches for the statistical signature: **≥2 distinct channels failing for the same model within 60 seconds** is treated as a machine-level burst — those targets are marked for only **30 seconds** (including retroactively shortening the one that failed first) and never escalate to the 1-hour tier, so the model becomes retryable the moment the network recovers instead of staying locked for 5 minutes. Only network-class failures count (transport errors and 5xx): 429 rate limiting is the upstream's own state, so two channels quota-ing out together keep their full cooldown. Once streaming has started there is no failover (the upstream would bill twice) — so CodeBuddy's in-stream errors never trigger it, and putting CodeBuddy last is the practical choice; those in-stream errors are at least recorded as failures in the request log rather than counted as a 200. The key is the **bare model name** (`deepseek-v4.1-flash`): it means "when this model is requested, try these channels in this order, the first by default", so one entry covers every channel that publishes the name. Targets are written as `provider/model`. An empty list restores the historical behaviour (route purely by model id). Persists to `model_order` in the settings file. A **clear-cooldown** button on the model row retries cooled targets immediately without touching the order.
- **Settings health banner** — `load_settings()` swallows a corrupt settings file by design (one bad comma must not take the proxy down), which used to mean *every* settings-backed feature silently vanished at once with nothing to go on. The admin UI now probes the file on load and shows a red banner at the top when it exists but cannot be parsed, quoting the raw JSON error.
- **Request log** — paginated request log read from `logs/metrics.jsonl` plus the 30-day archive, filtered by date range, provider, model and client. Filter and date selections are stored in URL query parameters, so refreshing or sharing the URL restores the same view. The page header also carries a **share-by-model** donut chart: slices are grouped by model name (a model served by several channels is one slice; hover breaks it down per channel), showing request counts, share and errors, and clicking a legend row focuses that model. It deliberately does **not** reuse the log query path — `query_logs` re-scans the JSONL and archives on every call (~38 ms for 30 days), while the donut reads an in-memory `(date, provider, model)` counter table on `MetricsCollector` that `record()` bumps by one and a query filters by date only (**0.023 ms**, ~1600x cheaper, so the 30-second poll is free). The provider/model filter buttons below also narrow the chart (the client filter cannot — the counter has no client dimension); the subtitle says so while a filter is active. The trade-off is a one-time full re-read of the current file plus the 30-day archives at startup (a few hundred ms at real line counts, counting only — token/latency aggregates are untouched). It reads whole files rather than the 2 MB tail that feeds the in-memory aggregates, so an archive over 2 MB does not silently lose its first half; when the window starts before the oldest archive the subtitle marks it `≈`. The **Credit** column shows the per-request amount when the upstream reports it (CodeBuddy, Qoder, and any channel that fills `usage`), falling back to a `≈` estimate — for trae, measured per-token input/output rates (`MEASURED_CREDIT_RATES`, reconciled against the official usage records) scaled by `current multiplier / calibration multiplier` so a `MODEL_CREDITS` edit tracks panel price changes automatically, with the multiplier-based rough estimate for models not yet measured; and the official GLM Coding Plan deduction coefficients (with cache-hit and time-of-day discounts) for zcode — and `—` when neither exists. Shown to 2 decimals like Qoder's own site; the raw value stays in `logs/metrics.jsonl`. Note **cached tokens are nearly free**: a 164k-input request that hit ~99% cache cost 0.12 credits, while a 12k-input request with no cache cost 0.31 — so a large input column does not imply a large bill.

The admin UI also lets you switch the **fallback provider** (used when a request matches no model); it persists in the same settings file.

Tabs load lazily and load in parallel on demand, and each tab shows a skeleton until its own data arrives — opening the UI no longer waits on the request log, which is the slowest endpoint because it reads and parses the whole JSONL tail on every poll. Manual refresh invalidates in-flight requests so a late-arriving old response can never overwrite a newer one.

Security: the `/ui/api/*` admin endpoints are **restricted to localhost (127.0.0.1)**. To manage the proxy from the LAN, set `BUDDY_PROXY_ADMIN_OPEN=1` (at your own risk). `/v1/*` proxy endpoints are unaffected.

## Providers

Every channel is optional: enable the ones you have subscriptions for, log in with `buddy login <provider>`, and `/v1/models` merges their catalogs. Multiple channels can be live at once; a bare model id that several channels declare resolves by registration order — use the explicit `<provider>/` prefix to pin one.

### CodeBuddy (default channel)

Tencent CodeBuddy / WorkBuddy subscription, via the IDE plugin auth (browser OAuth, `buddy login codebuddy`, alias `workbuddy`). Features:

- **Multi-account failover** — accounts live in `~/.buddy-proxy/codebuddy/` (`index.json` + one credential file per account, mode 0600). Re-logging the same account updates it in place (failover order unchanged); a new account joins at the end. On 401 (credential invalid) / 429 (quota exhausted, e.g. code 14018) the account is cooled down (60s / 5min, `Retry-After` honored) and the next account takes over — a single exhausted account no longer takes the whole channel down. The legacy `~/.codebuddy-session.json` is migrated to account #1 on first touch.
- **Daily check-in** — per-account activity check-in (streak credits), aggregated in the UI with per-account detail; claims every claimable account one by one.
- **Credits** — resource-pack summary per account (`CodeBuddy #N · …` groups in the admin UI), plus per-request usage records (`/ui/api/codebuddy/usage-records`).
- Account management: `GET /ui/api/codebuddy/accounts` plus `order` / `rename` / `delete` endpoints; the admin panel exposes ▲▼ reorder, ✎ rename, ✕ delete per account.
- **International edition (`codebuddyintl`)** — same upstream family with a different host: `https://www.codebuddy.ai`; the `/v2/plugin` protocol is byte-identical on CN and Global (verified 2026-10). Log in with `buddy login codebuddy --region global` (alias `codebuddyintl`); global accounts carry a `region` flag in the same account store and never mix with CN accounts. The channel is registered automatically on restart once a global account exists — route models explicitly as `codebuddyintl/<model>`, and the admin UI shows a separate "CodeBuddy 海外版" quota card (quota + ✎ rename; ▲▼/✕ stay on the CN card). The intl session is kept in `~/.codebuddy-session-global.json` (never in the CN legacy path, so legacy migration can't absorb an intl account). Its catalog is deliberately independent from the stale desktop `product-ide.json`: every bundled Claude 3.7/4.0, GPT-5 and Gemini 2.5 id now returns upstream `11102`. The published set was instead verified by real Global-account calls from the online multiplier panel (2026-10-10), retaining only the newest generation in each family: `hy4-preview` (x0.00), `gpt-5.6-sol` (x3.47), `gpt-5.6-terra` (x1.39), `gpt-5.6-luna` (x0.14), `gemini-3.5-flash` (x0.99), `glm-5.3` (x0.79), and `kimi-k3` (x1.62).

### Doubao provider (optional)

A built-in provider that drives the local Doubao desktop app (DoubaoWork.app) via the Chrome DevTools Protocol — it reuses the app's own login state and injects risk-control signatures inside the page's JS context. Pure stdlib, no Playwright. Enable with `--doubao`.

> **Key rule: let the proxy launch Doubao — don't open the app yourself first.**
> On the first `doubao` request (or a "Test" click in the admin UI) the proxy automatically
> starts the app with a CDP debug port (9223), connects to its embedded browser and reuses
> your login. If Doubao is already running without that port, the proxy refuses to kill
> your app and the first request fails with 502 "主 App 正在运行但未开启 CDP 调试端口" —
> fully quit Doubao (Cmd+Q) and retry; the proxy then launches it correctly.

| Symptom | Cause | Fix |
| --- | --- | --- |
| 502 "…未开启 CDP 调试端口" | Doubao was started before the proxy | Quit Doubao (Cmd+Q) and retry — the proxy relaunches it |
| requests fail after a Doubao update/restart | stale CDP connection | Quit Doubao and retry, or `buddy restart` |
| 401 "doubao not logged in" | login expired | sign in inside the Doubao app, retry |

Models: classic pipeline `doubao` / `doubao-think` / `doubao-expert` (the server routes to its default model — the `model` field is ignored) and agent pipeline `doubao-auto`, `doubao-2.1-turbo`, `doubao-2.1-pro`, `orange-5.0`, `gemini-3.7-flash`, `gpt-5.6-sol` (real per-model routing via the app's own model menu; optional `reasoning_effort` 3–7).

### DuMate provider (optional)

A built-in provider for Baidu's DuMate (千帆桌面端) desktop app. It connects to the app's embedded local OpenAI-compatible proxy (`dumate-main-server` on `127.0.0.1:<port>`), reusing your Baidu Cloud login to reach the `dumate-svc.baidu.com` gateway — no QR scan, no cloud token. The local auth key (`X-Dumate-Inapp-Key`) is read from the running app's process env and rotates each launch. Enable with `--dumate`.

- **Prerequisite**: DuMate.app installed, logged into a Baidu Cloud account, and **running**. After a reboot just reopen the app; `buddy login dumate` only does a status check.
- **Models** (packet-captured + individually verified 2026-10): `dm-auto-model/text.L0` (auto-router, the app default), `kimi-k3`, `qwen3.8-max`. 192k context / 128k output, function calling works, and your `system` prompt is passed through and billed verbatim (a 9.6k-char system was metered exactly into `prompt_tokens`). DeepSeek / GLM-5.3 / overseas models (Claude / GPT / Gemini) are not open to Baidu consumer accounts.
- **Check-in**: the auto check-in loop claims daily (`POST /api/dumate/points/loginBonus` via the bceConsole channel); the panel shows cumulative check-in points.
- **Quota**: numeric points via the app's own bceConsole endpoint (`GET /api/dumate/points/quota_overview`, the underscore variant — the camelCase one always 500s): subscription total/used/remaining plus per-package breakdown with expiry dates. Falls back to the local proxy's boolean `/api/dumate/points/remaining` when not logged in.
- **Usage records**: real per-call point consumption via `GET /api/dumate/points/records/usage` (second-level `startAt`/`endAt` timestamps; ms returns 500). The panel subtitle shows today's consumed points and call count; `GET /ui/api/dumate/usage-records?days=N&page=&limit=` serves the raw ledger.
- **Protocol**: OpenAI chat completions; Anthropic `/v1/messages` is converted back from the OpenAI-shaped response (same adapter as kimi/qoder), so Claude Code works directly.
- **Dependencies**: pure Python stdlib (includes a zero-dependency AES-256-GCM to decrypt the bceConsole cookie).

### Trae provider (optional)

Decrypts the Trae IDE's locally stored login state and talks straight to the underlying models.

```bash
buddy start
```

- **How it works** — decrypts the AES-128-CBC + SHA-512 `tc` blob in the local Trae IDE storage, or reads `TRAE_TOKEN` / `TRAE_USER_ID` from `.env`, then connects to the Trae gateway directly.
- **Catalog** — lists verified model IDs and any external aliases, ordered as the upstream client displays them.
- **Native channel** — since 2026-09 all requests (plain chat included) go through the `chat_v3` direct path: with `tools` present it does native function calling (structured `tool_calls` + `role:"tool"` history replay); plain chat gets no server-side agent preset, no suppression instructions and no leak scrubbing. Native requests rejected with `4001` fall back to the text protocol. Since 2026-10-07 the upstream rejects `chat_v3` **streaming** requests outright (same body succeeds non-streaming), so for a streaming client request the proxy first retries the native channel with `stream=false` (buffered upstream, still wrapped as an SSE response) and only falls back to the text protocol (also non-streaming) if that is rejected too. The fallback chain uses **post-failure pacing** against the upstream's fast-retry penalty: the upstream rejects a request fired right after a failed one with another `4001` — a single rejected streaming probe poisons the model for ~30-60s (non-streaming calls fail too), and the window grows into minutes under dense failures — so the proxy remembers the model's last `4001` and quietly waits until 25s past that moment before any further upstream attempt (`WB_TRAE_REJECT_QUIET_S` to tune) — waiting produces no failures, so the window decays instead of growing (and a successful native call clears the timestamp, so follow-up requests don't wait along); the reader thread keeps feeding heartbeats during the wait, invisible to the client. A rejected non-streaming call is paced and retried once before the chain moves on. After a rejection the model gets a ~30-minute TTL marker (`WB_TRAE_STREAM_REJECT_TTL_S` to tune — don't set it low, the probe itself poisons the pacing window) that skips the doomed streaming attempt entirely, so only one probe per TTL window pays the fallback tax (and the penalty window stops being fed). The direct channel also returns real token usage. Set `WB_TRAE_NATIVE_TOOLS=0` to always use the legacy prompt-taught text protocol.
- **PAT channel** — `traepat/<model>` addresses the underlying-model channel with multi-account failover: each account's `4031` / `4008` / `4011` codes are classified and cooled independently, exhausted channels fail fast with a `429` instead of probing every account, and cooldowns are cleared once real credits are confirmed back.
- **Quota** — free accounts have daily/weekly caps; when exhausted you get `4011` (today's usage limit reached), forwarded with a friendly Chinese message.
- **Dependencies** — pure Python standard library (including a zero-dependency AES fallback); no Node.js required.
- **Overseas edition (`traeintl`, since 2026-10)** — log in with `buddy login trae --region global` and a separate channel spins up automatically (`traeintl/<model>` addressing, its own quota card, no check-in — the overseas upstream has no such endpoint). Its model pool is **entirely separate** from CN (probe-verified 2026-10-06; `gpt-5.2` removed on user request 2026-10-07, 9 models): `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `kimi-k3`, `gpt-5.4`, `glm-5.2`, `minimax-m3`. Two protocol differences from CN: `messages[].content` must be a content-block array (a plain string gets a 400 deserialization error), and the Work function binding is a per-region table — gpt-5.6 family / `glm-5.2` / `minimax-m3` return `4001` under the default `solo_work_lite` and are routed to `chat_v3` automatically. Models visible in the IDE dropdown but rejected by the agent channel across all three functions (`gpt-6-astra` / `glm-5.3` / `deepseek-v4.1-flash` / `gemini-*-preview`) are not listed. Billing is **request-count + dollar mixed** (Pro plan): "Premium fast requests" 600/month (upstream never reports usage count — shown as "—") plus "Basic usage" in real dollars spent; `is_hide` packs (hidden from the upstream UI) are filtered out.

### Optional Trae account utility (`trae-cli`)

`trae-cli` is **not needed to sign in**. Use `buddy login trae` for Trae Work login: it opens the authorization URL in a browser and completes sign-in through the local callback. `trae-cli` is a separate optional utility for checking/claiming check-in credits, viewing entitlements, and sending a test chat. From the repository, run it through the project environment:

```bash
trae-cli status            # check-in / credit status
trae-cli claim             # claim today's check-in credits
trae-cli usage             # entitlements / usage (total, used %, pack list)
trae-cli chat -m glm-5.3 -q "hello"
```

`uv sync` installs this entry point in the project virtualenv; a package install exposes it in that Python environment. Auth is loaded automatically: the Work multi-account state dir `~/.buddy-proxy/trae/` (`index.json` + one 0600 cred per account; generated/appended by `buddy login trae`, upserted by uid/refresh_token — relogin updates, a new account appends) first. The legacy single-account `~/.buddy-proxy/trae_work.json` (and `~/.ethan/trae_work.json`) is auto-migrated to account #1 on first read. Then the decrypted local Trae IDE `storage.json`. No manual token setup.

The domestic Trae Work model catalog exposes lowercase IDs so `model_order` stays consistent across providers (for example, `doubao-seed-evolving` and `deepseek-v4-pro`). Requests are translated to the case-sensitive upstream names (such as `Doubao-Seed-Evolving`) automatically. The old `kimi-k2.7-code`, `glm-5.2`, `deepseek-v4-flash`, `glm-5`, `glm-5-turbo`, and `qwen-3.7-plus` entries have been removed.

Work channel multi-account **primary/backup failover**: account #1 by login order is preferred; account-level errors (401 credential expired / 429 quota) cool down for 60s/5min then fail over to the next. Upstream SSE quota codes (4008 quota exceeded / 4011 / 4021 / 4031) and auth codes (1001 / 4010) are mapped to 429/401 into the same classification (since 2026-10-06: previously everything passed through as 502 without switching, so with two accounts — one drained, one funded — the request still failed outright). Checkin/quota query and claim **every account**; the admin panel has `▲▼` reorder / `✕` delete (same as qoder/kimi/antigravity). With multiple accounts, the check-in status carries a per-account breakdown (`accounts`: index/name/state) and the check-in card renders one line per account (已签到 / 可领 / 查询失败 with the reason) — the aggregated badge alone could not tell which account actually failed; manual claims toast each account's result (who got +N, who failed). Each line's name follows the alias > nickname > uid > id display chain (an ✎ rename shows up here too, matching the quota panels) and carries an ✎ rename button of its own. Single-account setups stay unchanged (no breakdown, same badge semantics).

**Per-account check-in device fingerprint**: the check-in API is device-scoped (one claim per device per day; error codes change with the device). The early hard-coded "ASUS TUF + windows" fingerprint (the original trae2api recipe) has been blacklisted by the upstream risk engine — under that fingerprint any *new* device fails its first claim with 9074 "当前参与用户太多" (verified 2026-10-06: switching the model string succeeds immediately; status stays fine the whole time, so it is not campaign congestion or concurrency). Each account now owns one **stable** device: the model is derived from a pool of real machine strings keyed by the account hash, and after the first successful claim the `(device_id, brand, type)` triple is pinned into `~/.buddy-proxy/trae/devices.json` (0600) so every day uses the same device; when a claim hits 9074 the next candidate device is tried automatically and the pinned record is replaced. 9095 "当前设备今日已经签到" is treated as an idempotent success (message marks it as device-already-signed).

### ZCode provider (optional)

Zhipu **GLM Coding Plan** via its Anthropic-compatible endpoint, passed through directly:

```bash
buddy start
```

Credentials come from `ZCODE_API_KEY`, `~/.buddy-proxy/zcode_api_key` (override the directory with `BUDDY_PROXY_STATE_DIR`), or `~/.zcode/v2/config.json`. `ZCODE_OPENAI_BASE` overrides the base URL. Models are listed by `/v1/models` and appear under the `zcode/` prefix (e.g. `zcode/glm-5.3`).

There is no browser login to automate: the credential is an API key issued in the Zhipu console, and minting one is a manual click. `buddy login zcode` therefore just reports the current state and prints how to get a key — the console URL (<https://bigmodel.cn/usercenter/proj-mgmt/apikeys>), the plan purchase page (<https://bigmodel.cn/glm-coding>), and the three ways to configure it. Already have the ZCode CLI logged into coding-plan? That path works too; the key is read from its config.

When writing the key file, use `>` (overwrite) rather than `>>` (append): only the first non-empty line is read, so appending leaves the old key in effect and changing keys silently does nothing. Run `mkdir -p ~/.buddy-proxy` first if the directory does not exist yet.

The quota panel reads `/api/monitor/usage/quota/limit`. Its `limits[]` entries share one `type` (`CREDIT_LIMIT`) and distinguish windows by `unit` + `number`, not by the reset time: `unit=3`/`number=5` is the 5-hour window and `unit=6`/`number=1` is the monthly one. Name the windows from `unit`/`number` — deriving the name from "how far away is `nextResetTime`" gets both wrong, since the monthly window resets only a few days out (so it reads as "weekly") and the 5-hour window *has no* `nextResetTime` at all (so it degrades to a bare `CREDIT_LIMIT`). Rows are ordered smallest window first, so the 5-hour entry leads the card; ordering by `nextResetTime` instead drops the missing-timestamp 5-hour entry to the end and headlines the monthly bucket. Note the two windows are separate allowances to be read independently — they are not added together.

### ZCode Start Plan response errors

For `zcode-start/glm-5.3-flash`, a successful upstream reply must be an Anthropic Message (or a stream starting with `message_start`). An upstream HTTP 200 containing a quota/error JSON object or a stream with no valid start event now returns HTTP 429 for quota errors (including code `1308`) or HTTP 502 for malformed replies, rather than passing a misleading HTTP 200 to Claude Code. When model-order routing is configured, these pre-commit failures can fail over to the next candidate. The OpenAI-compatible streaming converter also accepts upstream SSE framed with either LF or CRLF, including line endings split across network chunks.

### GLM provider (official, optional)

The same GLM Coding Plan upstream as ZCode, but driven by **your own console-issued API key** instead of the ZCode CLI's credentials:

```bash
buddy start
```

`GlmProvider` subclasses `ZcodeProvider` — passthrough forwarding, SSE pumping, the model table and the quota endpoint are all inherited. The only difference is the credential chain: `GLM_API_KEY` or `~/.buddy-proxy/glm_api_key`, and **never** `~/.zcode` — the two channels' keys belong to independently purchased plans, and cross-reading them would bill one plan for the other's usage (and mix up the quota cards). Registered *after* zcode, so a bare `glm-*` request still lands on zcode when both are enabled; use the explicit `glm/<model>` prefix to pin the official key.

`buddy login glm` reports state and prints the key setup steps, same style as zcode (no automatable browser login — the key is minted by hand in the Zhipu console). Plan access measured 2026-10-06 (lite): `glm-5.3` / `glm-5.3-flash` / `glm-5-turbo` work; `glm-5.3-flashx` is refused with `1311 套餐暂未开放` until the plan is upgraded — the model stays in the table as a reserve, no code change needed when it unlocks.

### MiMo provider (optional)

Xiaomi **MiMo** (platform.xiaomimimo.com), exposed under the `mimo/` prefix (`mimo-auto`, `mimo-pro`):

```bash
buddy start
```

Two auth modes, tried in order:

1. **API key** — `MIMO_API_KEY` (plus `MIMO_BASE_URL` to pick the billing / token-plan host), `~/.mimocode/auth.json` (written by MiMo Desktop's "API Key" mode), or `~/.buddy-proxy/mimo_api_key.json`.
2. **Xiaomi SSO** — sign in with `buddy login mimo` (credentials land in `~/.buddy-proxy/mimo_account.json`); if you have never logged in, the proxy falls back to reusing the login state of an installed **MiMo Desktop** app. Either way it performs the same two-stage exchange the app does to obtain a short-lived `serviceToken`; tokens are refreshed automatically and a stale one is retried once. No clipboard or cookie export needed.

```bash
buddy login mimo     # opens a browser; the CLI picks up the result automatically
```

#### Sign-in (`buddy login mimo`)

Log in with a Xiaomi account in the browser — **nothing to paste, nothing to do back in
the terminal**. Same shape as `buddy login qoder` (device flow): generate a login link →
open the browser → long-poll for the result → write `~/.buddy-proxy/mimo_account.json` (`0600`).

> **Why not a local callback server** (the way `buddy login trae` works): Xiaomi's
> `callback` parameter is **server-side signed** and only whitelisted domains are accepted.
> A self-hosted `http://127.0.0.1:xxxx/cb` is rejected outright — `{"code":10025,
> "desc":"Callback连接不合法"}` — and so is Xiaomi's own `https://account.xiaomi.com/sts`
> (the signature is recomputed per request and can't be forged). So this uses the official
> `longPolling/loginUrl` endpoint instead (**no `callback`**, ticket-based).

Two details worth knowing (both found the hard way, 2026-09):

- **`loginUrl` is an API endpoint, not a page.** Opening it in a browser shows a raw
  `{"code":70016,"desc":"登录验证失败"}` blob. The real sign-in page (a SPA that renders
  the QR code client-side) is in that same response's `location` field
  (`account.xiaomi.com/fe/service/login?...`) — the CLI follows one hop to get it.
- **A browser User-Agent is required.** With a client UA the request 302s instead of
  returning the `location`-bearing body.

The ticket is valid for **300 s** (Qoder's device flow gets 10 minutes). The CLI pins its
poll deadline to that `expires_in` rather than a fixed constant — once a ticket expires,
the long-poll still hangs without returning any error, so there is nothing to react to but
your own clock.

Credential precedence: **the file written by the login flow wins**, and only if it's absent
does the proxy fall back to the desktop cookie DB — so a **fresh machine works without MiMo
Desktop installed**, and machines that do have it behave exactly as before. API keys still
outrank both (if you have one configured, logging in has no effect).

MiMo is OpenAI-shaped upstream, so requests are forwarded as-is — **except** Anthropic (`/v1/messages`) clients such as Claude Code, where the response is converted back into Anthropic events (streaming `message_start` / `content_block_delta` / `message_stop`, plus `thinking` and `tool_use` blocks) because MiMo has no native Anthropic endpoint.

The admin UI shows a quota panel with two rows: **weekly quota used** (the upstream reports *remaining* percent, which the UI converts to used) and **plan validity** (days elapsed out of the plan term). Note the two are different periods: the quota window is **7 days anchored at the subscription start**, while the plan term is usually **30 days**.

> `--mimo` is only needed when you want this channel; without it the provider is not registered and `mimo/...` model names fall through to the fallback provider.

### Qoder provider (optional)

Alibaba's **Qoder** IDE (qoder.com global / qoder.com.cn CN), exposed under the `qoder/` prefix:

```bash
buddy start
buddy login qoder     # device flow (PKCE S256); picks the region interactively
```

The client talks to Qoder's **COSY-signed** face (`/algo/api/v2/service/pro/sse/agent_chat_generation`) — the same endpoint the official IDE uses, and the only one serving the Qwen3.8 models. Signing is reimplemented in pure Python (no extra dependencies, no vendored wasm): `Authorization: Bearer COSY.<payload>.<sig>` plus the mandatory `Cosy-User` header, with the request body in Qoder's custom-alphabet encoding. Global and CN both need signing — the region only changes *how the token is obtained*.

**Model names are lowercase real names**, not the upstream codenames:

| Model id | Upstream key | Notes |
|---|---|---|
| `qoder/qwen3.8-max` | `qmodel_38max` | reasoning + vision |
| `qoder/qwen3.8-flash` | `qfmodel` | reasoning + vision |
| `qoder/glm-5.3` / `qoder/glm-5.3-flash` | `gmodel` / `gfmodel` | |
| `qoder/kimi-k3` | `kmodel_latest` | |
| `qoder/deepseek-v4-pro` | `dmodel` | |
| `qoder/auto` | `auto` | platform-routed tier — the only tier upstream actually has; currently gated off upstream (directory `enable=false`) |

Older models (Qwen3.7 series, GLM-5.2, Kimi-K2.8-Preview, MiniMax-M2.7, Cantus, Sonus, DeepSeek-Flash) are **hidden from the list but still callable** — just less clutter in `/v1/models`. All three spelling forms work: the public id, the official display name (`Qwen3.8-Flash`), and the raw upstream key (`qfmodel`); the upstream key is echoed back as `upstream_key` for troubleshooting.

**The model catalog is per-account and region-scoped** — a full account reports all 14 models while a restricted one may only see the Qwen3.8 pair (risk control; the gate is account-level, and it has been seen to drift). `refresh_models` therefore walks **every account in this provider's region** and publishes the union ("at least one account supports it"); a background loop fetches it on startup and re-fetches every `CACHE_TTL_S` (the catalog lives in memory only — without the warmup a restart used to leave the list at the bundled static fallback, which is how the UI once showed just 2 qoder models). When specific accounts lost specific models, the per-model allowlist lives in `qoder/models.json` (shipped next to the provider): entries `{"id": "glm-5.3", "accounts": ["<uuid>", ...]}` list the account ids (UUIDs, shown on the account card) that **support** the model — a model not listed, or `accounts: "all"`, means every account. Forwarding **skips** accounts that do not support the requested model (no cooldown, no failure — the account is healthy, it just lacks the entitlement) and fails fast with 404 when no account supports it at all, instead of paying a slow read-timeout / in-band 400 per unsupported account. The bundled fallback catalog mirrors the full-account measurements (third-party models restored with measured price factors); the admin UI tags limited models 「部分账号」 (hover for the supporting accounts) and each qoder account card notes its restrictions with the full model list in a hover tooltip. Editing the JSON requires a restart (loaded once, deliberately static).

**Qoder Global (`qoderintl`)** is registered when Buddy starts with `--qoder` and a Global Qoder account already present. It has a separate quota card and check-in row, while reusing Qoder's quota and campaign check-in implementation. The Intl quota card is read-only and intentionally does not call the CN-only account reorder/delete APIs; account cards are titled by account name (alias first) so multiple Global accounts are distinguishable. If a Global account is added after Buddy has started, restart Buddy so the provider is registered; no separate UI sign-in implementation is needed.

Anthropic clients (`/v1/messages`, e.g. Claude Code) are supported: the proxy converts the OpenAI-shaped upstream stream into `message_start` / `content_block_delta` / `message_stop` events, including `thinking` blocks from the model's reasoning output.

Three upstream quirks are normalised per-message in `_build_upstream` before sending (all
would otherwise reject the whole request, and all are Qoder-specific — the shared converter
is left alone):

- **`developer` role is rejected at deserialisation** — rewritten to `system`, and its
  `tool_calls` (if any) dropped: `tool_calls` may only hang off an `assistant` message, so a
  `system` message carrying them poisons the `tool` reply that follows.
- **A message carrying `tool_calls` may not have `content: null`.** Anthropic's tool_use-only
  turn converts to exactly that, so any session that had *used a tool* failed while a plain
  question succeeded. The upstream's error for this is misleading — it reports
  `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`,
  which sends you hunting in the tool-pairing code — so it is rewritten to `""` here.
  The rewrite keys off `tool_calls` rather than the role, and runs *before* the `tool_calls`
  are stripped above; a plain `content: null` assistant turn is legal and left untouched.

Upstream in-band errors carry the real cause in a `details` field (`message` alone is just
`Error in upstream response`); `_describe_upstream_error` surfaces it, and failures are
returned in Anthropic's `{"type":"error","error":{...}}` shape so Claude Code recognises them
as terminal instead of retrying ten times.

### Per-request credit cost

Qoder's upstream returns the **exact** amount charged, so nothing is estimated: every SSE
stream ends with a usage chunk carrying `credits`, `original_credits` and `billable`
alongside the token counts. The proxy reads it through and the admin UI's **Credit** column
shows it directly (2 decimals; the full-precision value stays in `logs/metrics.jsonl`).

Note the field is spelled **`credits`** (plural) here and **`credit`** (singular) on
CodeBuddy — reading only one silently drops the other channel's data, which is exactly why
Qoder requests used to log `credit: null`. `core/metrics._credit_field` accepts both, plus
`original_credits` as a fallback.

**Cached tokens are nearly free**, which is why a request with a huge input column can still
cost almost nothing. Measured on a live account:

| input | cached | uncached | output | credits |
|---:|---:|---:|---:|---:|
| 164,166 | 163,968 | 198 | 525 | 0.124 |
| 12,030 | 0 | 12,030 | 505 | 0.312 |

So ~136k-input requests billed at 0.07–0.13 are expected, not a bug: roughly 99% of the
input hit cache.

### Daily activity credits (check-in)

Qoder runs a **daily "claim 100 Credits" campaign** (the popup the desktop app opens on
launch). It is served from a different face than chat — `{openapi}/sash/api/v1/me/campaigns` —
and, unlike `/algo/**`, it **needs no COSY signature**: a plain `Authorization: Bearer <dt-token>`
works (the desktop app calls it from the Electron main process, logged as `requestSource: "native_main"`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sash/api/v1/me/campaigns` | list campaigns + claim status |
| `GET` | `/sash/api/v1/me/campaigns/{id}/reward` | grant result |
| `POST` | `/sash/api/v1/me/campaigns/{id}/claim` | **claim** |

The Qoder channel declares `supports_checkin`, so it shows up in the admin UI's
**打卡 & 额度** panel next to CodeBuddy and is included in auto check-in. With multiple
accounts, status is checked per account and shown as a breakdown, so a first account with
only `VIEW_DETAILS` cannot hide a later account's check-in reward; a manual claim selects
the first claimable account in priority order. If the upstream returns only non-claimable
campaigns such as `VIEW_DETAILS`, the UI says an activity exists but has no claimable
check-in reward, rather than incorrectly saying there is no activity.

**Check-in campaigns are targeted per machine fingerprint** (verified 2026-10-07, both CN
and Global): the upstream only hands today's `CLAIM_BENEFIT` entry to devices it recognizes
via the `Cosy-Machine*` headers. With only a static `machine_id`, an account that has never
been claimed through the proxy (e.g. only used on the desktop app) gets a campaign list
without the check-in entry — the desktop app, same account, still sees it, and the panel
wrongly reports "activity exists but no claimable check-in reward". The proxy now mirrors the
desktop: it shells out to the app-bundled `runtime-info` native binary (under
`Contents/Resources/umid/` of `/Applications/Qoder.app` / `Qoder CN.app`) to generate a
per-account fingerprint and sends the real `Cosy-MachineToken/Code/Type`. Without the binary
(desktop app not installed) or on failure it falls back to the static headers — accounts
already registered with the static fingerprint keep working, fresh ones will not see the
campaign. Paths can be overridden via the `QODER_UMID_BIN` / `QODER_UMID_BIN_CN` env vars. A second, read-only fallback covers the remaining process-session targeting case: if the direct `/sash` result still contains no `CLAIM_BENEFIT`, the proxy scans the newest 2 MiB of the installed desktop app's fresh `main.log` and imports only a current-window entry whose payload `uid` exactly matches the account. A direct claim entry always wins; stale logs, another uid, malformed rows and future/expired windows are ignored, and a desktop claim replaces only the same-id `VIEW_DETAILS` placeholder.

Three behaviours worth knowing (all verified against a live account, 2026-09):

- **It is per-day, keyed by a new campaign id.** The window is literally `10:00 → next 09:59`
  (UTC+8), and `campaignKey` increments daily (`act-20260923-556` → `-557`), with `campaignId`
  a fresh UUID each day. Always re-list; never cache the id across a day boundary.
- **Claiming is idempotent.** Re-claiming an already-claimed campaign returns
  `200 {"status":"CLAIMED","replayed":true}` — that is a *replay*, not a new grant
  (`claimedAt` is the past one). Only `replayed: false` means credits were actually granted.
- **Don't trust the top-level `claimable` flag** to decide whether to claim: it also covers
  `VIEW_DETAILS` campaigns and unopened windows. The real test is per entry:
  `actionType == "CLAIM_BENEFIT" && claimStatus == "CLAIMABLE"`.

Activity grants like these land in `/api/v2/quota/usage` as **dedicated resource packages**
(`dedicatedResourcePackages`), which coexist with the subscription quota and add-on packs —
they are part of the account's total allowance, each with its own (earlier) expiry. The admin
UI's quota panel shows every available package as its own row, so the total matches what the
account really has; packages with `available: false` (expired/invalidated) are skipped.

> `--qoder` is only needed when you want this channel; without it the provider is not registered and `qoder/...` model names fall through to the fallback provider.

### Gemini provider (optional)

Google's **Gemini CLI** free quota (Code Assist for individuals), exposed under the `gemini/` prefix:

```bash
buddy login gemini   # Google OAuth (PKCE + local callback)
buddy start
curl http://127.0.0.1:8787/v1/chat/completions -d '{"model":"gemini/gemini-2.5-flash","messages":[...]}'
```

The gateway talks to the same `v1internal:generateContent` endpoint as the real gemini CLI, with the CLI's own request fingerprint (UA, `x-goog-api-client`, no `safetySettings` — see `src/buddy_proxy/gemini/README.md` for the full alignment table and the risk notes).

**Cache hits are reported** — upstream `usageMetadata.cachedContentTokenCount` maps to OpenAI's `prompt_tokens_details.cached_tokens` (`prompt_tokens` already includes the cached part, OpenAI convention — no deduction here). The Anthropic-protocol exit converts it to `cache_read_input_tokens` and discounts `input_tokens` accordingly, so the request log's cached-token column fills in too. The gemini and antigravity channels share these converters, so both get it at once.

**Login is interoperable with the local `gemini` CLI** — both sides use the same OAuth client, so the credentials are mutually recognized:

- After `buddy login gemini` succeeds, the credentials are written back into `~/.gemini` in the CLI's own format (`oauth_creds.json`, `settings.json` auth type, `google_accounts.json`) — the `gemini` command has a login state immediately, without running its own login.
- When `~/.gemini/oauth_creds.json` already holds a usable login, `buddy login gemini` offers to reuse it (default yes, no browser round-trip); expired access tokens are refreshed with the same OAuth client, and onboarding is completed automatically.

Start the proxy with the configured defaults using `buddy start`. Models (free tier, community-measured: ~250 req/day flash / ~100 req/day 2.5-pro, upstream 429s pass through as-is):

| Model id | Upstream | Notes |
|---|---|---|
| `gemini/gemini-2.5-flash` / `-pro` / `-flash-lite` | Gemini 2.5 series | list lives in `src/buddy_proxy/gemini/models.json` (`verified` flags update after a real run) |
| `gemini/gemini-3-pro-preview` / `gemini-3-flash-preview` | Gemini 3 preview | may 404 until Google enables the channel for the account |

Free-tier prompts may be reviewed by Google for training (the onboarding response says so) — keep sensitive content off this channel.

> `--gemini` is only needed when you want this channel; without it the provider is not registered and `gemini/...` model names fall through to the fallback provider.

> **Note (Oct 2026):** Google product-retired the Gemini CLI free tier on 2026-06-18 (`UNSUPPORTED_CLIENT` at onboarding; confirmed with the official CLI 0.33.1/0.62.0 as well). Personal/free accounts should use the Antigravity channel below; this gemini channel keeps working for standard-tier (paid/`GOOGLE_CLOUD_PROJECT`) setups.

### Antigravity provider (optional)

Google's **Antigravity** quota (the official successor to the Gemini CLI free tier; both personal free and Google AI Pro tiers land here), exposed under the `antigravity/` prefix. One OAuth login unlocks **Gemini 3.x, Claude Sonnet/Opus and GPT-OSS** models; quota is two independent pools (a Gemini group and a Claude/GPT group), each with a weekly + 5-hour rolling limit shared by the models inside the group:

```bash
buddy login antigravity   # Google OAuth (PKCE + local callback); reuses the local agy CLI login if present
buddy start
curl http://127.0.0.1:8787/v1/chat/completions -d '{"model":"antigravity/claude-sonnet-4-6","messages":[...]}'
```

**Login imports from the local `agy` CLI (official Antigravity CLI)** — agy keeps its OAuth token in the system keychain (`security find-generic-password -s gemini -a antigravity`), and `buddy login antigravity` offers to adopt it directly (default yes, no browser round-trip; expired access tokens are refreshed with the same OAuth client, onboarding completed automatically). The import is read-only — agy has no plaintext config files to write back.

The gateway talks to `cloudcode-pa.googleapis.com/v1internal:streamGenerateContent` (daily endpoint first, prod fallback) with the Antigravity client fingerprint (`X-Client-Name`, identity system-instruction and request envelope per the community-verified shape — see `src/buddy_proxy/antigravity/README.md`). Upstream only accepts the variant names from its `fetchAvailableModels` list (bare `gemini-3.x` names get a fake 429), so `reasoning_effort` maps to the `-low/-medium/-high` suffix per the model table (`efforts`/`default_effort` in `models.json` — e.g. `gemini-3.1-pro` defaults to `-low`, `gemini-3.8-flash` sends as `gemini-3.8-flash-tiered`, `gpt-oss-120b` as `gpt-oss-120b-medium`). The models-tab refresh walks every available account, unions its `fetchAvailableModels` results, strips only recognized effort suffixes (never identity-bearing endings such as `-thinking`/`-image`/`-agent`), preserves hand-verified metadata for known entries, and makes newly discovered models immediately routable. Kimi uses the same UI capability against its official `GET /v1/models`; both catalogs are instance-local and keep the previous snapshot when all accounts fail.

**Thought signatures survive protocol conversion** — gemini models return a `thoughtSignature` on every `functionCall` part and the upstream then *requires* it back on multi-turn tool calls (missing → `400 Function call is missing a thought_signature`); the claude/gpt-oss models instead require `functionCall.id` and `functionResponse.id` to be sent back as a pair. The signature only has a home in an OpenAI-only field, so an Anthropic client (Claude Code etc.) would drop it — the gateway therefore loops it through the one channel both protocols faithfully round-trip, the tool-call id: responses mint a unique id and stash the signature + function name in a small in-process LRU (`gemini/thought_signature.py`), requests restore by id, pair the `fc.id`/`fr.id`, and fall back to the upstream-accepted sentinel `skip_thought_signature_validator` when the cache misses (e.g. after a gateway restart). All three behaviors verified against live upstream 2026-10-03 (matrix in the module docstring).

Models (verified against a real account; list lives in `src/buddy_proxy/antigravity/models.json`):

| Model id | Upstream name | Notes |
|---|---|---|
| `antigravity/gemini-3.1-pro` | `gemini-3.1-pro-low` (default) / `-high` | Gemini group quota |
| `antigravity/gemini-3.6-flash` | `gemini-3.6-flash-medium` (default) / `-low` / `-high` | Gemini group quota |
| `antigravity/gemini-3.8-flash` | `gemini-3.8-flash-tiered` (auto tier) | Gemini group quota |
| `antigravity/gemini-3.1-flash-image` | `gemini-3.1-flash-image` | Image generation via `chat/completions`; the image comes back in `choices[0].message.images[]` (Anthropic exit: `image` content block) |
| `antigravity/claude-sonnet-4-6` / `claude-opus-4-6-thinking` | bare names | Claude/GPT group quota |
| `antigravity/gpt-oss-120b` | `gpt-oss-120b-medium` | Claude/GPT group quota |

Quota shows up in the admin panel when `fetchAvailableModels` is reachable: one bar per group (the tightest model's watermark inside the group), remaining on a 0–1000 scale (e.g. `989.9 / 1000`) with the next reset time. Not reachable (or not logged in) it degrades to a static note.

**Multiple accounts with automatic failover** — run `buddy login antigravity` again with a different Google account to append a backup (logging in with the same email just refreshes that account's credentials and keeps its priority). Accounts are tried in priority order, which starts as login order and can be **rearranged from the admin panel**: hovering an account card in the Antigravity panel reveals ▲▼ buttons (multi-account only) that resubmit the full account id list to `POST /ui/api/antigravity/accounts/order` and rewrite `priority` to 0..n-1 — a partial or duplicated list is rejected with 400; `added_at` (the login fact) is preserved. The quota cache key carries `priority` (`quota_epoch`), so a reorder invalidates the cached allowance snapshot and the next refresh shows the new order — and the panel now actually swaps on the spot: the post-write refresh waits out any in-flight request and re-issues a real refetch instead of being trumped by the stale pre-write response. Each account card also carries a **↻ refresh** button (same on the kimi panel): `POST /ui/api/benefits/refresh` invalidates just this channel's quota cache and re-queries immediately (`benefits.invalidate_quota`, prefix-based so epoch-suffixed keys die too; checkin state is untouched), no need to wait out the 5-minute TTL. The card title row's **✎ rename** button (same on qoder/kimi/antigravity quota cards and the trae per-account check-in rows) opens a modal to edit the **display name** (`alias` via `POST /ui/api/{qoder|kimi|antigravity|trae}/accounts/rename`): display-only — credentials and priority are untouched, empty input restores the default name (email / official nickname). The alias is a separate index field: re-login (upsert only rewrites the named `nickname`/`name`/`email` keys) and the index self-heal rewrite (`kept.append(AccountRef(...))` is the one place fields are rebuilt from an entry) both keep it; display names use one chain everywhere (alias > name/email/nickname > id) on quota cards and check-in rows alike. Note the upstream reading semantics: `fetchAvailableModels`'s `remainingFraction` only drops as usage **approaches** the pool cap — light daily use still reads 1.0, so a full-looking bar does not mean zero consumption (the consumption-driven 5h `resetTime` is what shows the pool was actually touched). When the primary hits 429 (quota) / 403 / dead credentials it is put on an in-memory cooldown (429 honors `Retry-After`, default 5 min; plain 403/credential issues 60 s; **403 "Verify your account to continue." means Google has blacklisted the account** — it still logs in fine but the upstream rejects everything, so it cools down for 6 hours and the panel subtitle flags it as "疑似拉黑 / likely blacklisted") and the next account takes over; the request itself is replayed on the fallback transparently. Business 4xx (bad model name etc.) is passed through without switching. In-memory cooldowns only — nothing is persisted, a restart re-probes every account. When all accounts are cooling down, forwarding fails fast with 429; when every account was tried and failed, the error spells out each account's state (who is quota-cooling for how long, who looks blacklisted) instead of just the last HTTP error. **Upstream timeouts are tiered** (measured 2026-10-03: first byte p50 4.8 s / p90 13 s / p99 57 s on successes, non-stream successes max 16 s — while the old 600 s read timeout made each hung call burn 10 minutes): streaming 90 s (cap between consecutive reads — covers both a stalled pre-first-event gate and a stalled mid-stream), non-streaming 120 s (effectively the total cap), connect 15 s. Timeouts and network errors no longer break failover (they used to raise 504/502 straight through the account loop): the current account gets a short 60 s cooldown and the next account retries (during a machine-wide outage every account cools briefly = channel-level fail-fast, re-probed automatically); clean EOF still cools nothing. The failover loop also carries a 180 s **attempt budget** — once earlier accounts have burned the time, no new attempt is opened (already-committed streams are unaffected; a stalled stream is bounded by the read timeout instead). Streams never double-bill: the first upstream event is held back, and an in-band 429/403 error before any semantic event switches accounts; once real content has started, the stream is never replayed. The gate opens the response's line iterator exactly once (httpx streams are single-consume — reopening raises `StreamConsumed` and silently drops everything after the buffered lines, which used to blank claude streams entirely); both the replay adapter and the pre-switch drain continue that same iterator.

Credentials live under `~/.buddy-proxy/antigravity/` (`index.json` + one 0600 JSON per account, cap 8). The legacy single-account `~/.buddy-proxy/antigravity_oauth.json` is migrated automatically as account #1 on first access (old file kept as backup). Remove an account via the ✕ button on its card (`POST /ui/api/antigravity/accounts/delete`, confirmed through the in-app modal, not the browser's `confirm()`): index entry, cred file and cooldown marks go together — cleaner than hand-deleting the JSON (which the index self-heal also tolerates). The admin UI shows a dedicated Antigravity panel (Trae-PAT style): one block per account — titled with the account name — the ✎ alias when set, the email otherwise — subtitled with its status (cooldown / likely-blacklisted / missing-project) — followed by that account's own quota bars. Quota snapshots invalidate immediately when the account list changes (cache key carries an account fingerprint), so a freshly added account never shows stale single-account data.

> `--antigravity` is only needed when you want this channel; without it the provider is not registered and `antigravity/...` model names fall through to the fallback provider.

### Codex provider (optional)

Turns a **ChatGPT subscription** (Plus / Pro / Team / Free) into an upstream channel, addressed as `codex/<id>`:

```bash
buddy login codex    # browser OAuth (loopback callback on 1455); paste the callback URL if no browser
```

- **No Codex CLI required.** Login uses OpenAI's public OAuth client (PKCE S256) — the same authorization Codex CLI uses — so it works on a machine that never installed Codex. Conversely, an existing `~/.codex/auth.json` can be pasted into the admin panel's "导入账号" dialog (or imported from another machine).
- **Protocol.** The upstream speaks the **Responses** API (`https://chatgpt.com/backend-api/codex/responses`, SSE), unlike every other channel's Chat Completions. The channel ships a bidirectional projection: requests are converted chat → Responses (system → `instructions`, tools flattened, assistant-history reasoning is *not* replayed since Responses reasoning items need an upstream signature); responses are dispatched per entry point — `openai` converts Responses SSE to chat deltas, `responses` passes the upstream stream through untouched, `anthropic` converts further into Anthropic events.
- **Multi-account failover.** Logging in again with another ChatGPT account appends a backup. 401 forces one token refresh and then switches; 429/403 cool the account down (`Retry-After` honored) and switch; 5xx passes through without switching. OpenAI **rotates `refresh_token`** — the new RT is persisted immediately. The admin panel offers ▲▼ reorder, ✎ rename, ✕ delete and import.
- **Quota.** The ChatGPT backend exposes no public quota endpoint, so the card only shows each account's plan tier and login state instead of inventing a `used_percent` (the `rate_limits` parser hook lives in `codex/upstream.py::parse_rate_limits`).
- Catalog: `gpt-5.5` (default), `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`, `gpt-oss-120b-medium` (taken from Codex 0.142's `models_cache.json`).

## Connect clients

### Codex CLI

`~/.codex/config.toml`:

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local proxy)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[profiles.codebuddy]
model = "glm-5.3"
model_provider = "codebuddy"
```

```bash
codex --profile codebuddy "your task"
```

### Claude Code / CC Switch

```json
{
  "DeepSeek-V4": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### OpenCode

`opencode.json`:

```json
{
  "model": "codebuddy/glm-5.3",
  "providers": {
    "codebuddy": {
      "name": "CodeBuddy (via local proxy)",
      "package": "@opencode-ai/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8787/v1", "apiKey": "noop" },
      "models": {
        "glm-5.3":         { "modelID": "glm-5.3",         "name": "GLM-5.3" },
        "deepseek-v4-pro": { "modelID": "deepseek-v4-pro", "name": "DeepSeek V4 Pro" },
        "kimi-k2.7":       { "modelID": "kimi-k2.7",       "name": "Kimi K2.7" }
      }
    }
  }
}
```

### Grok CLI

`~/.grok/config.toml`:

```toml
[models]
default = "hy3"

[model.hy3]
model = "hy3"
base_url = "http://127.0.0.1:8787/v1"
name = "HY3 Main"
api_key = "noop"

[model.dv4f]
model = "deepseek-v4-flash"
base_url = "http://127.0.0.1:8787/v1"
name = "DeepSeek V4 Flash"
api_key = "noop"
```

### Oh My Pi (OMP)

`~/.omp/agent/models.yml`:

```yaml
providers:
  codebuddy:
    baseUrl: http://127.0.0.1:8787/v1
    api: openai-completions
    auth: none
    models:
      - id: hy3
        name: Hy3 (CodeBuddy)
        reasoning: true
        contextWindow: 192000
        maxTokens: 64000
      - id: deepseek-v4-flash
        name: DeepSeek V4 Flash (CodeBuddy)
        reasoning: true
        contextWindow: 1000000
        maxTokens: 50000
```

### Other OpenAI-compatible clients

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: blank (or your `--api-key`)
- Model: any id from `/v1/models`, e.g. `glm-5.3`, `deepseek-v4-pro`, `kimi-k2.7`, `auto`

---

## Command-line options

```
--host HOST               bind address (default 127.0.0.1; proxy.sh uses 0.0.0.0)
--port PORT               bind port (default 8787)
--endpoint ENDPOINT       CodeBuddy backend address
--session-file PATH       session file (default ~/.codebuddy-session.json)
--log-file PATH           JSONL log (default <project>/logs/buddy-proxy.jsonl, override via BUDDY_PROXY_LOG_FILE)
--desensitize             enable desensitization (recommended)
--optimize-context        enable message compression (recommended for Codex)
--default-model MODEL     default model, e.g. zcode/glm-5.3; used when a request has no `model`
                          field. Seeded into the settings file on first start; afterwards
                          ~/.buddy-proxy/settings.json (editable from the admin UI) wins
--default-provider NAME   fallback channel for model names that match no provider
                          (codebuddy/zcode/trae/doubao/mimo, default codebuddy)
--trae                    enable the Trae provider (decrypts the Trae IDE login)
--zcode                   enable the ZCode provider (Zhipu GLM, Anthropic passthrough)
--glm                     enable the GLM official provider (BigModel Coding Plan key,
                          same upstream as zcode but independent credentials)
--doubao                  enable the Doubao provider (drives the desktop app over CDP)
--mimo                    enable the MiMo provider (API key or MiMo Desktop login state)
--qoder                   enable the Qoder provider (COSY-signed, Qwen3.8/GLM/Kimi)
--gemini                  enable the Gemini provider (Google OAuth, Code Assist free quota;
                          login state is shared with the local gemini CLI)
--login                   browser login at startup (opens the browser; prints the login URL)
--no-browser              don't auto-open a browser. Implicit/background re-auth (e.g. the
                          auto-checkin poll) never opens a browser and never prints a login
                          URL regardless; it only logs one `[Auth] ...` line. Use --login
                          when you actually want the interactive login link
--verbose-llm             emit expanded safe diagnostics (never logs request/response bodies, tokens, or UIDs)
--mock-dir DIR            serve recorded fixtures (testing)
```

Env vars: `BUDDY_PROXY_HOST`, `BUDDY_PROXY_PORT`, `CODEBUDDY_ENDPOINT`, `CODEBUDDY_MODEL`, `BUDDY_PROXY_LOG_FILE`, `BUDDY_PROXY_SETTINGS` (settings file path), `BUDDY_PROXY_STATE_DIR`, `BUDDY_PROXY_ADMIN_OPEN=1` (lift the localhost-only restriction on admin endpoints), `PROXY_DEFAULT_PROVIDER` (fallback channel, default `codebuddy`), `TRAE_ENABLED` / `ZCODE_ENABLED` / `DOUBAO_ENABLED` / `MIMO_ENABLED` / `QODER_ENABLED` / `GEMINI_ENABLED` (`1` enables that provider, same as the flags), `TRAE_TOKEN` / `TRAE_USER_ID` (skip Trae IDE decryption and use these directly), `ZCODE_API_KEY`, `ZCODE_OPENAI_BASE`, `MIMO_API_KEY` / `MIMO_BASE_URL`, `BUDDY_CLIENT_NAMES_FILE` (override the client-name map used in the request log).

Trae stream tuning: `WB_TRAE_HEARTBEAT_INTERVAL` (heartbeat while waiting for the buffered upstream response, seconds, default 45, `0` disables — keeps clients with per-chunk timeouts like Ethan's 120s from aborting long generations), `WB_TRAE_NATIVE_TOOLS` (native channel for all Trae requests — native function calling for tool requests, preset-free chat for plain ones; default `1`; `0` falls back to the legacy prompt-taught text protocol with leak guards), `WB_TRAE_IDE_VERSION_CODE` (Trae client version header, default `20260906` — the upstream gates per-model capabilities by this header; raise it when a model suddenly 4001s), `WB_TRAE_NONSTREAM_MAX_S` (non-streaming aggregation cap), `WB_TRAE_SEMANTIC_TIMEOUT`, `WB_TRAE_TOKEN_KEEPALIVE_S` (background token refresh interval).

Trae PAT rate limits: some extended models are rate-limited per account and per model upstream (queue-full / too-many-requests codes). Before any content is sent, the PAT channel switches to another account; when every account is limited it returns 429 so `model_order` fails over to the next target (429 targets cool down for only 60s and never escalate). A model can declare `"rate_limit": {"requests": N, "window_s": W}` in `models_config.json` to be paced proactively; when every account's window is full the request waits up to `TRAE_PAT_PACE_MAX_WAIT_S` seconds (default 30, heartbeats keep flowing) before giving up with 429.

## API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/ui`                 | admin UI (`/` 302-redirects here) |
| GET  | `/ui/api/*`           | admin API (overview / models / stats / benefits / settings / checkin / test / logs / model-toggle / model-schedule / traepat accounts & model-status, localhost only) |
| GET  | `/health`             | service + auth status |
| GET  | `/v1/models`          | model list |
| POST | `/v1/chat/completions` | OpenAI chat (tools + streaming) |
| POST | `/v1/responses`       | Responses API (Codex CLI) |
| POST | `/v1/messages`        | Anthropic Messages (Claude Code) |

Endpoints authenticate using the local session; no extra token is required.

## Disclaimer

For learning and research purposes only. Please comply with CodeBuddy's Terms of Service. Use at your own risk.
