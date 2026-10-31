# Architecture

`buddy-proxy` is a local FastAPI gateway that exposes OpenAI Chat Completions,
Responses, and Anthropic Messages APIs over several upstream providers.

## Package layout

The flat package root was reorganized into domain subpackages (no
backward-compat shims — imports point at the new paths):

```text
src/buddy_proxy/
  __main__.py            # CLI entry + repo-root resolution (parents[2])
  trae_provider.py       # published trae-cli entrypoint + trae façade
  core/                  # state, settings, paths, logging_setup, metrics,
                         #   credit_estimate, desensitize, checkin, errors
  protocols/             # anthropic_adapter, responses_adapter,
                         #   responses_projection, dsml_parser (解析层),
                         #   dsml_scanner (底层扫描原语)
  web/                   # routes, ui, model_list + models_config.json + static/
  providers/             # base.py (BaseProvider), zcode.py, glm.py (官方 key 子类)
  auth/                  # login, trae_work_login, trae_work_login_server
  codebuddy_provider/    # default provider pkg; client.py = CodeBuddy HTTP client
  trae/                  # trae domain (credentials, transport, sse, pat, ...)
  qoder/                 # qoder domain (provider + convert/errors/quota helpers)
  doubao/                # doubao domain (provider, cdp_client, payloads, ws)
  dumate/                # DuMate domain (discovery = local-proxy discovery + inapp-key,
                         #   cookies = pure-stdlib AES-256-GCM bceConsole cookie decrypt,
                         #   checkin = daily sign-in + points quota/usage-records, provider)
```

`core.state` imports `CodeBuddyClient` / `BaseProvider` only under
`TYPE_CHECKING` — importing them at runtime would recreate a
`codebuddy_provider` package → `observability` → `core.state` import cycle.

## Request path

```text
/v1/chat/completions ─┐
/v1/responses         ├─ web/routes.py: protocol normalization
/v1/messages          ┘
                         │
                         ▼
             codebuddy_provider.forward.forward_chat()
             - defaults, explicit provider prefix, model lookup
             - disabled/scheduled model policy
             - unified metrics instrumentation
                         │
             ┌───────────┼────────────┐
             ▼           ▼            ▼
        CodeBuddy      Trae/PAT      ZCode / Doubao
```

- **`web/routes.py`** owns public protocol endpoints and request parsing.
- **`codebuddy_provider/forward.py`** owns cross-provider routing and model
  availability policy. New protocol endpoints must use this path rather than
  call a provider directly.
- **`providers/base.py`** defines the deliberately small `BaseProvider` contract:
  model catalog, authentication, forwarding, health, and optional check-in /
  quota capabilities.
- **`codebuddy_provider/`** contains the default provider's routing,
  instrumentation, upstream pipeline, provider implementation, and the
  `client.py` CodeBuddy HTTP client. Its quota path layers two upstream
  sources: `get-user-resource-summary` for the per-pack capacity numbers and
  `get-user-resource` (resource instances) aggregated per pack for the
  expiry dates — the second source is best-effort and must never fail the
  quota card.
- **`trae/`** is split by responsibility (credentials, transport, SSE,
  native tools, text fallback, and PAT). `trae_provider.py` remains the
  published `trae-cli` entrypoint and trae façade (not a shim to remove).
- **`web/ui/`** provides localhost-only administration endpoints (split by
  responsibility: channels, models, settings, queries, page) and the bundled
  UI under `web/static/` (an `index.html` skeleton plus `style.css` and six
  JS files, served from `/ui/{name}`). `benefits.js` (calendar + quota fold +
  channel-specific panels) was split by responsibility once it passed 700
  lines: `benefits_accounts.js` holds the shared multi-account card helpers
  (delete-confirm modal / move / delete / snapshot backfill) that new
  multi-account channels reuse, `benefits_checkin.js` the checkin rows + Kimi
  panel, `benefits_panels.js` the Trae PAT / Antigravity / Qoder panels.
  The panel mount order in `index.html` is also the visible quota-card order:
  regional twins stay adjacent (CN then Global), Kimi sits between Trae and
  Qoder, and the operational Trae PAT card is intentionally last. Dedicated
  panels put one provider-wide quota refresh in the title rather than repeating
  it on every account card; Trae Global has a separate quota panel and an
  explicit unsupported-check-in row. The UI may update runtime settings but
  must use `core/settings.py` for persistence.

## Runtime composition

`__main__.py` parses CLI/environment configuration, instantiates enabled
providers, loads persisted settings, and creates `ProxyState`. The application
currently uses a module-level FastAPI app and `ProxyState`; this is intentional
for the single-process local deployment model. Do not introduce an app factory
as a drive-by refactor: it affects lifecycle code, compatibility shims, and
existing isolated tests. Revisit it only when supporting multiple app instances,
workers, or explicit provider shutdown lifecycles.

## Multi-account state

Each multi-account channel (trae / kimi / qoder / antigravity) keeps its own
state dir `~/.buddy-proxy/<ch>/` with `index.json` (the account list) plus one
0600 credential file per account. `AccountRef` in each `credentials.py` is the
source of truth for index fields; **every** construction site must pass fields
as keywords and carry `alias` (the display name edited via the admin ✎ button,
default `""` = fall back to the default name). Two invariants protect it:

- The index self-heal rewrite inside `list_accounts()` (which rebuilds
  `AccountRef` from raw entries and rewrites the file) is the **only** place
  that re-creates fields from an entry dict. Dropping a field there silently
  erases it on the next enumeration — when adding an index field, that
  `kept.append(AccountRef(...))` site is the one that gets missed.
- `save_account_cred` upsert (same credential → existing account) only rewrites
  the named credential keys (`nickname`/`name`/`email`); anything outside that
  list (like `alias`) survives re-login by design. Do not "clean up" the dict
  wholesale there.

`rename_account(account_id, alias)` (also keyword-safe, file-locked like every
other index write) is the sole writer of `alias`; the HTTP surface is
`POST /ui/api/{ch}/accounts/rename` (`{id, alias}`, empty string clears it,
`ValueError` → 404). Display names everywhere run one chain
(`alias > name/email/nickname > id`) in `failover.accounts_status()`, trae
check-in detail rows and the frontend panels, so the quota card and the
check-in card never disagree about who is who. The request log follows the
same rule via **store-id / resolve-at-display**: forward paths write the
stable account id into `metrics` `account` (trae used to write the alias at
request time — that froze old rows on a stale name), and `/ui/api/logs`
attaches `account_name` resolved through the per-channel `accounts_status`
alias chain (`web/ui/queries._account_name_map`, 60 s cache, local reads
only), so a rename retroactively relabels every historical row. Trae PAT has
no aliases; its local profile list resolves an id to `PAT #N（id）`, matching
the quota-card title. Rows that cannot be resolved (pre-change trae rows,
deleted accounts) pass through untouched.

**qoder's model catalog is per-account.** The upstream model list is bucketed
by account (a risk-controlled account may only see the Qwen3.8 pair while a
full account sees all 14, and the state drifts), so `qoder.refresh_models`
walks **every** account and stores the union — publishing one account's view
used to erase the others' models from `/v1/models`. The catalog is memory-only
per region; `__main__` installs a background warmup loop (fetch on startup,
re-fetch every `CACHE_TTL_S`) so a restart no longer strands the list on the
bundled static fallback. Per-model account restrictions are an explicit
allowlist in `qoder/models.json` (`{"id": "<public id>", "accounts":
[<uuid>, ...]}` — listed ids **support** the model; unlisted models and
`"all"` mean every account). `forward` filters candidate accounts through it
**before** the failover loop: unsupported accounts are skipped silently (no
cooldown — the account is healthy, it just lacks the entitlement) and an
empty candidate list fails fast with 404 instead of paying a slow read
timeout per restricted account. The JSON loads once at import; editing it
requires a restart.

`qoderintl` is a separate Global-region subclass of `QoderProvider`, registered
at startup only when `--qoder` is active, the primary Qoder provider is not
already Global, and a Global account exists. Its quota panel is deliberately
read-only: account management routes such as `/ui/api/qoder/accounts/order`
operate on CN accounts and must not be reused. Check-in uses the inherited
campaign provider capability and the generic `/ui/api/checkin` route; the UI
renders a separate provider row because check-in rows are provider-driven.
Adding a Global login while the server is already running still requires a
restart for registration.

**Qoder account identity comes from `/api/v1/userinfo`, never from the
token exchange.** None of the deviceToken endpoints (poll / refresh / PAT
exchange) return `name`/`email` — verified live 2026-10-08 — so accounts
logged in before identity backfill existed show up as bare UUIDs in
check-in detail rows and quota panel titles. `credentials.fetch_userinfo`
queries the (previously dead) `userinfo_url` config entry;
`backfill_identity` fills `name`/`email` into index + cred file (alias
untouched), runs once at login (`only_if_missing=False`) and lazily from
`checkin_status`/`_quota_one` when a nameless account appears (per-process
once per account; failures are swallowed — identity is cosmetic). Other
channels were swept for the same disease and are clean: trae/codebuddy/kimi
persist upstream nicknames at login, antigravity uses email as the account
id, and the single-account channels have no per-account rows at all.

Campaign check-in (`/sash/**`) is **fingerprint-targeted** (CN and Global
alike): the upstream only returns the daily `CLAIM_BENEFIT` entry to device
identities it recognizes. Requests therefore carry the desktop app's
`runtime-info`-generated `Cosy-MachineToken/Code/Type` per account
(`qoder/umid.py`, cached 1h, subprocess-based with a static-header fallback
when the binary is absent); without a recognized fingerprint the campaign list
silently omits the check-in entry and the UI misreports "no claimable reward".
For the remaining process-session targeting case, `qoder/campaigns.py` has a
strict read-only desktop-log fallback: it scans only the newest log tail, matches
payload uid exactly, rejects stale/future/expired entries, and is consulted only
when direct results contain no claim action. Direct state always wins; a desktop
claim may replace the same-id `VIEW_DETAILS` placeholder but cannot overwrite an
actual direct claim result.

The ASGI event loop is shared by model streaming and management routes, so
blocking operations must stay off-loop even when wrapped by an `async` method.
Trae's first-event gate and sync iterator both call `next()` on a blocking
upstream generator; each is driven with `asyncio.to_thread`. Qoder quota HTTP
calls run in the quota executor, but waiting for those futures must also be
asynchronous (`asyncio.wrap_future` / `wait_for`); the single-account call uses
`to_thread` as well. Otherwise a slow quota/stream request stalls health,
benefits, and unrelated model routes in the same process.

## Models and settings

- `web/models_config.json` is the source for static CodeBuddy/PAT catalog
  metadata. New PAT entries use lowercase public model IDs even when the
  upstream model or `config_name` is case-sensitive; `upstream_model` /
  `config_name` are the explicit boundary mapping and must preserve the exact
  upstream spelling. Dynamic providers expose their catalog through
  `BaseProvider.models()`.
- A provider that implements optional async `refresh_models(force=True)` is
  capability-discovered by `web/ui/models_api.py`; only then does its group show
  “↻ 重新拉取”. Every refreshable provider walks every account in its region and
  publishes the successful union (Trae CN additionally copies the upstream
  `consumption_rate` back as the credit multiplier; it only probe-verifies
  previously unknown names, never the whole table). Trae CN also filters
  client-private models at fetch time (`display_config.is_custom_model` /
  `builder` — other accounts' hand-added entries must never be republished),
  and both Trae regions run the same **generation convergence** in `models()`:
  when a family has a higher parseable generation in the pool, the lower one is
  omitted from the listing (still callable by name, multiplier untouched).
  Names without a parseable generation never converge. Catalog discovery must not
  reuse the cooldown-filtered chat
  failover list: cooldown is transient and cannot prove an account-only model was
  removed. For Kimi/Antigravity/Trae a partially successful round is therefore
  monotonic (new ids may be added, previous ids stay); only an all-account success
  may confirm removals. The replacement is provider-instance local and atomic;
  an all-failed or valid-but-empty round raises and leaves the previous catalog
  untouched. Newly discovered entries must feed the same runtime lookup
  used by forwarding, otherwise the UI would advertise a model that routing
  silently rewrites or rejects.
- `hidden_models` and `disabled_models` are intentionally different policy
  layers. A hidden `provider/model` is filtered from the models UI, selectors
  and both `/v1/models` arrays, but direct addressing still routes it; a disabled
  pair fails fast. The UI builds the hidden-recovery list from an unfiltered
  catalog and retains missing keys so stale settings remain removable.
- Any persisted model identity must be built with `core.settings.model_key(
  provider, model)`. It normalizes the legacy `workbuddy` name to `codebuddy`;
  bypassing it can make UI policy and runtime routing disagree.
- `core/settings.py` owns local settings path, normalization, and atomic
  persistence. Callers must not open or write the settings JSON directly.
  Every overwrite keeps the previous version as `settings.json.bak` first, so a
  UI write is always reversible. Backing up is best-effort and must never make
  a save fail.
- `model_order` (settings key) maps a **bare model name** to an ordered list of
  `provider/model` candidates, tried top-down by `forward._forward_with_order`.
  The key is the name the client asks for, not a channel-scoped pair: one entry
  then covers every channel that publishes it (a `glm-5.3` on four channels is
  one config, not four), and the runtime picks whichever channel claims the name
  first. `forward` also accepts the historical `<provider>/<model>` key form so
  configs written by older versions keep firing.
  **Failover is only legal when an attempt failed before any byte was committed
  to the client** — a retryable `HTTPException`, a transport-level exception
  (`_RETRYABLE_EXC`: `httpx.HTTPError` / `OSError`), or a non-2xx
  `JSONResponse`. A `StreamingResponse` is never replayed (double-billing risk;
  same invariant as `trae/pat/chat.py`). Known blind spot: codebuddy reports
  upstream stream errors as an *in-band* error chunk, so its stream failures
  never fail over. `metrics.SSEErrorExtractor` now spots that chunk in
  `observability._metrics_stream` and records the request as failed, so the
  metrics no longer show a false green — but the *routing* behaviour is
  unchanged (a stream that already started is still never replayed).
  Conversely, **programming errors are not failover material**: a `TypeError`
  escaping a provider is re-raised, not treated as an upstream outage, so a real
  bug surfaces as a 500 instead of being masked by a working fallback channel.
- Routing precedence: an explicit `provider/model` prefix in the request is a
  deliberate instruction and returns before `model_order` is consulted, so a
  prefixed request never fails over. Failover applies to bare model names,
  where the router resolves the owning provider itself.
- A prefix naming a channel in `settings.KNOWN_PROVIDER_IDS` that is *not*
  registered this run is a **400 `provider_disabled`**, not a fallthrough.
  Letting it fall through sent the whole `qoder/...` string to the fallback
  channel, which the upstream rejected as `model [...] service info not found`
  — the client then reported "model not found", hiding the real cause (the
  channel was never enabled). Prefixes that are *not* channel names
  (`openrouter/...`) still fall through: `provider/model` is a legal plain
  model id, and `trae`/`traepat` exceptions aside, the router must not claim it.
  The error text comes from `settings.PROVIDER_ENABLE_HINTS` rather than being
  assembled from the prefix — `traepat` has no `--traepat` flag (it rides the
  `--trae` branch, gated by `TRAE_PAT_BEARER`), so a generated hint would name
  a flag that does not exist. The dual-region ids follow the same shape:
  `traeintl` / `qoderintl` have no flags of their own either — they ride the
  `--trae` / `--qoder` branches and register only when at least one
  `region=global` account is logged in, so their hints point at
  `buddy login <provider> --region global` first.
- `core/cooldown.py` owns transient target health: a failed target is skipped
  for a short TTL (escalating on repeat failure). It is deliberately **in-memory
  and not persisted** — a restart is a legitimate reason to re-probe, and user
  intent lives in `settings.json`. Not every failure is cooldown material: a
  **local DNS resolution failure is machine-level**, not this upstream's fault.
  Marking it cools *every* candidate at once and amplifies one blip into a
  whole-model outage with no re-probe for five minutes (measured 2026-10-02:
  all three candidates marked, then `model_order_skip reason=cooldown` for
  5 minutes straight, zero upstream requests dispatched). `core/errors.py`
  (`is_local_dns_failure`) classifies it and `forward` skips the mark — the
  failover still moves to the next candidate, it just leaves no trace behind.
  Classification **must walk the `__cause__` chain**: every provider converts
  the `httpx`/`httpcore` error into `HTTPException(502)` at its boundary, so
  checking the top-level type alone misses all of them. `core/errors.py` is a
  leaf module (stdlib only), same convention as `core/cooldown.py`.
  `describe_exception` lives there too: `httpcore` maps some failures onto
  exceptions whose own `str()` is empty (`httpx.ReadError()`), and a bare `%s`
  on those logs a blank line — it falls back to the type name and the first
  non-empty cause.
  DNS is not the only machine-level failure mode. Same evening, ~1 hour later:
  the local Clash tunnel died and qoder/codebuddy/trae all failed within 50
  seconds — but with genuine `502 Bad Gateway` responses (the tunnel's own
  error page), which is *not* classifiable per-request: the tunnel error body
  and a real upstream 502 look identical once wrapped. So `core/cooldown.py`
  uses a statistical signal instead: **≥2 distinct channels failing for the
  same model within a 60s window ⇒ machine-level burst** (network-class
  failures only — transport errors and 5xx; 429 quota rejections are the
  upstream's own state and never enter the window, so simultaneous rate
  limiting keeps its full 5-minute cooldown and escalation) — the current target
  and the already-marked ones in the window are (re)marked for only 30s and
  never count toward escalation. The first-failing target gets shortened
  retroactively (it was marked 5 min before the second channel's failure
  proved the burst); burst evidence is wiped by `clear()` so a manual
  clear-cooldown can't be overridden by stale window entries. Accepted
  trade-off: two genuinely-broken channels failing within 60s also get 30s
  marks — failover still works, the marks just rotate faster.
- The `model_order` editor lives in its own **Model order** tab of the bundled
  `/ui` page: one card per configured model, expanding to the reorderable target
  list. **Its render source is `GET /ui/api/model-order`, which returns the
  settings field verbatim** — one item per key, so the page shows exactly what
  the config holds. It deliberately does *not* drive cards off the channel
  catalog: `GET /ui/api/models` annotates each (channel, model) pair, so a name
  published by four channels came back four times and the page drew four
  identical cards. Its channel list and model choices (for the "add model"
  picker) come from `GET /ui/api/model-order/options` (the page's global
  `MODELS` is only populated once the models tab has loaded, so the editor must
  not depend on it). Targets are picked from that list but remain free-text,
  because an upstream may accept an id its catalog does not advertise.
- A model card's key is the **bare model name** — no owning channel. The card is
  the config key, and the runtime bare key fires on every channel that publishes
  it, so a channel-scoped key would silently narrow the rule to one channel.
  Related mismatch worth remembering: the catalog's `m["id"]` carries a channel
  prefix (`qoder/deepseek-v4.1-flash`) while `model_order` targets and metrics
  are recorded against the bare name. Looking up the per-model request counts on
  the models tab with the prefixed id made that column read ~0 for every channel
  that prefixes its ids.
- Editing is **page-only**. There is deliberately no row-level order button on
  the models tab: that table's action column grew crowded and the entry point
  became unfindable. The row keeps a `⇄ n` badge (targets, plus `⏸ n` cooled) and
  a clear-cooldown button, nothing more.

## Check-in rotation

Check-in ("打卡") is per-provider: a provider opts in with
`supports_checkin = True` and implements `checkin_status` / `checkin_claim` /
`quota`. `benefits.BenefitsManager` aggregates them for the admin UI and runs
the auto-claim loop. Doubao is one of them: its check-in runs in the app's
embedded Chromium via the same CDP session as chat (`doubao/checkin.py`), tries
a short ordered list of activity endpoints and only accepts HTTP 200 with
`{"code": 0}` plus a recognisable field; anything else is returned as a
`query_failed` status so the card shows as unknown instead of "not signed in". The
claim POST is only sent against the endpoint prefix that just produced a valid
status — never blindly. Three invariants matter because getting them wrong is
silent:

- **Check-in enumeration ignores quota cooldown.** `checkin_status` /
  `checkin_claim` walk the full (region-filtered) account list, *not*
  `available_accounts()`: cooldown guards model forwarding, and filtering by
  it silently hid a cooling account from the check-in card — with two
  CodeBuddy accounts and one cooling, the card showed a single-account view
  and summed half the daily credit (user report 2026-10-07, fixed on
  CodeBuddy/Trae/Qoder alike; the quota/forward paths keep their cooldown
  filter).

- **A provider's rotation period is not necessarily a calendar day.** Qoder's
  campaign window is `10:00 → 09:59` next day (verified 2026-09), while
  CodeBuddy / Trae rotate at local midnight. Local history
  (`logs/checkin.jsonl`) dedupes by *calendar day*, so an early-morning poll
  can record the previous Qoder round as "today's claim" and make the rest of
  the day look done. **Anything the upstream reports as claimable therefore
  wins over the local record** (`benefits.claimable_now`) — both for the UI's
  `done_today` and for the auto-claim loop. A provider that contradicts itself
  (`checked_in` *and* `claimable`) is treated as already claimed, so a
  malformed reply costs at most a skipped round rather than a claim request
  per poll.
- **The timestamp source must be reported, not assumed.** `core/checkin.py`
  computes `next_ts` (the moment the current state flips) plus
  `next_ts_source`. Only Qoder's upstream actually returns a window
  (`startAt`/`endAt`, so `upstream`); CodeBuddy returns only the whole campaign
  season and Trae no time fields at all, so midnight is *inferred* from the
  real claim timestamps and must be labelled `inferred` — the UI shows "≈".
  When `next_ts` cannot be computed (season over, campaign inactive), the field
  is omitted rather than filled with a stale timestamp.
- **Multi-account channels report per-account check-in detail.** Trae's
  aggregated badge (`checked_in`/`claimable`) cannot say *which* account
  failed, so `checkin_status` (multi-account) and `checkin_claim` attach an
  `accounts` list (`index`/`name`/state, or `error: 查询失败` for a failed
  probe) and the check-in card renders one line per account; the claim toast
  reports each account's outcome too. Qoder also queries each same-region
  account because their campaign lists are independent; it aggregates whether
  any account can claim and exposes the per-account state. Manual Qoder claims
  still claim only the first claimable account in priority order. Single-account
  responses carry a one-row `accounts` list too (2026-10-07): the card layout
  keeps the badge row and the account row uniform across channels, instead of
  a single-account channel like qoderintl rendering a differently-shaped card.
  The aggregation also forwards `daily_credit`/`benefit_kind`/`activity_name`
  (top level and per row) — dropping them made the “每日 +100.00” chip appear
  on single-account channels only. Codebuddy and Trae had the same disease in
  their own aggregations (2026-10-07): both now forward `daily_credit` from
  their per-account payloads (`today_credit` / `credits`) through the
  aggregated rows and top level too.
- **The check-in card declutters (2026-10-07).** When per-account rows are
  present, the top-level state badge is dropped — one badge per account row
  already tells the story, and the aggregate badge next to the channel name
  repeated it (kept when the aggregate query itself failed: there the badge's
  error tooltip is the only lead). The upstream campaign key
  (`activity_name`, e.g. `act-20260930-551`) is no longer rendered as a chip:
  opaque to users, who found it confusing; the field is still delivered for
  log digging.
- **Activity presence is not the same as a claimable check-in.** Qoder's
  campaign list may contain `VIEW_DETAILS` entries alongside, or instead of,
  `CLAIM_BENEFIT`. Only the latter with `CLAIMABLE` is sent to the claim API.
  A non-claimable campaign is reported as “有活动，暂无可领签到奖励” rather than
  “今日无签到活动”, and its claim button stays disabled; this preserves the
  action-type safety check without claiming that the whole activity list is empty.
- **A cached status snapshot expires at its own rotation, not just its TTL.**
  The UI counts down to `next_ts`, so the refresh right after that moment must
  actually show the new state — but the 300 s snapshot cache would otherwise
  serve the pre-rotation state, leaving "已签到" and a disabled button for up to
  five more minutes (long enough for Qoder to lose a whole round, since its
  window expires on miss). `benefits._state_flipped` therefore treats a *past*
  `next_ts` as an earlier expiry, which is free: the cached entry already
  carries the moment it stops being true. Bounded by `FLIP_GRACE_S` (1 h) so an
  upstream that keeps returning a long-past timestamp cannot render the cache
  permanently useless and hammer the upstream on every poll.
- **Force refresh is per-scope, not a blanket invalidation.** The check-in
  card's 强刷 button (`benefits_checkin.js refreshCheckin`) posts
  `/ui/api/benefits/refresh` with `checkin: true`, which drops the provider's
  `checkin:{pid}` snapshot (`BenefitsManager.invalidate_checkin`) before
  re-querying — for multi-account channels that means re-hitting the upstream
  once per account. The quota card's ↻ button posts the same endpoint *without*
  the flag and therefore must not drop the check-in cache; the two scopes are
  separate methods so a quota refresh can never carry that per-account cost.
- **Expiry and reset are different things and live in different fields.** A
  quota item carries `expire_ts` (the allowance is voided — plans, packs,
  check-in credits) and `reset_ts` (it refills on a cycle — ZCode's 5-hour
  window, antigravity's weekly pool). Upstreams rarely label which one they
  mean, and the *same field name* means different things in different
  channels: Qoder's `expiresAt` is an expiry, MiMo's `nextResetTime` a reset,
  Trae's `entitlement_base_info.end_time` an expiry — but Trae **PAT's**
  `end_time` is a reset (weekly/daily pools refill at midnight). All of them
  were once fed into `reset_ts` and rendered as "· 重置", which is how Qoder's
  "expires 10-30" displayed as "10-30 resets". Guessing wrong is not
  cosmetic: `benefits._expiring` only
  reads `expire_ts`, so a reset misfiled as an expiry would put "your 5-hour
  window expires in 2 hours" on the warning banner every single round. Items
  also carry `unit` (`credit` / `day` / `count` / `permille`), which gates the
  banner's amount filter — a 200-credit check-in pack expiring in 7 days is
  noise, a 4000-credit plan is not; days and counts are not comparable to a
  credit threshold and are filtered by time alone.
- **A quota reading's sign convention is part of its meaning.** Antigravity
  reports *remaining* (`remainingFraction=1` means full) and has no interface
  that exposes the weekly pool at all; CLIProxyAPI shows `100%` for the same
  accounts, which is also *remaining*. buddy renders both `remaining`/`total`
  and `percent`/`used` (progress-bar semantics), so a full allowance shows as
  "剩 1000 / 1000 · 已用 0%" — correct, but it read as "quota is 0" until the
  item also spelled out "满额" and listed `models_in_group`. When one upstream
  hands the same single value to two UIs, a discrepancy is a sign convention,
  not a data difference; say so in the item's `note` rather than guessing.
- **Providers report every quota item; the frontend decides what to show.**
  `quota()` must not truncate `items` for display. CodeBuddy used to return
  only the first 4 packs (`if len(packs) < 4`) and the cut was invisible in a
  specific way: the dropped rows are never sent, so nothing downstream can
  know they exist or say so. Trae has the same shape of bug historically
  (deduped by name, capped at 3, hiding 12 unspent allowances). Row count is a
  *view* concern, so it lives in the view: `benefits.js` renders items through
  `quotaItemsHtml`, which shows only items with remaining allowance, folds the
  used-up ones (remaining ≤ 0) into a collapsed block **after** the live ones,
  and clips the whole region past `--qfold-max` with an expand button. Three
  rules are load-bearing. (1) *Items with unknown remaining are never folded* —
  a PAT failure notice or `reset_pending` carries text, not a number, and
  reading that as zero would hide the one row that matters. (2) *The fold
  decision uses measured height, not an item count* — in the two-column layout
  a narrow card's usable height differs from a full-width card's. (3) *Expand
  state lives in `QUOTA_FOLD`, not the DOM* — the panel is rebuilt from
  `innerHTML` every 30 s (same reason as `_state_flipped` above: the page's own
  refresh cycle is the thing that would otherwise undo the user's action).
  Anything the backend withholds for display reasons is unreachable by
  construction, so "keep the payload small" is not a valid reason to trim here.

## Logging and privacy

**Never log token, UID, request body, response body, tool arguments, or bearer
credentials.** Diagnostic code records only safe metadata such as provider,
model, counts, byte length, status, and short one-way hashes. `--verbose-llm`
and `WB_DEBUG_DUMP` retain compatibility but may add only safe summaries.

## Validation before changing behavior

```bash
uv run ruff check src tests
uv run pytest -q
```

Keep provider changes small and add an offline regression test. Live upstream
probes are billable and must never print credentials or request bodies.
