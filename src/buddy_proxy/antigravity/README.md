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

### agent 框架身份同样被拦（2026-11-15 实测，dsh）

除了「竞品模型」身份，**agent 框架的自我介绍**也被同一套硬校验拦。dsh
（DeepSeek Harness）的系统提示首行是
`You are an AI agent powered by DeepSeek Harness.`，只要这句留在
systemInstruction 里，上游就回一个**单事件 429 `RESOURCE_EXHAUSTED`**，
不带 `Retry-After`、也不报原因；代理按 (账号, 模型) 记 5 分钟冷却，表现
成「dsh 必 429、curl 必 200」。

直连对照实验（同一份 4 KB 的 dsh 模板一字不改，只换这一句）：

| systemInstruction 首行 | 上游 |
|---|---|
| `You are an AI agent powered by DeepSeek Harness.`（原文） | 429 |
| `You are the assistant.` | 200 |

零宽空格绕不过，必须整句替换。`scrub_identity` 对 dsh 走**整段替换**
（复用 `core.desensitize._DEEPSEEK_HARNESS_SUMMARY`，与 CodeBuddy 通道
同一份短摘要），因为模板其余部分（工具约定、exit code 契约）上游都放行。
匹配前会先抹掉零宽字符，客户端把这句话拆开也一样命中。副作用是：整段
替换后那些工具约定与 exit code 契约也一并变成摘要——实测这是安全的，
dsh 侧的契约由客户端自己执行（读工具结果），不依赖模型记住这段话；若要
保留，得改成只剪掉身份句、其余原样，但那样要多测一轮上游，暂不做。
注入的 `_SYSTEM_IDENTITY` 本身含 "Antigravity" 字样，不受影响。

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
| UA | `antigravity/hub/<ideVersion> (aidev_client; os_type=<os>; arch=<arch>)[ <model>]`（ideVersion fallback `2.32.1`） | 见下方「UA 形态与版本门槛」 |
| `x-goog-api-client` | `gl-node/18.18.2 fire/0.8.6 grpc/1.10.x` | 参考实现（Electron 内嵌 Node） |
| CLIENT_METADATA | 数字枚举 `{ideType: 9, platform: 1-5, pluginType: 2}` | agy 是数字枚举（与 gemini 通道的字符串枚举不同） |
| redirect path | `/oauth-callback`（PKCE S256 + state） | agy strings |

### UA 形态与版本门槛（2026-11-15 实测）

上游对 User-Agent 有两层校验，**形态**和**版本**都得对：

**1. 形态（Gemini 4 族的准入开关）**——只有带字面 `hub/` 路径段 + 圆括号元数据
的形态能路由 `gemini-4-*`：

| UA | `gemini-4-hydrogen-high` |
|---|---|
| `antigravity/3.0.0 darwin/arm64` | 404 |
| `antigravity/3.0.0 darwin/arm64 gemini-4-hydrogen-high` | 404 |
| `antigravity/3.0.0 (aidev_client; os_type=darwin; arch=arm64)` | 404 |
| `antigravity/2.32.1 (aidev_client; os_type=darwin; arch=arm64)` | 404 |
| **`antigravity/hub/2.32.1 (aidev_client; os_type=darwin; arch=arm64)`** | **200** |
| `antigravity/hub/2.32.1 (...)` + 尾部模型名 | **200** |

`hub/` 前缀与圆括号元数据**缺一不可**；尾部模型名无害（buddy 仍附带，便于上游对账）。
旧形态对其余模型全部正常，所以过去只表现为「gemini-4 就是没有」。

同一 UA 下 `fetchAvailableModels` 返回 **31** 个模型（含 gemini-4 的
argon/hydrogen 各档 + `gemini-4-carbon-disabled`）；旧形态只回 **23** 个、零个
gemini-4。所以**修 UA 后模型目录自动恢复**，无需手工 seed。

**2. 版本（流式端点）**——旧版 `streamGenerateContent` 被网关短路：

| ideVersion | 流式响应 | 耗时 |
|---|---|---|
| `2.0.3`（更早的 fallback）、`2.5.0` | 单事件固定文案「This version of Antigravity is no longer supported.」，无 `usageMetadata` / `finishReason` | ~0.7s |
| `2.9.0`、`3.0.0`、`3.5.0`、`4.0.0`、`2.32.1` | 真回复，带完整 `usageMetadata` / `finishReason: STOP` | 2.5–3.4s |

更严的下限来自 Gemini 4：`MIN_UA_VERSION = 2.23.0`（云码端低于该版本拒绝新模型，
与 CLIProxyAPI 注释一致）。所以 `_detect_versions` 在本机 product.json 版本
**低于门槛时抬到 fallback**，装老客户端也不会把新模型锁死；env 显式覆盖不抬升，
留给排障时压测旧版本。

`X-Client-Version` 不是门槛：单独把它从 `1.110.0` 提到 `2.0.0` 仍是提示文本，
所以它保持 `1.110.0` 不动。

判断某次响应是不是短路，看有没有 `usageMetadata` 即可——单事件、只有
`candidates`、无 `finishReason` 的就是被版本门槛挡了，别当成模型回答。
对 gemini-4 返回 **404**（而非 429）则先怀疑 UA 形态被改坏了。

## 配额结构（2026-10-04 实测，两个 g1-pro-tier 账号）

agy `/usage` 显示两组**独立**限额，组内各模型共享 weekly + 5h 滚动双池，
按 token 成本比例消耗：

- **GEMINI MODELS**：gemini-3.x / 4.x 系（`group: "gemini"`；其中 gemini-4-argon
  在上游是自己的周池，见下）
- **CLAUDE AND GPT MODELS**：claude-opus/sonnet-5-5、gpt-oss（`group: "claude-gpt"`）

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

管理面板按 upstream 名前缀归模型，再**按额度池**聚合（**池内取最小剩余**代表水位；
满额时全池都是 1，退化为「满额」），展示用千分制（0.9987 → `989.9 / 1000`）。
`percent` / `used` 是**已用**（进度条约定，由 `1 - worst` 反推）。

**分池判据是 `resetTime`（精确相等）**（2026-11-15 实测，两个账号一致）：同一池
内各模型的 `resetTime` **严格相等**（秒级字面值都一样），不同池差整周（argon 的
周池 resetTime 是 2026-11-22）。所以组内出现次数最多的 `resetTime` 是主池，
**其余每个不同的 `resetTime` 各成一池**，缺失 `resetTime` 的并回主池——不做
「差多少分钟才算另一池」的容差判断（早期版本有 `_QUOTA_SEPARATE_POOL_MIN_GAP_S`
阈值，会漏掉只差几分钟的新池）。更早的实现按 `group` 合并、组内取 min，argon
的独立水位被 hydrogen 掩盖成「Gemini 组」一个数；现在两池各自可见。
于是面板会拆成三条：

| 条目 | 成员 | resetTime |
|---|---|---|
| `Gemini 组（其余 Gemini 模型 5 小时池）` | 3.8-flash 各档、gemini-4-hydrogen 各档 | 当日 5 小时窗口 |
| `Gemini 组（Gemini 4 Argon 周池）` | gemini-4-argon 各档 | 一周后（周一） |
| `Claude/GPT 组` | claude-opus/sonnet-5-5、gpt-oss | 当日 5 小时窗口 |

组内只有一个池时标签不加后缀，维持旧文案。`_quota_items_from` 与 `_pool_key`
共用同一个 `_exact_pools` 拆分，避免「面板显示的池」与「缓存 key 的池」口径不一致。

池后缀按池内是否含 `gemini-4-argon` 命名。若以后上游换了名字、冒出认不出的独立池，
两个池会落到同一个通用后缀而撞名——此时第二条起改用该池的**下次重置时刻**
（如 `Gemini 组（其余 Gemini 模型 5 小时池）（11-21 03:00 重置）`）做区分，保证 label 唯一。

**水位落差 → 积分（请求日志 Credit 列）**：上游没有积分字段，也没有公开抵扣公式，
唯一可观测的计费信号就是这个 `remainingFraction`。口径是**每条请求收尾读一次水位、
与上一次读数相减**（`core.credit_estimate.record_quota_mark`，水位差 ×1000 记成积分，
**标定值不是官方口径**）。前提是读数**必须来自固定连接**——2026-11-16 实测：

- 每次新建连接读：`0.7551521 / 0.7562798 / 0.7592398` 随机跳，还会「回升」——
  水位按连接/接入节点缓存，换连接就换了一份快照。**所以面板路径每次新建 client 没事
  （只看当前值），积分探测必须复用一个长连接**（`provider._fetch_watermark`）；
- 复用同一条连接连读 8 次：恒为同一值；
- 固定连接上连发 4 条请求，逐条收尾读数 `0.7562798 → 0.75617707 → 0.7560655 →
  0.7559592 → 0.7558411`，落差与该条请求体量对得上（1 token ≈ 0.0000015，
  4393 token ≈ 0.0008）。

**结论是「只能看量级」**：落差精度只有 4 位有效数字，同样规模的一条请求实测
0.11 / 0.12，也出现过 6.41（约 60 倍）；上游还会不规律回升（空转 30 s 实测
+0.0014，比一次大请求还多），回升那条记 `—` 并把基线跟到新读数。读不到水位、
水位没动、或落差 round(2) 归零时一律记 `—`，不拿 0 冒充「免费」。

**旧的按数计费口径**（2026-11-15，固定 prompt、每模型 6–10 次取前后水位差；量级仍可参考）：
- 旧代 Flash（`gemini-2.5-*`、`3-flash`、`3.1-flash-*`、`3.5-flash-lite`）当时**测不出**
  水位下降（9259 token 10 次 Δ=0），但不能断言 0 成本，需长时段验证；
- 计费池按 token 成本比例扣：3.8-flash 各档 ≈ `5.5e-5 /1k token`，
  gemini-4-hydrogen ≈ `8.1e-5`，gemini-4-argon ≈ `1.7e-4`（约氢的 2.1 倍、3.8 的 3 倍）；
- claude 组另一池：sonnet ≈ `1.4e-3`、opus ≈ `2.9e-3 /1k token`。

**读数的坑（用户实际反馈过）**：满额时 `used=0`、进度条是空的，容易被读成
「额度是 0」。要清楚这是**剩余**语义：CLIProxyAPI 显示 `100%` 是剩余比例，
buddy 显示「剩 1000 / 1000 · 已用 0%」是同一份数据的另一种口径，不是数据不一致。
因此 `remainingFraction=1` 时面板文案显式写「满额（组内各模型均为 100%）」，
并列出组内包含哪些模型（`models_in_group`）、说明 `resetTime` 只是 5 小时窗口的
滚动刷新点（`reset_note`，不代表整组/每周池重置）。

## 模型名与 effort 后缀（实测坑）

上游**只认 `fetchAvailableModels` 列表里的名字**，2026-10-02 实测：

- gemini 3/4 系**裸名会被伪装拒绝**——早期（2026-10-02）是 429
  `RESOURCE_EXHAUSTED`（伪装成配额错误的「模型不存在」），2026-11-15 复测多为
  404 `NOT_FOUND`——必须带 effort 后缀；各模型可用档位不同
  （`gemini-4-argon` low/medium/high，`gemini-4-hydrogen` 另有 `-xhigh`）。
- `gemini-3.8-flash` 的默认变体是 `-tiered`（自动档），实测 `-low`/`-medium`/
  `-high` 也可用（不是只有 `-tiered`，旧结论已被推翻）；`gpt-oss-120b` 只有
  `-medium`；claude 系**裸名已不可用**（见下一条），现在是 `claude-*-5-5`
  + `-low/-medium/-high`。
- **下架模型有两种伪装**：`gemini-3.1-pro`（`-low`/`-high` 均 404）、
  `gemini-3.6-flash`（裸名 404，`-medium` 两账号全 429 后挂起）；claude 这边
  更阴——`claude-sonnet-4-6` / `claude-opus-4-6-thinking` 直接回 **200**，正文却是
  「Claude Opus 4.6 is no longer available. Please switch to Claude Opus 5.5.」的
  空壳。所以「200 且非空」不等于模型可用，验新模型时要看正文。
- `upstream` 写**裸基名**、档位交给 `efforts`——写成 `gemini-3.8-flash-tiered`
  再加 `tiered` 档会拼成 `gemini-3.8-flash-tiered-tiered`（2026-11-15 真实 404
  事故，`test_models_table_upstream_names` 守着）。
- 上游列表还有 2.5 系、3.5 系、`*-agent`、`*-image` 等变体和 `chat_*`/
  `tab_*` 内部条目，模型表只暴露了实测可用的主力模型，其余见
  `models.json` 的 `_comment`。

所以 effort 后缀由模型表逐模型声明（`efforts` / `default_effort` /
`upstream`），`convert.apply_effort_suffix` 解析成上游真名，不按名字前缀猜。

**档位不进模型 id**：`/v1/models` 与已存的客户端配置里**只有基名**
（`antigravity/gemini-4-hydrogen`），档位取自**请求体**的 `reasoning_effort`，
缺省用 `default_effort`（hydrogen 是 `high`）。所以旧版文档里
`/model antigravity/gemini-4-hydrogen-xhigh` 这种写法现在会 400
`未知模型`——不是模型下架，而是 id 变了。要 xhigh 就在请求里带
`reasoning_effort: "xhigh"`（Claude Code 侧表现为「思考强度」设定），
或直接发 `xhigh` 之外的档位；`-xhigh` 等后缀只出现在上游真名与
`fetchAvailableModels` 里。

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
| gemini-3/4 系 | 带上要成对 | 必带（缺 → 400） | ✓ 200 |
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
`efforts`/`default_effort`），实测一发再置 `verified: true`。下线的模型**从
`models` 里删掉**、挪进 `_comment` 记一笔，整族已下架的再把基名前缀加进
`blocked_prefixes`（否则刷新会把它们灌回来）。实测判据是**正文**不是状态码
（下架模型也能 200）。

### 运行期「重新拉取」为什么不是全信上游目录

`refresh_models` 拿 `fetchAvailableModels` 做目录发现，但那张表**既脏又缺**，
不能当权威（2026-10-14 实测两账号均如此）：

- **脏**：仍回传已淘汰的旧代/内部变体（`gemini-2.5-*`、`gemini-3-flash(-agent)`、
  `gemini-3.1-flash-lite/-image`、`gemini-3.5-flash-*`，以及已下架的
  `gemini-3.1-pro*` / `gemini-3.6-flash*`）。全收进来 `/v1/models`
  就被灌满旧模型——用户「点一下刷新就拉下一堆没用的模型」就是这个原因。
  → `models.json` 的 `blocked_prefixes` 列这些**去 effort 后缀后**
  的基名前缀，刷新时命中即丢。只拦旧代，真·新模型（如 `gemini-4-argon`，不在表里）
  照常被发现收录。
- **`-disabled` 占位条目**：`gemini-4-carbon-disabled` 也照常回传，但它是列表里
  **唯一没有 `quotaInfo`** 的条目，`generateContent` 直接 404
  `Requested entity was not found.`。它落在 `gemini-4-*` 这个「真·新模型」区间里，
  前缀表拦不住，由 `model_catalog._BLOCKED_BASE_SUFFIXES`（`-disabled` 后缀）
  单独丢弃（2026-11-15 实测后加，此前刷新会把它灌进目录）。
- **缺**：**不回传**能路由但目录接口不吐的条目（claude/gpt-oss 族——上游只给单变体，
  收进来会把多档并成一个 effort 档）。全账号成功时若按上游目录下架，就把人工
  校验过的好模型洗掉。→ 刷新时无条件把 `verified: true` 的条目按其 upstream base
  注入发现集，保证**永不下架**。
- 2026-11-15 起删除的 4 个（`gemini-3.1-pro` / `gemini-3.6-flash` /
  `claude-sonnet-4-6` / `claude-opus-4-6-thinking`）本身**不再标 verified**，所以
  上游即使用旧目录回传也不会被钉回来；前两族还有 `blocked_prefixes` 兜底。

改 `blocked_prefixes` 时务必复核：任何前缀都不能命中某个 verified 条目的
upstream base（有单测 `test_refresh_blocks_stale_families_and_pins_verified_curated`
守着这条）。

## 延迟、限频与可用性（2026-11-15 实测）

完整表格见仓库根 `docs/antigravity-bench.md`，复测脚本 `tools/bench_antigravity.py`。
要点：

- **TTFT**（上游名、短 prompt、`maxOutputTokens=4096`）：`gemini-2.5-flash` /
  `gemini-3.5-flash-lite` ≈1 s，`gemini-3.8-flash-tiered` 7–10 s，
  `gemini-4-hydrogen-xhigh` 9–14 s、`-high` ≈11 s，`gemini-4-argon` 80–86 s，
  `claude-sonnet-5-5-medium` ≈1.7 s，`claude-opus-5-5-medium` ≈3.7 s，
  `gpt-oss-120b-medium` ≈1.4 s。hydrogen 四个 effort 档的耗时**不随档位单调**
  （`-low` 反而 34 s，思考 token 量才是主因）。argon 是周池，同为重推理但会排在
  其他请求之后（连测两次，后一次更慢）。
- **限频**：快档 20 req/s 连打 5 秒（100 次）全部 200，未触上限；免费层的主要
  约束是配额而非速率。
- **`remainingFraction` ≠ 排队深度**：它是池级水位，≈1.0 时 3.8-flash 仍可能
  等 30–70 s 首字。别用配额条推断"现在快不快"。水位读数还**按连接缓存**
  （见上文积分口径），跨连接比较水位差值没有意义。
- **`maxOutputTokens` 太小 → 空正文**：思考占满预算时上游回
  `finishReason: MAX_TOKENS` + `continuationToken` + 空 text，Anthropic 侧表现为
  `content: []`。至少 4096。
- **当天已确认下架并清理**：`gemini-3.1-pro`（404 `NOT_FOUND`）、`gemini-3.6-flash`
  （裸名 404，`-medium` 无 system 也 429 / 挂起）、`claude-sonnet-4-6` 与
  `claude-opus-4-6-thinking`（200 空壳 "no longer available"）；
  `gemini-4-carbon-disabled`（404 `NOT_FOUND`，上游占位条目，按 `-disabled`
  后缀挡在刷新之外，不在 `models` 里）。
- **目录缺口**：`gemini-2.5-flash` / `gemini-3.5-flash-lite` 直连可用且快，但上游
  不给它们 `quotaInfo`，故不放进对外目录（`models.json` 的 `_pinned_not_exposed`
  记着）；真要用可以显式请求上游名，或把基名挪进 `blocked_prefix_exceptions`
  当 verified 条目（需同步单测）。

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
