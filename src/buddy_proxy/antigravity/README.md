# Antigravity 通道（Google Antigravity 免费额度）

把 Antigravity 的免费额度代理成 OpenAI / Anthropic 兼容接口。Antigravity 是
Gemini CLI 免费个人层的官方继任者（2026-06-18 起 Gemini CLI 免费层
onboarding 直接 `UNSUPPORTED_CLIENT`），一次 OAuth 登录同时解锁 Gemini 3.x、
Claude Sonnet/Opus 和 GPT-OSS。

协议与指纹**以社区逆向的 Antigravity 客户端形态为准**（antigravity-claude-proxy，
大量用户验证过的 JSON REST 形态），并用本机官方 `agy` CLI 二进制的 strings
交叉佐证（OAuth client/secret、双端点、模型名、keyring 机制均能对上）。
注意 agy 本体是 Go gRPC 黑盒，没有可读源码——如果哪天官方放出可对照的
客户端实现，以它为准重新对齐。

## 文件结构

| 文件 | 职责 |
|---|---|
| `models.json` | 模型表（id/描述/配额组）。**加删模型改这里，不改代码** |
| `fingerprint.py` | UA / X-Client-* / x-goog-api-client 请求头指纹（版本自动探测） |
| `credentials.py` | OAuth 凭证存取与刷新（多账号：`~/.buddy-proxy/antigravity/` 目录） |
| `setup.py` | loadCodeAssist / onboardUser onboarding（项目 ID 获取；daily→prod 端点 fallback） |
| `login.py` | `buddy login antigravity`（PKCE + 本地回调；含 agy 登录态采用） |
| `cli_bridge.py` | 与本机 agy CLI 凭证互通（**只读** keyring 导入） |
| `convert.py` | OpenAI chat ↔ Antigravity envelope 双向转换（内层复用 gemini convert，含 thoughtSignature/id 回环） |
| `failover.py` | 多账号 failover：可用账号枚举 + 内存冷却 + UI 账号状态数据 |
| `provider.py` | BaseProvider 实现（转发、SSE、协议转换、配额展示） |

## 使用

```bash
buddy login antigravity     # Google OAuth 登录（检测到本机 agy 登录态可直接导入）
# 启动代理加 --antigravity（或 ANTIGRAVITY_ENABLED=1）
curl http://127.0.0.1:8787/v1/chat/completions -d '{"model":"antigravity/claude-sonnet-4-6","messages":[...]}'
```

凭证存放 `~/.buddy-proxy/antigravity/`：`index.json`（账号清单，priority 即
failover 顺位——登录顺序初始化，管理页可调）+ 每账号一份 `<account_id>.json`（0600 原子写，account_id 由
email 规范化，email 缺失时退 `acct-<token hash 前 12 位>`）。access_token
过期自动刷新；refresh_token 长期有效（Google 安装型应用不轮换）。删掉某账号
的 JSON 文件 = 退出该账号（索引自愈）；删除 `index.json` 全部账号即全退出。

历史单账号文件 `~/.buddy-proxy/antigravity_oauth.json` 在首次访问时自动
copy 迁移为账号 #1（旧文件保留作备份，迁移失败只告警不影响其它账号）。

## 多账号与自动 failover

- **登录即追加**：换 Google 账号再跑一次 `buddy login antigravity` 就是追加
  备用账号；相同邮箱 = upsert 该账号凭据且顺位不变。上限 8 个。
- **顺位可调**：管理页面板每个账号卡片悬停出 ▲▼（多账号时），点一下即改
  failover 顺位——`POST /ui/api/antigravity/accounts/order` 提交**完整**的
  id 顺序列表（少/多/重复一律 400），后端重写 index.json 的 priority 为
  0..n-1，added_at 保留（登录事实不动）。quota 缓存键带 priority
  （`quota_epoch`），重排后旧额度快照自动失效、下一轮即换新顺位。
- **换号条件**：HTTP 429（额度）/ 403 / 401 强刷后仍拒 / 凭据层 AuthError /
  缺 project_id / 上游超时或网络错（`_UpstreamUnavailable`，两 endpoint 都
  拿不到 HTTP 响应）→ 冷却当前账号换下一个；业务 4xx（模型名等）原样透传
  不换号。
- **冷却时长**：429 尊重 `Retry-After`（钳 1s~7d），默认 5 分钟；403/凭据/
  超时 60 秒；403 文案是「Verify your account to continue.」= Google 风控
  拉黑（能登录但上游一律拒），6 小时档 + `blacklist` 类别（面板标「疑似
  拉黑」，管理页 ✕ 删除）。只放内存不落盘——重启清零，代价只是每账号重探
  一次。全账号冷却时转发直接 429（通道级快速失败），全部试败时报错带
  `cooldown_report()` 逐账号画像（谁在冷却剩多久/疑似拉黑）。
- **超时分级**（2026-10-03 实测驱动）：流式 read 90s（相邻两次读上限——
  首事件前卡死在闸门抛 TimeoutException、`_Gate.timed_out` 短冷却换号；流中
  卡死在转换器断）；非流式 read 120s（一次读拿全响应=总上限，实测成功最长
  16s）；connect 15s。旧值 600s 曾让挂死调用白等 10 分钟（5 单 504 实录）。
  failover 循环带 180s 尝试期预算（`_ATTEMPT_DEADLINE_S`，首个账号不受挡）：
  防超时换号把 N 个账号串成分钟级等待。
- **删除账号**：`POST /ui/api/antigravity/accounts/delete`（管理页 ✕，
  弹窗确认）→ `delete_account`（索引 + cred 文件）+ `clear_cooldown`
  （防内存残留）。
- **防串号**：`ensure_account_token(account_id)` 把 token 与 cred 快照同源
  返回，project_id 从同一份快照取；每账号独立刷新锁 + 锁内重读双检。
- **流式防重复计费**：首事件闸门（`_gate_first_event`）压住第一个上游事件
  再定性——带内 429/403 error（一个字节没出网）冷却换号；语义事件
  （candidates）出现即 committed，缓冲行经 `_ReplayStream` 补放、绝不重放；
  语义前 EOF 视为假成功换号（不冷却）。闸门只创建一次 `resp.aiter_lines()`
  并把它（`_Gate.lines`）传下去：httpx 响应流一次性消费，重开必抛
  StreamConsumed 静默丢光剩余事件（claude 流式曾因此完全空流）。
- **可观测**：每次转发把实际服务的账号写入 `ACCOUNT_META`（metrics 落库可
  归属）；管理页有独立 Antigravity 面板（各账号额度左右分栏 + 账号状态行，
  数据来自 `failover.accounts_status()`，纯本地不触网）。

## 与本机 agy CLI 互通（只读导入）

agy（官方 Antigravity CLI，Go）把 OAuth token 存系统 keyring，**没有明文
配置文件可回写**——所以互通方向与 gemini 通道相反：只读导入，不回写。

- **macOS**：login keychain 的 generic password，
  `security find-generic-password -s gemini -a antigravity -w`（service 归在
  gemini 产品线下，就叫 `gemini`；二进制 strings + 本机 keychain 实测确认）。
  读取不需要授权弹窗（login keychain 自己的条目）。
- **Linux**：go-keyring 走 Secret Service，`secret-tool lookup service gemini
  username antigravity`（实验性支持）。Windows 无对应 CLI 工具，提示手动。

keyring 值格式（`go-keyring-base64:` 前缀 + base64 JSON）：

```json
{
  "token": {
    "access_token": "...", "token_type": "Bearer",
    "refresh_token": "...",
    "expiry": "2026-10-02T23:55:02.382106+08:00"
  },
  "auth_method": "consumer",
  "id_token": "<JWT，payload 带 email claim>"
}
```

`expiry` 是 ISO 带时区，与 buddy cred 的 `expiry` 同格式，原样透传。
`buddy login antigravity` 检测到可用登录态会先问「直接使用它吗？」（默认
yes）：access token 过期自动用同一 OAuth client 刷新、onboarding 自动补跑、
落盘我们自己的凭据文件。两边共享同一个 refresh_token，各自刷新互不冲突
（Google 安装型 client 不轮换 refresh_token）。

## 请求形态（envelope）

`POST {base}/v1internal:streamGenerateContent?alt=sse`（流式）/
`v1internal:generateContent`（非流式），body 是 Antigravity 包装：

```json
{
  "project": "<cloudaicompanionProject>",
  "model": "gemini-3.8-flash",
  "request": { "contents": [...], "generationConfig": {...} },
  "userAgent": "antigravity",
  "requestType": "agent",
  "requestId": "agent-<uuid>"
}
```

与 gemini 通道的差异：`model` 是裸名（不带 `models/` 前缀）、内层 request
不带 `session_id`、systemInstruction 走身份断言注入（见下）。

## 身份断言与 scrub（已知风控点）

免费层上游会检查 systemInstruction 里的身份自认。社区共识（issue #76）：
不带 Antigravity 身份断言、或带着「You are Claude/Codex/ChatGPT」等竞品
断言硬调，会触发 429 `RESOURCE_EXHAUSTED`。处理：

1. 注入两个 user-role part 的身份断言（正文 + `[ignore]` 包裹版各一）；
2. 用户自己的 system prompt 先 scrub 竞品身份断言
   （"You are Claude Code" → "You are the assistant" 这类，规则见
   `convert.py` 的 `_DEFAULT_SCRUB_RULES`，可用 `ANTIGRAVITY_SCRUB_IDENTITY`
   环境变量追加 `term=>replacement` 规则）。

## 指纹对齐表

版本探测顺序：env（`ANTIGRAVITY_UA_VERSION` / `ANTIGRAVITY_CLIENT_VERSION`）
> 本机 `/Applications/Antigravity.app/.../product.json` > 内置 fallback。
跟随客户端升级时优先确认这几个值。

| 项 | 值（fallback） | 来源 |
|---|---|---|
| client_id | `1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com` | agy strings + 参考实现一致 |
| scopes | cloud-platform, userinfo.email, userinfo.profile, cclog, experimentsandconfigs | 同上 |
| 端点 | `daily-cloudcode-pa.googleapis.com` → `cloudcode-pa.googleapis.com` | 参考实现同序；`CLOUD_CODE_URL` env 可覆盖（agy 内置） |
| `X-Client-Name` | `antigravity` | 参考实现 |
| `X-Client-Version` | 本机 product.json `version`（fallback `1.110.0`） | version-detector 同策略 |
| UA | `antigravity/<ideVersion> <os>/<arch>[ <model>]`（ideVersion fallback `2.0.3`） | 参考实现 |
| `x-goog-api-client` | `gl-node/18.18.2 fire/0.8.6 grpc/1.10.x` | 参考实现（Electron 内嵌 Node） |
| CLIENT_METADATA | 数字枚举 `{ideType: 9, platform: 1-5, pluginType: 2}` | agy 是数字枚举（与 gemini 通道的字符串枚举不同） |
| redirect path | `/oauth-callback`（PKCE S256 + state） | agy strings |

## 配额结构（2026-10-04 实测，两个 g1-pro-tier 账号）

agy `/usage` 显示两组**独立**限额，组内各模型共享 weekly + 5h 滚动双池，
按 token 成本比例消耗：

- **GEMINI MODELS**：gemini-3.x 系（`group: "gemini"`）
- **CLAUDE AND GPT MODELS**：claude-sonnet/opus、gpt-oss（`group: "claude-gpt"`）

**上游只给单值，且只在逼近池上限时才下调**：`fetchAvailableModels` 每个模型变体带
`quotaInfo.remainingFraction`（0~1）+ `resetTime`（下一个 5 小时窗口的滚动刷新点），
**没有 weekly/5h 分池字段**。实测两个账号：

- `fetchAvailableModels` 与 `retrieveUserQuota` 返回**同一份**数据（每个模型都
  `remainingFraction: 1`，`resetTime` = 当下 +5h）；`retrieveUserQuota` 的 bucket
  字段只有 `modelId` / `tokenType` / `remainingFraction` / `resetTime`，没有分池。
- `fetchUserStatus` / `fetchCredits` / `getUserQuota` 均 **404**。
- `loadCodeAssist` 的 `paidTier.availableCredits` 只有 `creditType` /
  `minimumCreditAmountForUsage`，**没有 `creditAmount`**（拿不到余额数字）。
- 所以 **weekly 池的剩余根本没有接口可查**——CLIProxyAPI 那种「Five Hour /
  Weekly Limit」两条是它自己把两个字段本地推算出来的，不是上游真有两条。

管理面板按 upstream 名前缀归模型、按组聚合（**组内取最小剩余**代表水位；满额时
全组都是 1，退化为「满额」），展示用千分制（0.9987 → `989.9 / 1000`）。
`percent` / `used` 是**已用**（进度条约定，由 `1 - worst` 反推）。

**读数的坑（用户实际反馈过）**：满额时 `used=0`、进度条是空的，容易被读成
「额度是 0」。要清楚这是**剩余**语义：CLIProxyAPI 显示 `100%` 是剩余比例，
buddy 显示「剩 1000 / 1000 · 已用 0%」是同一份数据的另一种口径，不是数据不一致。
因此 `remainingFraction=1` 时面板文案显式写「满额（组内各模型均为 100%）」，
并列出组内包含哪些模型（`models_in_group`）、说明 `resetTime` 只是 5 小时窗口的
滚动刷新点（`reset_note`，不代表整组/每周池重置）。

## 模型名与 effort 后缀（实测坑）

上游**只认 `fetchAvailableModels` 列表里的名字**，2026-10-02 实测：

- gemini 3 系**裸名直接 429 `RESOURCE_EXHAUSTED`**（伪装成配额错误的
  「模型不存在」），必须带 `-low/-medium/-high` 后缀；各模型可用档位不同
  （3.1-pro 只有 low/high，3.6-flash 有 low/medium/high），且
  `gemini-3.1-pro-high` 实测 400 `INVALID_ARGUMENT`。
- `gemini-3.8-flash` 只有 `-tiered` 变体（自动档），`gpt-oss-120b` 只有
  `-medium`；claude 系裸名可用。
- 上游列表还有 2.5 系、3.5 系、`*-agent`、`*-image` 等变体和 `chat_*`/
  `tab_*` 内部条目，模型表只暴露了实测可用的主力模型，其余见
  `models.json` 的 `_comment`。

所以 effort 后缀由模型表逐模型声明（`efforts` / `default_effort` /
`upstream`），`convert.apply_effort_suffix` 解析成上游真名，不按名字前缀猜。

## thoughtSignature 与 functionCall id（实测，2026-10-03）

gemini-3 系在每个 `functionCall` part 上返回 `thoughtSignature`（~0.7-1.2KB
密文）；第二轮回传时**强制要求原样带回**，缺失直接 400
（`Function call is missing a thought_signature in functionCall parts`——
用户实测报错即此）。claude/gpt-oss 系不返回签名，但要求 `functionCall.id`
与 `functionResponse.id` **成对回传**（缺 → 400 `tool_use.id: Field
required` / `Expected the 'id'`）。三族都容忍哨兵值
`skip_thought_signature_validator`（官方给无状态客户端的逃生门）。

实测矩阵（直连上游逐项跑过）：

| 模型族 | fc.id/fr.id | thoughtSignature | 哨兵 |
|---|---|---|---|
| gemini-3 系 | 带上要成对 | 必带（缺 → 400） | ✓ 200 |
| claude 系 | 必须成对 | 不需要 | ✓ 200 |
| gpt-oss | 必须成对 | 不需要 | ✓ 200 |

两个实现坑：

1. **签名过不了 Anthropic 协议**——签名只在 OpenAI 的扩展字段里有容身
   之处，Claude Code 这类客户端走 `/v1/messages`，tool_use 块只有
   id/name/input，签名根本到不了回程。修法：哨兵兜底 + id 回环缓存。
2. **上游 id 是短计数器**（`call_850216`），跨响应/跨账号会撞车——不能
   拿它当回环键。实测上游**只校验 fc.id 与 fr.id 成对、不校验等于原
   值**（自造 id 配对也 200），所以响应方向一律自造 `call_<24hex>` 并
   把签名/函数名记进 `gemini/thought_signature.py` 的进程内 LRU，请求
   方向凭 id 还原；缓存未命中（如代理重启）时 gemini 系注哨兵兜底，
   非 gemini 系不注（保持请求体干净）。

`functionResponse.name` 也从 id 还原：Anthropic 的 tool_result 不带函数名，
过去拿 tool_call_id 顶替（sanitize 后是错名字），现在从缓存取回真名。

## 模型更新怎么做

同 gemini 通道：改 `models.json` → 重启代理。新模型先看
`fetchAvailableModels` 列表确认上游名（带对后缀），加条目（`upstream` +
`efforts`/`default_effort`），实测一发再置 `verified: true`。下线的模型挪进
`_comment` 记一笔。

### 运行期「重新拉取」为什么不是全信上游目录

`refresh_models` 拿 `fetchAvailableModels` 做目录发现，但那张表**既脏又缺**，
不能当权威（2026-10-14 实测两账号均如此）：

- **脏**：仍回传已淘汰的旧代/内部变体（`gemini-2.5-*`、`gemini-3-flash(-agent)`、
  `gemini-3.1-flash-lite/-image`、`gemini-3.5-flash-*` 等）。全收进来 `/v1/models`
  就被灌满旧模型。→ `models.json` 的 `blocked_prefixes` 列这些**去 effort 后缀后**
  的基名前缀，刷新时命中即丢。只拦旧代，真·新模型（如 `gemini-4-argon`，不在表里）
  照常被发现收录。
- **缺**：**不回传** `gemini-3.1-pro` / `gemini-3.6-flash` 这类 premium curated
  条目（它们在 IDE 可见、也能直接路由，但目录接口不吐）。全账号成功时若按上游
  目录下架，就把人工校验过的好模型连旧模型一起洗掉。→ 刷新时无条件把
  `verified: true` 的条目按其 upstream base 注入发现集，保证**永不下架**。

改 `blocked_prefixes` 时务必复核：任何前缀都不能命中某个 verified 条目的
upstream base（有单测 `test_refresh_blocks_stale_families_and_pins_verified_curated`
守着这条）。

## 封号风险与已知边界

- **指纹来源是社区逆向**（不是官方客户端源码——agy 是 gRPC 黑盒），形态
  被大量用户验证过，但官方改协议时我们只能跟着社区更新，滞后是常态。
- **身份断言是硬要求**：绕过 scrub 直接发竞品身份会 429；429 原样透传，
  代理不做重试（避免风暴）。
- **数据隐私**：免费层 prompt 大概率同样被 Google 用于训练（Antigravity
  条款同 Code Assist 精神），敏感内容别走这条通道。
- token 只落本机 `~/.buddy-proxy/`（0700 目录 + 0600 文件），别进备份/仓库。
- agy 升级可能换 OAuth client 或端点——`strings ~/.local/bin/agy | grep
  GOCSPX` 可快速复核 secret 是否还匹配。
