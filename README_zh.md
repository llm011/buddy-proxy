# buddy-proxy

> 一个本地多通道模型网关：把各家 AI IDE / 编码客户端（CodeBuddy、Trae、Qoder、Gemini、Antigravity……）的订阅额度转换成标准的 **OpenAI Chat Completions**、**Responses** 和 **Anthropic Messages** 协议——让你把这些模型接到 Codex CLI、Claude Code / CC Switch、OpenCode、Grok、Oh My Pi 以及任意 OpenAI 兼容客户端上，一个端点、按模型名路由。

> **English docs: [README.md](README.md).**

---

## 特性

- **多 Provider** — 除 CodeBuddy 外，内置 **Trae**（解密 Trae IDE 登录态直连底层模型）、**ZCode**（智谱 GLM）、**GLM 官方**（BigModel Coding Plan 官方 key，与 ZCode 同上游、凭据独立）、**火山方舟**（Volcengine Ark，模型/接入点走本地 `ark.json`、不进代码，Responses 优先自动降级 chat，三协议可直连 Claude Code）、**豆包**（纯 stdlib CDP 直连豆包工作 App）、**百度搭子**（DuMate 千帆桌面端本地代理，GLM / Qwen / Kimi）、**小米 MiMo**（API key，或复用 MiMo 桌面登录态）、**Qoder**（COSY 签名纯 Python 复刻，千问3.8 / GLM / Kimi）、**Gemini**（Google OAuth，Code Assist 免费额度——登录态与本机 gemini CLI 双向互通）与 **Antigravity**（Google Antigravity 免费额度——一个 OAuth 登录通吃 Gemini 3.x / Claude / GPT-OSS，可导入本机 `agy` CLI 登录态），统一经 `/v1/models` 列出、按模型名路由
- **多账号 failover** — 多数订阅通道（codebuddy / trae / qoder / kimi / codex / antigravity…）支持多账号：按登录顺位主备轮转，账号级错误（401 凭据失效 / 429 额度耗尽）自动冷却当前账号并切换下一个；逐账号额度、签到、改名 / 顺位 / 删除都在管理页操作
- **协议转换** — `/v1/chat/completions`(OpenAI)、`/v1/responses`(Codex CLI)、`/v1/messages`(Anthropic / Claude Code)
- **管理界面** — 内置 Web 控制台 `/ui`：按 provider 分组管理模型、一键「设为默认启用模型」、每个模型一键测试（发条 hi）、每个模型一键复制**带通道前缀**的全名（`trae/gpt-6`、`ark/ds-flash`——正是客户端 `model` 字段该填的值；「模型顺序」页每一档也可单独复制）、按模型维度聚合请求统计图表。模型页每个 provider 标题栏带「启用」开关（默认开）：关掉后该通道调用直接 403、`/v1/models` 不再列出、额度卡与告警隐藏、顺序页对应行置灰（codebuddy 是默认兜底通道，不可停用）；动态目录通道可点「↻ 重新拉取」，模型也可只“隐藏”（从目录/选择器消失但直接点名仍可调用，与停用不同），并能在「已隐藏」弹窗恢复。配置分别持久化在 `disabled_providers` / `hidden_models`。按页签懒加载，首屏不会卡在最慢的日志接口上
- **模型列表** — `/v1/models` 返回 OpenAI 兼容的模型列表，附带每个模型的完整元数据（上下文窗口、积分倍率、输入模态 / 图片支持）；本地隐藏项会同时从该端点、管理页目录和模型顺序候选中移除
- **脱敏**（`--desensitize`）— 向 system 消息里的合规关键词插入零宽空格，避免后端关键词审核误拦
- **消息压缩**（`--optimize-context`）— 压缩长历史 / 大 schema / 超大工具输出，大幅降低 token 消耗
- **工具调用** — 完整的 function calling 支持，自动过滤无效工具定义；`tool_choice` 在 OpenAI / Anthropic 两种形态之间统一归一化，绝不会以 object 形式发给上游
- **DSML 解析** — 自动识别并转换 DeepSeek Markup Language 工具调用
- **流式输出** — SSE 实时返回，带空闲 / 总时长双重超时保护
- **双协议** — 同一批模型同时提供 OpenAI（`/v1/chat/completions`）与 Anthropic（`/v1/messages`，即 Claude Code）；各 provider 负责把响应转回客户端要的协议

---

## 安装与启动

代理是 `src/` 下的普通 Python 包，用 [uv](https://docs.astral.sh/uv/) 安装依赖后，日常通过 `buddy` 脚本管理：

```bash
uv sync
./buddy install            # 一次性安装 buddy 命令到 PATH
buddy start                # 启动代理并打开管理页
```

尚未安装脚本时，在仓库目录内使用 `./buddy start`。登录上游用 `buddy login <provider>`；
`buddy` 是仓库提供的 shell 脚本，不是 `uv sync` 自动安装的 Python 命令。

首次运行会自动创建状态目录 `~/.buddy-proxy/`（权限 `0700`，可用
`BUDDY_PROXY_STATE_DIR` 整体覆盖）。机器本地的东西都在这里：`settings.json`
（默认模型、已停用/隐藏模型、已停用通道、限时窗口）、Trae Work 凭证 `trae_work.json`、PAT token
缓存 `trae_pat_token.json`、客户端名映射 `buddy_client_names.json`。目录内含凭证，
注意别被备份或版本控制带走。启动时会以 `[State] ...` 打印实际路径。

首次使用：按上文说明先安装 `buddy` 包装命令，然后启动代理并按需登录：

```bash
buddy start
buddy login codebuddy
```

默认监听 `http://127.0.0.1:8787`，管理界面在 **http://127.0.0.1:8787/ui**。

### `buddy` 命令（推荐）

`buddy` 是日常使用的入口：一条命令启动 + 自动打开管理页，也可以把代理注册成 macOS 系统服务（launchd，登录自启 + 崩溃自动拉起）。

```bash
# 安装前在仓库目录里用 ./buddy；一次性安装到 PATH 后可在任意目录用 buddy
./buddy install            # 安装到 /usr/local/bin（不可写时退回 ~/.local/bin）

buddy start                # 启动（未运行时）并打开 http://127.0.0.1:8787/ui
buddy stop / restart / status / logs
buddy login [provider]     # 登录/自检上游账号（codebuddy(=workbuddy)/trae(=traeintl 海外)/zcode/glm/doubao/dumate/mimo/qoder(=qoderintl 海外)/gemini/antigravity/kimi/codex）
                           # trae/qoder 可加 --region cn|global（两区账号不通用），其它 provider 忽略该参数；
                           # traeintl/qoderintl 是登录别名，等价 --region global；登录过海外账号会自动启用独立通道，
                           # 管理页里海外额度单独一张卡
buddy ui                   # 仅打开管理页（必要时先启动）
buddy update               # 更新到最新代码（git pull -> uv sync -> 重启）

# 注册为系统服务（launchd）：登录自启、崩溃自动拉起
buddy service install      # 之后 start/stop/restart 自动走 launchctl
buddy service status
buddy service uninstall
```

- 有系统服务时 `buddy start/stop/restart` 自动转 `launchctl`，否则走 `proxy.sh` 的 pid 管理
- 环境变量 `PROXY_HOST`（默认 0.0.0.0）、`PROXY_PORT`（默认 8787）、`PROXY_EXTRA_ARGS` 对 `start` 与 `service install` 生效
- `buddy update` 在安装来源那个仓库里跑 `git pull --ff-only` → `uv sync` → 重启服务。**工作树有未提交改动时它会直接拒绝执行**——不 stash、不 merge、不碰任何在制品，所以写了一半的改动不可能被悄悄覆盖。拒绝时会把「脏在哪」列出来，并按实际情况给对应的处理办法：只要含未跟踪文件就提示 `git stash -u`（普通 `git stash` 收不走未跟踪文件，照做会带着同一个 `??` 再被拦一次），纯已跟踪改动才提示普通 `git stash`。`--ff-only` 保证本地分叉时明确报错、而不是替你造一个意外的 merge commit——报错信息里会引 git 自己的原话（`Not possible to fast-forward` 对应本地分叉，`Could not read from remote` 对应连不上远端），好让你一眼分清是哪种。`uv sync` 失败则保留服务继续运行，不会用坏掉的依赖去重启。已经是最新版本时跑它也无害：只是重新同步依赖并重启一遍。

### 用 `proxy.sh` 后台管理

`buddy` 内部即调用 `proxy.sh`；想手动精细控制时可以直接用它：

```bash
./proxy.sh start          # 后台启动，立刻返回
./proxy.sh stop           # 停止
./proxy.sh restart        # 重启
./proxy.sh status         # 显示 PID 和监听地址
./proxy.sh logs           # tail -F 日志
./proxy.sh ui             # 确保在跑并打开管理页

# 自定义 host / port / 额外参数
./proxy.sh start -p 9000 -H 0.0.0.0
PROXY_PORT=9000 PROXY_EXTRA_ARGS="--desensitize --optimize-context" ./proxy.sh start
```

脚本行为：
- 自动检测 `.venv/bin/python`（优先使用项目 venv）
- 用 `nohup ... &` 启动，`start` 命令**立即返回**，不会阻塞终端
- PID 写到 `logs/proxy.pid`，启动输出写到 `logs/proxy.sh.log`；应用日志按天滚动（`logs/proxy.log` 与 `logs/buddy-proxy.jsonl`，保留 30 天）
- 停止用 `kill` 优雅退出；10s 内未退出会 fallback 到 `kill -9`

## 通道（Providers）

每个通道都是可选的：有什么订阅就启用什么，`buddy login <provider>` 登录，`/v1/models` 自动合并各家目录。多个通道可同时在线；多个通道都声明的裸模型名按注册顺序解析——要钉死某一家就用显式 `<provider>/` 前缀。

### CodeBuddy（默认通道）

腾讯 CodeBuddy / WorkBuddy 订阅，走 IDE 插件认证（浏览器 OAuth，`buddy login codebuddy`，别名 `workbuddy`）。能力：

- **多账号 failover** —— 账号存于 `~/.buddy-proxy/codebuddy/`（`index.json` + 每账号一份凭据文件，0600）。同账号重登只更新凭据、failover 顺位不变；新账号追加到末位。撞 401（凭据失效）/ 429（额度耗尽，如 code 14018）时该账号进入冷却（60s / 5min，尊重 `Retry-After`）并自动切换下一个账号——单账号额度耗尽不再拖垮整个通道。历史的 `~/.codebuddy-session.json` 首次触达时自动迁移为账号 #1。
- **每日签到** —— 逐账号活动签到（连签积分），管理页聚合展示、逐账号明细；领取时把每个可领的账号都领一遍。
- **积分** —— 逐账号资源包汇总（管理页按 `CodeBuddy #N · …` 分组），每个资源包条目带**有效期**
  （「· MM-DD 到期」，从资源实例接口 `get-user-resource` 按包聚合生效中实例的最晚周期结束时刻；
  订阅包=月结周期结束、裂变包=最近一次发放的窗口结束；聚合接口失败只丢有效期、不影响额度数字），
  另有按请求粒度的消耗流水（`/ui/api/codebuddy/usage-records`）。
- 账号管理：`GET /ui/api/codebuddy/accounts` 及 `order` / `rename` / `delete` 端点；管理面板支持逐账号 ▲▼ 顺位、✎ 改名、✕ 删除。
- **海外版（`codebuddyintl`）** —— 同上游家族、换 host：`https://www.codebuddy.ai`，2026-10 实测两边 `/v2/plugin` 协议逐字节同构。登录：`buddy login codebuddy --region global`（别名 `codebuddyintl`）；海外账号在同一账号 store 里落 `region` 标，与 CN 账号互不混用。有海外账号后重启 Buddy 自动注册通道——模型用 `codebuddyintl/<模型>` 前缀显式路由，管理页单独一张「CodeBuddy 海外版」额度卡（额度 + ✎ 改名；▲▼/✕ 仍在 CN 卡）。海外登录态写在 `~/.codebuddy-session-global.json`（绝不写 CN 的历史 session 路径，避免 legacy 迁移把海外号并进 CN 区）。模型目录不再采用桌面包中过期的 `product-ide.json`：其中 Claude 3.7/4.0、GPT-5、Gemini 2.5 已全部实测返回 11102。当前表来自海外客户端在线倍率面板并逐个用真实账号调用通过（2026-10-10），同系列只保留新一代：`hy4-preview`（x0.00）、`gpt-5.6-sol`（x3.47）、`gpt-5.6-terra`（x1.39）、`gpt-5.6-luna`（x0.14）、`gemini-3.5-flash`（x0.99）、`glm-5.3`（x0.79）、`kimi-k3`（x1.62）。

### 豆包 Provider（可选）

除 CodeBuddy 外，内置豆包 provider（纯 stdlib CDP 直连本地「豆包工作」App，零额外依赖）：

```bash
# 启用豆包 provider（需要本机已安装并登录豆包工作 App）
buddy start
```

- **原理**：复用豆包工作 App 的登录态与内置 Chromium（CDP 直连），在页面 JS 环境 fetch
  自动注入 a_bogus 风控签名，无需扫码、无需额外凭证
- **依赖**：纯 Python 标准库，不需要 Playwright / chromium

#### 正确使用姿势（重要）

豆包通道的本质是**代理替你操作本机豆包桌面 App**（DoubaoWork.app）——通过 Chrome 调试协议
（CDP，端口 9223）连进 App 内置浏览器，以你的登录态发请求。因此有一条关键原则：

> **让代理来拉起豆包，不要自己先打开豆包 App。**

- 首次 doubao 请求（或在管理页对豆包模型点「测试」）时，代理会自动：以调试模式拉起豆包 →
  连接内置浏览器 → 导航到聊天页 → 确认登录态，之后一直复用这条连接
- 如果豆包**已经先被你打开了**（没带调试参数），代理无法给运行中的 App 追加启动参数，
  首次请求会报 502：「豆包主 App 正在运行但未开启 CDP 调试端口」。这是刻意设计
  （代理绝不强杀你正在使用的 App）

#### 常见失败与恢复

| 现象 | 原因 | 恢复 |
| --- | --- | --- |
| 502「豆包主 App 正在运行但未开启 CDP 调试端口」 | 豆包先于代理启动（或你手动开过豆包） | **完全退出豆包（Cmd+Q）→ 再点一次测试/重发请求**，代理会自动以正确参数拉起它 |
| 豆包 App 升级/重启后请求开始报错 | CDP 连接已失效。旧版本会一直复用这条死连接，**重开 App 也没用，只能重启代理** | **本版本起自愈**：发现连接失效会先丢掉旧 socket 再重连；断线如果发生在**还没吐出任何内容之前**，会自动重连并重试一次，所以重开 App 就够了。只有重连也失败（App 确实不在了）才会报 409「CDP connection lost」，此时重新拉起豆包再重试即可；409 是刻意设为不可换档的，不会把模型打进冷却 |
| 豆包工作 App 点不开 / 开了又退（代理已连过豆包） | 无界面的**裸 Helper**（`--saman-from-chat=1`）和 App 共用 `~/Library/Application Support/DoubaoWork`，抢走了 Chromium 单例锁（`SingletonLock -> <host>-<pid>`）；锁即使指向已死的 pid 也不会自愈，App 就一直起不来 | 本版本起代理会**自动收掉**：复用端口上的 CDP 前先确认它不是裸 Helper，并删除失效的 `SingletonLock`/`SingletonSocket`/`SingletonCookie`。手动恢复只需三步：`kill -9 <裸Helper的pid>` → `rm -f ~/Library/Application\ Support/DoubaoWork/Singleton{Lock,Socket,Cookie}` → `open -a DoubaoWork`。**不要**用 `pkill -f "DoubaoWork Browser"`——那会连带杀掉 App 自己的内嵌 Helper |
| 401「doubao not logged in」 | 豆包内登录态失效 | 打开豆包 App 重新扫码登录 → 重试 |
| 报「请先完成豆包扫码登录」 | 首次使用尚未登录 | 代理拉起豆包后在 App 里登录，等几十秒自动就绪 |

> 实测口诀：**豆包报错，先 Cmd+Q 退出豆包，再点一次测试**。90% 的豆包通道问题这一步就解决。

#### 模型列表

- 经典通道：`doubao`（默认模型快速）、`doubao-pro`（旧别名）、`doubao-think`（深度思考）、
  `doubao-expert`（专家）——服务端固定路由到当前默认豆包模型，请求里的 model 字段会被忽略
- agent 通道（App 模型菜单同款协议，真正按模型路由，2026-09 实测）：`doubao-auto`（App「自动」）、
  `doubao-2.1-turbo`、`doubao-2.1-pro`（额度消耗更快）、`orange-5.0`（支持极高/最高推理强度）、
  `gemini-3.7-flash`、`gpt-5.6-sol`（App 内提供的第三方模型）；
  请求体可带 `reasoning_effort`（3低/4中/5高/6极高/7最高，默认 5）

#### 签到

管理页「各通道签到」里豆包占一行，勾选「自动打卡」后由打卡循环每日领取，也可点「立即打卡」。
签到走 App 内置 Chromium 的**独立签到页面连接**（`doubao/checkin.py`）：复用已打开的
`https://www.doubao.com/pc/agent-explore` webview，没有时才创建后台页并只关闭自己创建的页；
不切换聊天 target、不导航或关闭用户页面。签到会话从选择 target 到请求、清理全程串行，手动领取与后台刷新不会在同一活动页上交叉执行。2026-11-01 在 DoubaoWork 2.34.12 实测，真实活动是
campaign v3 的 `pc_incentive_task_mvp_v3` / `daily_signin`，状态用
`POST /alice/ug/campaign/v3/detail/`（body 带 `campaign_group_code`），领取用页面 SDK 同款的
`POST /alice/ug/campaign/landing/checkin`（body 为 `scene`），不是旧版猜测的 `/api/activity/*`。
详情响应先在页面内提取签到任务，避免大段 `example_cases` 被截断；已签状态不重复领取，
领取必须看到 `data.checked_in=true`，再由 detail 复查确认。上述请求去掉 `msToken` / `a_bogus`
仍实测成功，不再借其它 URL 的签名。查询只认 `{"code": 0}` 和明确的 `daily_checkin` 字段；
App 未登录或响应形状不认识时卡片显示「状态未知」（不会误画成「未签到」可点），原因写在
代理日志里（`doubao checkin …`）。

登录态**只问服务端**，不看 `localStorage.flow_web_has_login`（前端缓存，服务端会话过期后仍是
`true`）：在豆包同源页对真实聊天路由 `/chat/completion` 空 POST，401 或业务码 `710012001`
= 未登录；200 + API JSON 或带业务 `error_code` 的 SSE = 已登录；其余（无响应 / 404 / 5xx /
200 + HTML）= 未确认，按「状态未知」处理，不把探针失败说成已登录或未登录。

### 百度搭子 Provider（可选）

内置 DuMate（百度搭子 / 千帆桌面端）provider，直连本机 App 的本地 OpenAI 兼容代理：

```bash
# 启用 DuMate provider（需要本机已安装并登录 DuMate.app 且在运行）
buddy start
```

- **原理**：复用 DuMate.app 内置的本地代理（`dumate-main-server`，监听
  `127.0.0.1:<port>`），以你的百度云登录态直连 `dumate-svc.baidu.com` 网关。
  无需扫码、无需配 token——本地鉴权 key（`X-Dumate-Inapp-Key`）从运行中的
  App 进程环境自动抽取，每次 App 重启自动轮换
- **前置**：本机已安装百度搭子桌面端并登录百度云账号，且 App 正在运行。
  重启电脑 / 退出 App 后重新打开即可，`buddy login dumate` 只做状态自检
- **模型**（2026-10 抓包 + 逐个实测）：`dm-auto-model/text.L0`（自动路由，
  App 默认）、`kimi-k3`、`qwen3.8-max`。上下文 192k / 输出 128k，支持
  function calling，system prompt 完全可控且如实计费（实测 9.6k 字符 system
  精确计入 prompt_tokens）。deepseek / glm-5.3 / 海外模型（claude/gpt/gemini）
  未对百度账号开放
- **签到**：自动打卡循环每日领取（`POST /api/dumate/points/loginBonus`，
  bceConsole 通道）；管理页显示累计签到积分
- **额度**：走 App 自己的 bceConsole 接口拿数字积分（`GET /api/dumate/points/quota_overview`，
  注意是下划线版；camelCase 的 `quotaOverview` 永远 500）：订阅总额/已用/剩余 +
  各积分包明细（含到期日）。未登录时退回本地代理的布尔态 `/api/dumate/points/remaining`
- **消耗流水**：真实逐笔扣减走 `GET /api/dumate/points/records/usage`
  （`startAt`/`endAt` 是**秒级**时间戳，毫秒会 500）。面板副标题显示「今日消耗
  N 分（M 次）」；`GET /ui/api/dumate/usage-records?days=N&page=&limit=` 可查原始流水
- **协议**：OpenAI chat completions；Anthropic /v1/messages 会把 OpenAI 响应转回
  anthropic 形态（与 kimi/qoder 同一套适配器），Claude Code 可直接调用
- **依赖**：纯 Python 标准库（含零依赖 AES-256-GCM 实现解密 bceConsole cookie）

### Trae Provider（可选）

除 CodeBuddy 和豆包外，内置 Trae provider（解密 Trae IDE 登录态，直连底层模型）：

```bash
# buddy start 默认已启用 Trae（需本机已安装并登录 Trae IDE）
buddy start
```

- **原理**：自动解密 Trae IDE 本地存储的 tc 加密登录态（AES-128-CBC + SHA-512），
  或从 `.env` 读 `TRAE_TOKEN` / `TRAE_USER_ID`，直连 `trae-api-cn.mchost.guru`
- **模型**：目录列出已验证可用的模型名及其别名（如 `deepseek-v4-pro` → `DeepSeek-V4-Pro`，
  对外统一小写、转发时还原上游大小写敏感名；step-5-preview 这类上游只认全小写的则原样透传），
  顺序沿用上游客户端的展示顺序。
- **原生通道（2026-09 起）**：全部请求（含纯聊天）默认走 `chat_v3` 直通——
  带 `tools` 时为原生 function calling（结构化 `tool_calls` + `role:"tool"` 历史回放），
  纯聊天无服务端 agent 预设、不再注入压制指令与泄漏清洗；原生请求遇 `4001` 会回落文本协议。
  2026-10-07 起上游整体拒绝 `chat_v3` 的**流式**请求（同 body 非流式正常），流式客户端请求
  会先降级 native 非流式重试（上游整段缓冲，仍包装为 SSE 输出），也被拒才落文本协议兜底
  （同为非流式上游调用）；兜底链用**失败后节流**对抗上游的快速重试惩罚——上游对刚失败
  （4001）的账号有惩罚窗：一次流式探测被拒会把该模型「毒」~30-60s（连非流式一并
  被拒），且窗口随连续失败拉长到数分钟。代理记住该模型最近一次 4001 时刻，此后的
  上游尝试先安静等到 25s 窗外再发（`WB_TRAE_REJECT_QUIET_S` 可调；等待不产生失败，
  窗口只衰减不增长；native 一旦成功就清掉时间戳，后续请求不再陪等），
  期间读线程照常喂心跳、客户端无感；非流式被拒时先节流原路重试
  一次、再落文本协议；被拒后该模型记 ~30 分钟 TTL 标记（`WB_TRAE_STREAM_REJECT_TTL_S` 可调；探测本身会毒化安静窗，别调太短），期间直接跳过必败的
  流式尝试，每个 TTL 窗口只有一次探测付兜底税；实测 17 个模型
  16 个原生可用（仅 glm-5-turbo 不在通道）；通道还带真实 token usage
- **依赖**：纯 Python 标准库（含零依赖 AES 兜底实现），不需要 Node.js
- **注意**：免费账号有日/周调用额度，耗尽时报 `4011`（今日用量已达上限），
  错误会以友好中文文案透传
- **海外版（`traeintl`，2026-10 起）**：`buddy login trae --region global` 登录海外账号后
  自动启用独立通道（`traeintl/<model>` 寻址、额度卡单独一张、无签到——海外上游没有签到端点）。
  模型池与 CN 是**两套**（2026-10-06 probe 实测收录；`gpt-5.2` 2026-10-07
  用户点名移除，现为 9 个）：`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`、
  `gpt-5.6-terra`、`gpt-5.6-luna`、`kimi-k3`、`gpt-5.4`、
  `glm-5.2`、`minimax-m3`。协议与 CN 两处不同：`messages[].content` 必须是内容块数组
  （纯字符串上游 400 反序列化错）；function 绑定按区分表——gpt-5.6 系 / `glm-5.2` /
  `minimax-m3` 在默认 `solo_work_lite` 下 4001，自动改走 `chat_v3`。IDE 下拉里有但实测
  agent 通道全 4001 的（`gpt-6-astra` / `glm-5.3` / `deepseek-v4.1-flash` / `gemini-*-preview`）
  不收录。额度只有 **Basic 美元用量**（Pro 包，`$` 真实已用/上限）：`premium_model_fast_request_limit`
  是「快速请求档位可用」的能力位、不是可花的额度（2026-10-25 用户纠正），不铺成额度行；
  `is_hide` 的垃圾包自动过滤

#### Trae 账号工具（可选：`trae-cli`）

`trae-cli` **不是登录必需工具**。Trae Work 登录请用 `buddy login trae`：它会打开授权 URL，
在浏览器完成授权后由本地回调自动接收结果。`trae-cli` 是独立的可选工具，仅用于查询/领取签到积分、
查看权益和测试对话。在仓库目录中通过项目环境运行：

```bash
trae-cli status            # 签到/积分状态（剩余积分、今日是否已签到）
trae-cli claim             # 领取今日签到积分
trae-cli usage             # 权益/用量（总额、已用比例、权益包列表）
trae-cli chat -m glm-5.3 -q "你好"    # 发一条对话测试
```

`uv sync` 会把该命令安装到项目虚拟环境；若单独安装 Python 包，命令则位于对应 Python 环境中。
认证自动加载：优先 Work 多账号状态目录 `~/.buddy-proxy/trae/`（`index.json` + 每账号
一份 0600 cred；由 `buddy login trae` 生成/追加，按 uid/refresh_token upsert——重登同号更新、新号追加）。
历史单账号 `~/.buddy-proxy/trae_work.json`（及遗留 `~/.ethan/trae_work.json`）会在首次读取时自动
迁移为账号 #1。其次解密本机 Trae IDE `storage.json`，无需手动配置 token。

国内版 Trae Work 模型目录使用小写 ID，方便与其他 provider 的 `model_order` 统一配置；例如 `doubao-seed-evolving`、`deepseek-v4-pro`。转发时会自动转换成上游要求的大小写敏感名称（如 `Doubao-Seed-Evolving`），客户端和顺序配置无需写大写。旧模型 `kimi-k2.7-code`、`deepseek-v4-flash`、`glm-5`、`glm-5-turbo` 已从目录移除
（`glm-5.2` 与 `qwen-3.7-plus` 2026-10-25 实测重新可用，但被「代际收敛」从列表隐藏——
点名写 `trae/glm-5.2` / `trae/qwen-3.7-plus` 仍可调用）。

Work 通道多账号**主备 failover**：按登录顺位优先用 #1，账号级错误（401 凭据失效 /
429 额度）冷却 60s/5min 后自动换下一个——上游 SSE 的额度码（4008 quota exceeded /
4011 / 4021 / 4031）与鉴权码（1001 / 4010）按码映射成 429/401 进同一分类
（2026-10-06 起：此前一律 502 透传不换号，双账号一空一满也会直接报错）；
签到/额度**每账号都查都领**，管理页有
`▲▼` 顺位调整 / `✕` 删除面板（同 qoder/kimi/antigravity）。
多账号时签到状态带 **per-account 明细**（`accounts`：index/name/状态），签到卡逐账号
渲染一行（已签到 / 可领 / 查询失败 + 原因）——只有聚合徽标说不清哪个账号没签上；
手动打卡的 toast 也逐账号报结果（谁 +N、谁失败）。明细里的 name 走
alias > nickname > uid > id 显示名链（✎ 改的名与额度面板一致），行尾带 ✎ 改名入口。
单账号无明细，行为不变。

**签到设备指纹（per-account）**：签到接口是 device 维度的（一天一签、错误码随
device 变化），早期写死的「ASUS TUF + windows」指纹（trae2api 方案原版）已被上游
风控拉黑——该指纹下新设备首签一律 9074「当前参与用户太多」（2026-10-06 实测：
换机型立即成功；同刻 status 畅通，与活动热度/并发无关）。现在每账号一台**稳定**
设备：机型从真实机型池按账号 hash 派生，首签成功后把
`(device_id, brand, type)` 固化进 `~/.buddy-proxy/trae/devices.json`（0600），
之后每天同一台；claim 撞 9074 时自动换下一台备选机重试并覆盖固化记录。9095
「当前设备今日已经签到」按幂等成功处理（消息标「设备今日已签」）。

> **`buddy login trae` 排障**：若浏览器显示「登录成功」而 CLI 一直停在等待界面，
> 九成是登录 URL 的参数没和 Trae CN 客户端对齐。关键项 `plugin_version` 必须是
> **`trae-handoff-1.0`**——传版本号形态会被授权页当版本号规范化并**丢掉
> `auth_callback_url`**，回调永不发起。另外回调是**跨源 fetch**（`redirect=0`），
> 本地服务必须回 `Access-Control-Allow-Origin`，否则请求会被浏览器直接拦掉。
> 回调服务带脱敏访问日志（`[srv] GET /authorize?...`），可据此判断浏览器到底有没有打过来。

### MiMo Provider（可选）

小米 **MiMo**（platform.xiaomimimo.com），以 `mimo/<id>` 寻址（`mimo-auto`、`mimo-pro`）：

```bash
buddy start
buddy login mimo     # 打印当前生效的认证模式，或配置指引
```

认证两种，按序尝试：

1. **API key** — `MIMO_API_KEY`（配 `MIMO_BASE_URL` 可切 billing/token-plan 域）、
   `~/.mimocode/auth.json`（MiMo 桌面「API Key」模式写入）、`~/.buddy-proxy/mimo_api_key.json`。
2. **小米 SSO** — 复用本机已安装的 **MiMo 桌面** 登录态：自动读取其账号 cookie，复刻
   桌面端「两阶段换 `serviceToken`」，过期自动刷新、被拒自动重试一次。无需复制粘贴 cookie。

MiMo 上游是 OpenAI 形态，请求原样转发；**但 Anthropic（`/v1/messages`）例外**——上游没有
Anthropic 原生端点，所以要把响应**反向转换**成 Anthropic 事件流（`message_start` /
`content_block_delta` / `message_stop`，含 `thinking` 与 `tool_use` 块），否则 Claude Code
会报「0 stream events received」/「body is JSON but not a Message」。

> 排障提示：`30012` 是「**未开通会员**」而不是 token 失效——遇到它本 provider 不会去重换票。
> 管理页额度面板的 `percent` 已由上游的「剩余」换算成「已用」，且管理页显示的
> 「已用 N%」用的是 `100 - 剩余`。

### 9. Codex Provider（`codex/` 子包，可选）

把 **ChatGPT 订阅**（Plus / Pro / Team / Free）的 Codex 额度变成一条上游通道，以
`codex/<id>` 寻址：

```bash
buddy login codex    # 浏览器授权（回环 1455 收回调）；打不开浏览器时粘贴回调链接
```

- **不需要安装 Codex CLI**：登录走 OpenAI 公开的 OAuth 客户端（PKCE S256），与 Codex CLI
  同一套授权，本机没有 `~/.codex/auth.json` 也能登。反过来，已有的 `~/.codex/auth.json`
  可以在管理页面板「导入账号」里直接粘贴，或从别的机器导出后导入。
- **协议**：上游是 **Responses** 协议（`https://chatgpt.com/backend-api/codex/responses`，
  SSE），与其它通道的 Chat Completions 不同。本通道内置双向投影：请求方向把 chat body
  转成 Responses（system → `instructions`、工具平铺、assistant 历史里的推理不回放，
  因为 Responses 的 reasoning item 需要上游签名）；响应方向按入口分派——`openai` 把
  Responses SSE 转 chat delta，`responses` 原样透传，`anthropic` 再转 Anthropic 事件流。
- **多账号 failover**：重复 `buddy login codex` 换账号登录即追加备用号；401 先强刷一次
  再换号，429/403 冷却换号（`Retry-After` 优先），5xx 透传不换号。OpenAI 会**轮换
  refresh_token**，刷新得到的 RT 立即落盘。管理页支持 ▲▼ 顺位、✎ 改名、✕ 删除与导入。
- **额度**：ChatGPT 后端没有公开的额度查询端点，面板只展示各账号套餐档位与登录状态，
  不编造 `used_percent`（`rate_limits` 的解析接入点预留为
  `codex/upstream.py::parse_rate_limits`）。
- **上游只收 SSE**：`/responses` 必须 `stream: true` + `store: false`（否则直接 400）。
  因此非流式客户端（不带 `stream` 的 `/v1/chat/completions`、`/v1/messages`）也是先以
  流式请求上游、收到 `response.completed` 后再聚合回非流式响应，行为与直连一致。
- **模型目录 + 「↻ 重新拉取」**：对话模型来自 `GET /backend-api/codex/models?client_version=`
  （这张表按客户端版本分代，版本号太旧就看不到新模型，因此固定用 `0.159.0` 查询），
  只收 `visibility=list` 且 `supported_in_api!=false` 的 slug。模型页「重新拉取」会用当前账号
  重拉并写入 `~/.buddy-proxy/codex/models.json`（运行期快照，优先于随包的
  `codex/models.json`；不改仓库文件）。上游返回空表时保留旧快照，绝不把目录清空。
  当前可见：`gpt-6.1-sol`（默认，500K 上下文）、`gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`、
  `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`；`gpt-reserve` / `codex-auto-review` 是
  内部用途（`visibility=hide`），不进列表。
- **生图**：`gpt-image-2.5`（默认）、`gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`、
  `gpt-image-2`、`gpt-image-1.5` 走 `POST /backend-api/codex/images/generations`。用法与
  对话模型一样，用 chat 协议把 prompt 放进 user 消息即可，图片以 data URL 返回在
  `choices[0].message.images[]`（`/v1/messages` 出口转成 Anthropic `image` 块，
  `/v1/responses` 出口转成 `output_image`）。上游首帧是十几秒的 `keepalive` 心跳，
  心跳不计入「已提交」，生图失败同样按 401/403/429 换号。

### 10. 火山方舟 Provider（`providers/ark.py`，可选）

把 **火山引擎方舟（Volcengine Ark）** 的 API key 变成一条上游通道，以 `ark/<id>` 寻址：

```bash
python -m buddy_proxy --ark           # 或 ARK_ENABLED=1；默认 EXTRA_ARGS 里已带 --ark
```

- **模型与 key 一律不进代码**：方舟按**自定义推理接入点**（`ep-...`）计费，接入点 id 是
  账号私有资源（本通道**故意不带任何默认模型表**），所以模型表完全来自本地配置：

  ```bash
  # 1) key：首行一个裸 key，权限 0600
  mkdir -p ~/.buddy-proxy && printf '%s\n' 'YOUR_ARK_API_KEY' > ~/.buddy-proxy/ark_api_key
  chmod 600 ~/.buddy-proxy/ark_api_key
  ```

  ```json
  // 2) ~/.buddy-proxy/ark.json
  {
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "models": [
      {"id": "ds-flash", "endpoint": "ep-xxxxxxxxxxxxxxxx", "name": "DeepSeek Flash"}
    ]
  }
  ```

  `id` 是**对外模型名**（`/v1/models` 里就是 `ark/ds-flash`），`endpoint` 是上游真名
  `ep-...`（裸模型名的账号改写成 `"model": "..."` 亦可，二者择一）。`base_url` 可选，
  缺省北京区。配置缺失或写坏时**模型表为空而不报错**，`ensure_auth()` 会给出指向这两个
  文件的提示。环境变量 `ARK_API_KEY` / `ARK_BASE_URL` 优先于文件。

- **上游 Responses 优先、自动降级**：默认打 `/api/v3/responses`；若该接入点回
  `404 InvalidEndpointOrModel*`（明确「这个接入点干不了 responses」），本进程后续改走
  `/api/v3/chat/completions` 并只记一次日志。**400/401/403/429 一律不降级**——那是请求
  或账号的问题，换端点只会把真错误洗成另一种形状。`ARK_UPSTREAM_PROTOCOL=auto|responses|chat`
  可强制指定。
- **三协议全支持**：`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 都通。
  Anthropic 出口复用 `dumate` 同一套事件机（`reasoning_content` → thinking 块、
  `tool_calls` → `tool_use` + `input_json_delta`），Claude Code 可直连；非流式
  `/v1/messages` 回一整个 Message JSON，不是 SSE。
- **首事件闸门**：上游 200 但带内是 `error` / `response.failed` 帧时，在吐第一个字节前
  还原成 HTTP 错误（`_iter_upstream_events` 解析路径与 `_pass_through_stream` 透传路径
  各有一道），不会变成「200 空流」。
- **额度**：方舟没有可匿名查询余额的公开接口，本通道 `quota()` 返回 `None`，管理页不出
  额度卡（不编造数字）。
- 发往上游恒为 `stream: true`，非流式客户端在本地聚合；回给客户端的模型名永远是
  `ark/<id>`。接入点背后托管的平台模型名（实测方舟在 Responses 事件里回的是
  `deepseek-v4-1-flash-261017` 这类底层真名，和 `ep-...` 毫无字面关系）也会被改写成
  对外名——这个名字配置里写不出来，所以由透传层从上游首条事件里学到，同一行即生效。
  另外 `response.incomplete`（`incomplete_details.reason == "max_output_tokens"`）是**正常的
  截断收尾**而不是错误：它终止流并把 `finish_reason` 标成 `length`。
- **裸名撞车**：`ark.json` 的 `id` 若与 CodeBuddy / Trae 静态目录里的模型同名，裸名仍归
  那些通道（否则新增 ark 会悄悄抢走既有流量），ark 的这个模型用 `ark/<id>` 调用
  （`route_priority = 50`，CodeBuddy 是 100）。模型页每行「复制」按钮复制的就是带前缀的
  全名，直接填进客户端的 `model` 字段即可。

## 模型列表

`src/buddy_proxy/web/models_config.json` 中的静态目录包含 **CodeBuddy 和 Trae PAT 共 51 个条目**，无需访问上游即可读取；Trae Work 等其他通道由 provider 自己提供目录。下方列出 CodeBuddy 和 Trae Work 模型，国内 Trae Work 目录定义在 `src/buddy_proxy/trae/config.py`。`GET /v1/models` 的 `data[].credits` / `models[].credits` 会返回积分倍率（消费 × 倍率）：

**CodeBuddy 通道**（19 个）——直接用模型名，无前缀：

| id | name | credits |
|---|---|---|
| `auto` | Auto（快速 / 均衡 / 极致 → 0.21 / 0.65 / 1.20） | 动态 |
| `default` | Default | x2.20 |
| `glm-5.3` | GLM-5.3 | x0.79 |
| `glm-5.3-flash` | GLM-5.3-Flash | x0.06 |
| `glm-5.3-flashx` | GLM-5.3-FlashX | x0.14 |
| `glm-5.2` | GLM-5.2（夜间折扣） | x0.79 |
| `glm-5.1` | GLM-5.1 | x0.79 |
| `glm-5v-turbo` | GLM-5v-Turbo（读图） | x0.71 |
| `hy3` | Hy3（限时免费） | x0.00 |
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

> 关于 `glm-*`：裸名会落到注册顺序里第一个声明它的通道（**zcode**，它提供
> `glm-5.3` / `glm-5.3-flash`）。唯独 `glm-5.3-flashx` 在 zcode 上被拒为
> `1311 当前订阅套餐暂未开放GLM-5.3-FlashX权限`，而 **CodeBuddy 能正常服务**
> ——这一个请显式写 `codebuddy/glm-5.3-flashx`。官方 **`glm/`** 渠道（下文）与
> zcode 同上游同模型表，想走官方套餐的 key 时显式写 `glm/<模型>` 前缀。

**Trae Work 通道**（对外列出 15 个国内模型，ID 统一小写）。倍率是当前目录值：

| 模型 ID | 倍率 |
|---|---|
| `glm-5.3` | x0.39 |
| `glm-5.3-flash` | x0.06 |
| `glm-5.3-flashx` | x0.31 |
| `deepseek-v4.1-flash` | x0.08 |
| `doubao-seed-evolving` | x0.08 |
| `doubao-seed-2.1-pro` | x0.08 |
| `qwen3.8-max` | x1.50 |
| `doubao-seed-2.1-turbo` | x0.20 |
| `step-5-preview` | x0.48 |
| `doubao-seed-code` | x0.06 |
| `mimo-v2.6-flash` | x0.06 |
| `mimo-v2.6-pro` | x0.15 |
| `qwen3.8-flash` | x0.08 |
| `kimi-k3.1-pro` | x1.20 |
| `minimax-m3.1-flash` | x0.09 |

**代际收敛**：同一家族只要目录里有更高代际在列，旧代就不再出现在 `/v1/models`
里，但**点名照样能调**（显式写 `trae/<旧名>` 即可）。上面这批把下面这些旧代收掉了：
`glm-5.1` / `glm-5.2` 让位给 `glm-5.3`，`minimax-m3` 让位给 `minimax-m3.1-flash`，
`kimi-k3` 让位给 `kimi-k3.1-pro`，`qwen-3.7-plus` 让位给 `qwen3.8-max` / `qwen3.8-flash`，
`deepseek-v4-pro` 与 `deepseek-v4-flash-official` / `deepseek-v4-pro-official`
让位给 `deepseek-v4.1-flash`。倍率仍按静态底表留着（旧配置继续可用）。
解析不出代际的名字（如 `doubao-seed-*`、`mimo-*`）不参与收敛，照常列出。

`kimi-k3.1-pro` 在客户端面板带「会员档位不足」灰锁，本机两个 CN 账号实测都回
`1005`——名字与倍率照目录收录，能不能调由上游按账号诚实报错。

**「↻ 重新拉取」拉的是真目录**：CN 网关 `get_detail_param` 返回完整候选目录，每条带
`consumption_rate`（就是客户端「模型倍率」面板显示的数值，2026-10-25 与面板截图逐条核对过）、
展示名、上下文窗口与读图能力。刷新时遍历本区全部账号取并集（不剔冷却），**倍率按
`consumption_rate` 抄回**到进程内 `DISCOVERED_CREDITS`（不覆盖人工核过的静态底表，重启
即回底表）；目录里**没有的新名字**会先真发一条最小请求验证（上游收下名字 ≠ 能服务，4001
拒收的不收录），已有模型不再逐条 ping。

目录顺序沿用上游客户端的展示顺序。PAT 通道仍可通过 `traepat/<模型>` 使用，但这里不再列出 PAT 模型表。要新增 / 调整 CodeBuddy 或 Trae PAT 静态模型，编辑 `src/buddy_proxy/web/models_config.json` 后重启生效。

## 管理界面（/ui）

浏览器打开 <http://127.0.0.1:8787/ui>（或 `buddy start` / `buddy ui` 自动打开）：

- **默认启用模型** — 按 provider 分组浏览所有模型，点「设为默认」即可把某个模型设为默认；
  客户端请求**不带 `model` 字段**时自动用它补齐。设置持久化在 `~/.buddy-proxy/settings.json`
  （可用 `BUDDY_PROXY_SETTINGS` 覆盖），重启后仍生效；启动时 `--default-model zcode/glm-5.3`
  可提供初始值（设置文件里已有的值优先）
- **模型搜索** — 模型列表上方有实时搜索框：输入即按「通道 id/名称 或 模型 id/名称」在
  前端筛选（忽略大小写与首尾空白，不发请求）。通道命中保留该组全部模型，模型命中只留
  命中行，无命中的组整体隐藏；搜索中匹配组强制展开、「全部展开/折叠」暂时禁用，清空
  （✕ 或 Esc）后精确恢复原展开/折叠布局——搜索全程不改写 localStorage 里的折叠偏好
  （`bp-collapsed`），30 秒自动刷新也不丢搜索词
- **目录刷新与隐藏** — Qoder、Kimi、Antigravity 这类实现动态目录能力的通道，标题栏显示「↻ 重新拉取」：遍历全部账号（包括因冷却暂时不参与聊天 failover 的账号）取并集并原子更新实例目录。部分账号成功时只新增、不删除上一版 id，因为失败账号可能是某模型的唯一来源；只有全部账号成功才确认下架，若所有账号都失败则保留最后一份好目录。Antigravity 的上游 `fetchAvailableModels` 列表当「脏且缺」的不可信输入处理，而非权威目录：它仍回传已淘汰的旧代（`gemini-2.5-*`、`gemini-3-flash*`、`gemini-3.1-flash*`、`gemini-3.5-*`），由 `models.json` 的 `blocked_prefixes` 在发现时按前缀丢弃（前缀保持粗粒度，仍要用的模型用 `blocked_prefix_exceptions` 按精确基名豁免，目前只有生图模型 `gemini-3.1-flash-image`；真·新模型不在表里，照常收录）；它又不回传 premium curated 模型（`gemini-3.1-pro` / `gemini-3.6-flash`——IDE 可见、能路由，但目录接口不吐），故每个 `verified: true` 条目被钉死、刷新永不下架。模型行的“隐藏”只影响模型页、选择器与 `/v1/models` 两套数组，显式 `provider/model` 仍可调用；与“停用”（直接拒绝调用）严格不同。隐藏键写入 `settings.json` 的 `hidden_models`，上游后来下架也能在「已隐藏」弹窗清理。
- **一键测试** — 每个模型一个「测试」按钮，向上游发一条 `hi`，弹窗里返回延迟、token 用量、回复预览，以及实际响应的 provider/模型（如 `zcode/glm-5.3`；模型顺序路由时也显示最终命中的通道）
  （非流式、`max_tokens=256`，走真实上游、会产生真实调用）
- **自动打卡 & 打卡日历** — CodeBuddy / Trae / Qoder 三家上游都提供每日签到：勾选「自动打卡」后
  代理每天定时（默认 09:30，启动时当天未签会立即补签）自动领取，最近 35 天的打卡情况在日历里
  展示；也可点「立即打卡」手动领。签到活动有档期——CodeBuddy 档期未开时界面显示「今日无签到
  活动」且不会误打。打卡历史逐行落在 `logs/checkin.jsonl`。ZCode（智谱 Coding Plan）没有签到
  API
- **每通道「下次打卡时间」** — 打卡行上显示下次能领的时刻 + 秒级倒计时（纯本地计算，不打上游；
  离开页签或切走标签页即停）。三家的**轮换周期不一样**，所以时刻来源也不同：Qoder 的活动窗口
  实测是「10:00 → 次日 09:59」（上游给 `startAt`/`endAt`，标 `upstream`），CodeBuddy 与 Trae
  是**本地零点**轮换——这两家上游只给整个档期（CodeBuddy）或干脆不给时间字段（Trae），零点这个
  时刻是从 `logs/checkin.jsonl` 的真实领取记录反推的，故标 `inferred`，界面上用虚线框 + 斜体时刻
  表示「这个值是推断的」（早先贴在「分后」后面的那个「≈」读着像错字，已换掉）。
  还没领时 Qoder 显示的是本轮**截止**时刻（错过即失效），已领时才是下一轮开始。算不出下次
  （活动已结束/档期已过/今天无活动）时不显示，而不是显示一个已过期的时刻。倒计时数到「即将
  刷新」后那一次轮询会**立刻**拿到新状态：这个时刻同时用作状态快照的到期条件（不然翻转点若落
  在快照缓存 5 分钟 TTL 之内，界面会继续显示「已签到」、按钮继续禁用，Qoder 那 5 分钟足够丢掉
  一整轮）。以防上游一直回一个很早以前的值把缓存卡死，这条提前失效只在 1 小时宽限窗口内生效
- **「今天已打过」以上游实况为准** — 本地历史按**日历日**去重，而 Qoder 按 10 点窗口轮换，两者
  对不上：凌晨的巡检会把上一轮的 `checked_in` 补记成今天已打，等 10:00 新活动开出、上游明明报
  `claimable`，旧逻辑仍按「今天打过了」整天跳过领取，界面也显示「已签到」+ 按钮禁用。实测
  `logs/checkin.jsonl` 里 Qoder 的两条记录 `message` 恰好都是「今日已领取」——那是
  `checkin_status` 在「上游已签到」时写的文案（`checkin_claim` 那条带「（无需重复领取）」后缀），
  说明两条都走的「上游已签到 → 补记」这条路，**代理自己一次都没领过**；上游那两次报的是
  `checked_in=True`，积分是被别处领走的，所以是「会漏」而非「已漏」。现在只要上游报可领就照领，
  按钮也不会再被错误禁用；对零点轮换的 CodeBuddy / Trae 是无影响的路径
- **签到「强刷」只刷签到** — 签到行上的「强刷」绕过缓存真打上游重查该通道的签到状态，返回后**只用响应里的快照原地重绘签到区**（签到行 / 日历 / 顶部角标），不再顺手把整页重拉一遍。早先它走的是「强制重拉 benefits + 整页 render」，于是点一下会连带：`stats` 也被强刷（概览卡片和图表）、各通道面板全部重建再各拉一次 `accounts`、traepat 再查一次模型负载、dumate 再探一次本地状态——一次点击打出十来个请求（2026-11-01 用户实报）。其余数据交给 30 秒轮询自然更新。另外各通道的账号快照加载器现在**同通道在飞去重**（`acctFetch`）：30 秒轮询与新一次整页重绘叠在一起时只发一发，不再并排重复请求。
- **权益到期告警** — 套餐、资源包、签到积分、加油包这类「领了就有保质期」的权益，到期时间
  现在单独采集并在界面上标出（「· MM-DD 到期」），与「· MM-DD 重置」区分开——**到期是这份额度
  作废，重置是周期回满**，两者早先共用 `reset_ts` 一个字段，Qoder 的 `expiresAt`（到期）在界面上
  就显示成「10-30 重置」，看起来像到期日没被记录。数据上拆成 `expire_ts`（到期）与 `reset_ts`
  （重置）两个字段，判断「快到期」的横幅只认前者，所以 ZCode 的 5 小时窗口、antigravity 的 weekly
  池不会被误报成「快到期了」。有权益将在 **7 天内**到期时，「打卡 & 额度」页顶部会出现一条横幅：
  汇总一行说明有几项、最近一项是哪天，点开列明细（通道 / 名称 / 到期日 / 剩余天数），已过期的
  项标红。筛选规则在后端一处（`benefits._expiring`），前端只渲染：除了 7 天这个时间窗，积分类
  （`unit == "credit"`）还要**剩余大于 300 积分**才提醒——每天签到送的 200 分小包一到 7 天内就
  会刷一排横幅，真正值钱的会员包反而被淹没；非积分类（MiMo 的「还剩几天」、ZCode 的次数、
  antigravity 的千分制）量纲不同、无法与 300 比较，只看天数。Trae 的权益包因此顺带补全了
  `used/total/remaining`：上游 `usage.credits_amount` 是**已用**不是剩余（2026-10-03 用账号总额度
  交叉校验：`Σlimit − Σamount` 与接口自报 remaining 差 0.00，反向假设差 3014），且 `usage` 缺失
  是「真·没花」不是「未知」（有记录的包已用合计恰好等于 `usage_summary.consumed_amount`，
  消耗严格按到期日 FIFO）——这条与 Trae PAT 那个「缺失不能按 0 算」的先例**相反**，两处不是
  一回事，代码注释里都写明了。界面上 27 个权益包全部列出（此前按名字去重 + 只留前 3 条，
  12 份未消费的额度直接看不见）。
- **余额告急告警** — 与到期告警同一条横幅、互补的另一半（2026-10-03）：权益离到期还早、
  但量快烧干的通道，只盯着期告警就要到烧干那天才被发现。阈值**按量纲分流**（同日用户澄清：
  「或不足 8%」是说给不按 Credits 计费的渠道的）：credits 渠道（CodeBuddy / Qoder / Trae）
  **只看绝对值**——合计剩余 `< 300 credits` 才报，剩 950 占比再低也不该半夜把人喊起来；
  不按 Credits 计费的渠道（ZCode / MiMo / antigravity）没有 credits 概念，**只看占比**——
  剩余 `< 8%` 报。加总口径与额度卡标题行一致，界面上看到的「剩 X / Y」就是判定的输入：
  `sum_items` 置真的通道先加总再判定（Qoder 的并存额度），没置的只取第一条（Trae 的权益包
  是总额度的明细，相加双算；CodeBuddy 自己把合计放首条，同样成立）。规则集中在后端
  `benefits._quota_low`，前端只渲染不判规则；恰好卡线（=300 / =8%）不算「不足」，不报。
- **额度查询** — 各通道剩余额度一目了然：CodeBuddy 积分包余额合计 + 各资源包明细（credits）；
  每个专属通道面板只在标题放一个渠道级 ↻（不再每账号重复），一次作废并重查整个 provider，直接采用 POST 返回的新快照。Trae 海外版有独立额度面板和「海外版无每日签到」说明，不提供伪领取按钮；运维性质的 Trae PAT 固定在额度页最后。每张卡的标题行在总量旁带「已用 N%」。CodeBuddy 原先自立一条「积分余额合计」**明细行**
  排在首位，现已去掉（别的通道都没有这种汇总行）——改为声明 `sum_items`，合计由标题行自己算；
  「余额合计」**始终遍历全部包**，明细也不再截断（此前后端只给前 4 条，被砍掉的条目前端
  无从得知、永久看不见；现在展示条数统一交给前端折叠，用户想看能看全）；
  **列表只铺还有剩余的**：已用完（余量 ≤ 0）的条目收进底部「已用完 N 项」区块，默认隐藏、
  展开可见，沉在还在用的条目**之后**（它们是历史，不是当前状态，用虚线分隔 + 降一档亮度）。
  条目再多也不会撑满一屏：整块超过 **208px**（CSS 变量 `--qfold-max`，JS 读同一个值判断要不要
  露按钮）就裁一刀，底部渐隐、下方给「展开全部 N 项 ▾」按钮，点开撑到自然高度、按钮变「收起 ▴」。
  规则由**实际高度**判定而非「超过 N 条」——两栏布局下窄卡片的可用高度与整宽卡片不同，按条数
  定界会裁错。展开状态存在 `QUOTA_FOLD` 而非 DOM 上：整页每 30 秒重建一次 innerHTML，挂 DOM
  里会被下一轮刷掉（用户刚点开、30 秒后自己收回去）。**拿不到余量的条目不算「已用完」**——
  PAT 的失败说明条（`remaining` 是「2/9 个账号查询失败」这类文案）和 `reset_pending` 的
  「用量待确认」都是**未知**而不是「没花」，据 0 收起正好会把最该看见的故障提示藏起来。
  主额度列表、Trae PAT 面板、antigravity 面板**共用同一套**，不会只有 trae 变好看；
  Trae 总额度剩余 + 权益包到期时间；ZCode 的 5 小时 / 月用量窗口与重置时间；MiMo 的
  周额用量 + 套餐有效期。每张卡的标题行由通道给的 `sum_items` 标记决定：置位时把各条目
  **相加**，否则取第一条。**不能一律相加**——各家的条目语义不同：Qoder 的订阅额度 + 加油包
  + 专属积分是**并存的三份额度**，加起来才是账号总量（上游自己的 `totalUsagePercentage`
  就是这么算的）；而 Trae 的权益包是它已报的总额度的**明细**（加了就重复计算），ZCode / MiMo
  的各条量纲都不同（5 小时窗口 vs 月窗口；百分比 vs 天数）。所以「可合计」必须由通道显式声明，
  默认关闭，免得新通道被默默算错。额度数据带
  5 分钟缓存，避免频繁请求上游。Trae PAT 的 standard 池**没有主动查询接口**，用量只能从
  `4031`（额度耗尽）错误体里被动采集；而 4031 只在池已满时才出现，所以超过 `reset_ts` 的
  快照会显示成「已重置 · 用量待确认」，而不是把陈旧的 100% 当现状。
  额度页是管理页里唯一触网的接口，因此查询做了四重收敛：请求上游前先探测网关可达性
  （DNS+TCP 预检，不可达就整轮跳过、直接用各账号缓存），单账号请求超时 6 秒，账号之间并发
  查询（常驻线程池），整轮再兜一个 8 秒上限（超时未回的账号先用缓存顶上，请求仍在后台跑完，
  下轮生效）——避免耗时随账号数线性增长。网关不可达或部分账号查询失败时，页面会明确给出
  「n/m 个账号未取到新数据」的说明条并用警告色区分，而不是一直转圈让人不知道卡在哪；这类
  失败结果只缓存 30 秒（成功结果仍缓存 5 分钟），网络抖动恢复后下一轮就能自愈
- **Trae PAT 账号** — 每张额度卡标题统一为 `PAT #N（profile id）`，该账号的 token / 冷却状态
  直接放在同一标题行右侧（纯本地读取，不触网，不再在卡片尾部集中重复）；「补签 Token」放在
  面板总标题右侧，只补缺失/临期账号。模型负载独立放在账号额度卡之后，以两栏展示（10 分钟缓存），
  按负载从高到低排序、查不到负载的模型沉到最后。
  作为服务账号运维卡，Trae PAT 固定收在额度页最后；Kimi 则紧邻百度搭子上方。
- **账号改名（alias）** — qoder / kimi / antigravity 额度卡与 trae 签到明细行的账号标题旁
  有 **✎** 按钮，弹窗改的是**显示名**（`alias` 字段，只动本地索引显示，凭据与顺位不动），
  `POST /ui/api/{ch}/accounts/rename`（`{id, alias}`，空串=恢复默认名）落盘；名字全链路一致
  （额度卡标题与签到明细同一个名字）。alias 存在账号索引里独立于凭据——重登（upsert 只改
  nickname/name/email）与索引自愈重写都不丢，清空即回退邮箱/官方昵称。
- **账号副标题改版** — 三通道（qoder/kimi/antigravity）账号卡副标题的「token 剩 Xh」已删：
  access token 几小时就自动刷新，那个数字对「还能用多久」毫无参考价值（qoder 还曾把
  毫秒当秒算出 4.97 亿小时）。副标题只留有意义的位：区域 / 冷却（额度/账号/疑似拉黑）/
  缺 project 等。kimi 的额度条目改为上游**绝对积分**优先（`limits[]`/`usage` 的
  limit/used/remaining 直出，`used_ratio` 只作兜底）——早先读 ratio 时「剩 93.31/100」
  其实是百分比伪装成积分。qoder 账号卡标题行下另加**合计行**「剩 N / M · 已用 x%」
  （订阅额度+加油包+专属积分三份并存份额求和，后端 `sum_items` 声明）。
- **模型停用与时段** — 可停用/启用单个 `(provider, model)` 组合（停用后该组合调用直接失败），
  也可给模型限定时段窗口（如 `22:00–08:00`、`12:00–14:00`）。两者都持久化到设置文件
- **模型顺序（候选上游换档）** — 独立的**模型顺序**页签，**`model_order` 里配了几个键就显示几张
  卡片**，不多不少。展开后是交互式的候选上游列表：可增删，可**拖拽 ⠿ 或按 ▲▼** 调整顺序。
  请求从上往下按序尝试，某个渠道在**尚未向客户端输出任何内容**就失败时换下一档，并给失败目标打
  5 分钟冷却标记（反复失败延长至 1 小时），期间自动跳过。**本机 DNS 解析失败是唯一不打标记的
  例外**：那是你机器的毛病、不是某个上游的毛病，一次抖动会让所有候选同时失败，全打上冷却就等于
  把一秒的抖动放大成五分钟的整模型不可用、期间连重试的机会都没有（2026-10-02 实际发生过）。
  这种情况照常换下一档，只是不留标记。不是每种机器级故障都能逐条识别：DNS 事故一小时后同一晚，
  本机代理隧道中断，三个通道在 50 秒内全灭——但回来的是真的 `502 Bad Gateway` 响应体，和上游
  自己的 502 根本分不出来。所以冷却层还盯一个统计特征：**同一模型 60 秒内有 ≥2 个不同通道失败**
  即判为机器级突发——这些目标（含回头缩短先失败的那个）一律只冷却 **30 秒**、且不参与 1 小时
  升级，网络一恢复模型立即可用，不再锁满 5 分钟。只有网络类失败（传输层/5xx）参与判定：
  429 限流是上游自己的状态，两个通道一起限流也照常吃满冷却。已开始流式返回后不再换档（否则上游会
  重复计费）——因此 CodeBuddy 的流式错误不会触发换档，把它排在末位更实用；这类流内错误至少会
  在请求日志里记成失败，而不再算作一次 200。键就是**裸模型名**
  （`deepseek-v4.1-flash`）：含义是「指定这个模型时按这个顺序选通道，默认第一个」，一条即覆盖
  所有发布该名字的通道。目标写成 `provider/model`。清空全部目标即恢复历史行为（纯按模型 id
  路由）。持久化到设置文件的 `model_order`。模型行上的**清冷却**按钮可立即重试被冷却的目标，
  不动顺序
- **设置文件告警** — `load_settings()` 对损坏的设置文件是**故意**静默吞掉的（一个逗号写错不该
  让代理起不来），代价是所有依赖设置的功能会一起悄悄失效、却毫无线索。管理页现在加载时会探测
  该文件，只要「文件存在但解析不出来」就在顶部弹红色条幅，并附上原始 JSON 报错
- **请求日志** — 从 `logs/metrics.jsonl` 与 30 天归档里读取的服务端分页请求日志，可按日期范围 /
  通道 / 模型 / 客户端筛选；筛选条件和日期范围会写入 URL query，刷新或分享链接后可恢复同一视图。
  页面头部还有一张**各模型使用占比**圆环图：扇区按模型名聚合（同一模型跨通道合并，悬浮框
  里看各通道明细），显示次数与占比、错误数，图例行可点击只看某一个模型。它**不复用日志查询
  那条链路**——`query_logs` 每次都要全量扫 JSONL 与归档（实测 30 天约 38 ms），而饼图读的是
  `MetricsCollector` 里一张按 `(日期, 通道, 模型)` 维护的内存计数表：`record()` 时顺手 +1，查询
  只按日期/通道/模型过滤内存（实测 **0.023 ms**，比磁盘扫描快约 1600 倍，30 秒轮询完全不心疼），
  下面日志表的通道/模型筛选也同步作用到饼图上（客户端维度计数表里没有，不参与）。代价是
  启动时整读当前文件 + 30 天归档重建一次（真实行数下约几百毫秒，只计次，不复原 token/耗时；
  按整文件读而不跟 2MB 尾部走，超 2MB 的归档才不会少算前半段）；窗口拉到比最早归档
  还早时副标题会标 `≈`，说明那几天没被计入。
  **积分**列在上游给出单次消耗时显示实扣值（CodeBuddy、Qoder 及任何填了
  `usage` 的通道），否则退回 `≈` 估算：trae 对已实测模型按输入/输出分开的实测单价
  （`MEASURED_CREDIT_RATES`，与官方使用记录对账校准）计价、并按「当前倍率/校准倍率」
  等比缩放（面板调价只改 `MODEL_CREDITS` 即自动跟进），未实测模型仍按倍率粗估；zcode 按 GLM Coding Plan
  官方抵扣系数精算（含缓存命中与时段折扣），两者都没有则显示 `—`。展示保留 2 位小数
  （与 Qoder 官网一致），原始精度仍在 `logs/metrics.jsonl` 里。注意**缓存命中几乎不计费**：
  输入 164k、命中约 99% 缓存的请求只扣 0.12 积分，而输入 12k、零缓存的请求扣 0.31——
  输入量大不代表扣得多。
- **统计图表** — 按 provider/模型维度聚合请求数、错误数、平均耗时、token 用量：
  近 14 天按通道堆叠的柱状图、模型请求 Top 榜、最近 50 条请求明细。
  模型页的耗时列**取最近 3 次**有耗时的实测均值（悬停看逐次数值与 14 天均值对照）——
  14 天均值跟近期体感差得多，看近三次更贴近「现在快不快」。
  多账号通道（qoder / kimi / antigravity / trae / traepat）的明细会在通道名后
  用括号标注实际服务的账号（failover 后是最终成功的那个）。
  每次请求完成追加一行到 `logs/metrics.jsonl`，重启后自动回读恢复历史（保留 30 天）
- **通道健康** — 各 provider 的登录/配置状态一目了然（CodeBuddy 是否登录、zcode key 是否配置等）

管理页里还可以切换**兜底通道**（请求未命中任何模型时的默认路由），设置持久化在同一文件。

页签按需懒加载、需要时并发拉取，各自的骨架屏独立显示——打开管理页不再等待最慢的日志接口
（它每次轮询都要读取并解析整个 JSONL 尾部）。手动刷新会作废在飞的旧请求，因此晚到的旧响应
不可能覆盖更新的结果。

安全约定：`/ui/api/*` 管理接口**仅允许本机（127.0.0.1）访问**；确需从局域网操作管理页时设置
`BUDDY_PROXY_ADMIN_OPEN=1` 放开（自担风险）。`/v1/*` 代理端点不受此限制。

## 快速验证

```bash
curl http://127.0.0.1:8787/health      # 服务 + 认证状态
curl http://127.0.0.1:8787/v1/models    # 模型列表
```

## 接入客户端

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

### 其它 OpenAI 兼容客户端

- Base URL：`http://127.0.0.1:8787/v1`
- API Key：留空（或启动时设置的 `--api-key`）
- Model：`/v1/models` 里的任意 id，如 `glm-5.3`、`deepseek-v4-pro`、`kimi-k2.7`、`auto`

---

## 命令行参数

```
--host HOST               绑定地址（默认 127.0.0.1；proxy.sh 用 0.0.0.0）
--port PORT               绑定端口（默认 8787）
--endpoint ENDPOINT       CodeBuddy 后端地址
--session-file PATH       会话文件（默认 ~/.codebuddy-session.json）
--log-file PATH           JSONL 日志（默认 <项目根>/logs/buddy-proxy.jsonl，可用 BUDDY_PROXY_LOG_FILE 覆盖）
--desensitize             启用脱敏（推荐）
--optimize-context        启用消息压缩（Codex 场景推荐）
--default-model MODEL     默认启用模型（如 zcode/glm-5.3）；请求未带 model 时使用，
                          首次启动写入设置文件，此后以 ~/.buddy-proxy/settings.json 为准
--default-provider NAME   兜底通道：模型名未命中任何 provider 时转发到哪个通道
                          （codebuddy/zcode/trae/doubao/mimo/qoder，默认 codebuddy）
--trae                    启用 Trae provider（解密 Trae IDE 登录态）
--zcode                   启用 ZCode provider（智谱 GLM，Anthropic 端点直通）
--glm                    启用 GLM 官方 provider（BigModel Coding Plan 官方 key，
                          与 zcode 同上游、凭据独立）
--ark                     启用火山方舟 provider（Responses 优先、自动降级 chat；
                          模型表取 ~/.buddy-proxy/ark.json，key 取 ARK_API_KEY /
                          ~/.buddy-proxy/ark_api_key）
--doubao                  启用豆包 provider（经 CDP 驱动桌面 App）
--dumate                  启用百度搭子 provider（DuMate 本地代理，需 App 在运行）
--mimo                    启用 MiMo provider（API key 或复用 MiMo 桌面登录态）
--qoder                   启用 Qoder provider（COSY 签名，千问3.8 / GLM / Kimi）
--antigravity             启用 Antigravity provider（Google Antigravity 免费额度，一个
                          登录通吃 Gemini 3.x / Claude / GPT-OSS；可导入本机 agy 登录态）
--gemini                  启用 Gemini provider（Google OAuth，Code Assist 免费额度；
                          登录态与本机 gemini CLI 互通）
--login                   启动时浏览器登录（会打开浏览器并打印登录链接）
--no-browser              不自动打开浏览器。隐式/后台补认证（如自动打卡轮询）
                          无论如何都不会弹浏览器、也不会打印登录链接，只留一行
                          `[Auth] ...` 日志。真想要登录链接就用 --login
--verbose-llm             输出扩展安全诊断（绝不记录请求/响应体、token 或 UID）
--mock-dir DIR            使用录制的响应（测试用）
```

环境变量：`BUDDY_PROXY_HOST`、`BUDDY_PROXY_PORT`、`CODEBUDDY_ENDPOINT`、`CODEBUDDY_MODEL`、`BUDDY_PROXY_LOG_FILE`、`BUDDY_PROXY_SETTINGS`（设置文件路径）、`BUDDY_PROXY_STATE_DIR`、`BUDDY_PROXY_ADMIN_OPEN=1`（放开管理接口的本机限制）、`PROXY_DEFAULT_PROVIDER`（兜底通道，默认 `codebuddy`）、`TRAE_ENABLED` / `ZCODE_ENABLED` / `ARK_ENABLED` / `DOUBAO_ENABLED` / `MIMO_ENABLED` / `QODER_ENABLED` / `GEMINI_ENABLED`（置 `1` 等同对应开关）、`ARK_API_KEY` / `ARK_BASE_URL` / `ARK_UPSTREAM_PROTOCOL`（`auto` / `responses` / `chat`）、`TRAE_TOKEN` / `TRAE_USER_ID`（跳过 Trae IDE 解密，直接用这两个值）、`ZCODE_API_KEY`、`ZCODE_OPENAI_BASE`、`MIMO_API_KEY` / `MIMO_BASE_URL`、`BUDDY_CLIENT_NAMES_FILE`（覆盖请求日志里的客户端名映射）。

Trae 流式调优：`WB_TRAE_HEARTBEAT_INTERVAL`（等待上游缓冲响应期间的心跳秒数，默认 45，`0` 关闭——避免像 Ethan 那种 120s 分块超时的客户端中断长生成）、`WB_TRAE_NATIVE_TOOLS`（全部 Trae 请求是否走原生通道，默认 `1`，`0` 回落到旧的提示词教文本协议）、`WB_TRAE_IDE_VERSION_CODE`（Trae 客户端版本头，默认 `20260906`——上游按此头放开各模型能力，某模型突然 4001 时调高它）、`WB_TRAE_NONSTREAM_MAX_S`（非流式聚合上限）、`WB_TRAE_SEMANTIC_TIMEOUT`、`WB_TRAE_TOKEN_KEEPALIVE_S`（后台 Token 刷新间隔）。

Trae PAT 频控：部分扩展模型上游按「账号 × 模型」限频（队列已满 / 访问量过大类错误码）。在向客户端吐出任何内容之前，PAT 通道会先换账号；所有账号都限频时返回 429，交给 `model_order` 切到下一个顺序目标（429 目标只冷却 60 秒且不升级）。模型可在 `models_config.json` 声明 `"rate_limit": {"requests": N, "window_s": W}` 做主动节流；所有账号窗口都满时最多等待 `TRAE_PAT_PACE_MAX_WAIT_S` 秒（默认 30，期间照常发心跳），仍无名额则 429。

## API 端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET  | `/ui`                 | 管理界面（`/` 302 跳转到 `/ui`） |
| GET  | `/ui/api/*`           | 管理接口（overview/models/stats/benefits/settings/checkin/test/logs/model-toggle/model-schedule/traepat 账号与模型状态，仅限本机） |
| GET  | `/health`             | 服务 + 认证状态 |
| GET  | `/v1/models`          | 模型列表 |
| POST | `/v1/chat/completions` | OpenAI 对话（工具 + 流式） |
| POST | `/v1/responses`       | Responses API（Codex CLI） |
| POST | `/v1/messages`        | Anthropic Messages（Claude Code） |

端点使用本地 session 认证，无需额外 token。

---

## Provider 接口一览

各 provider 统一注册到 `providers/` 包的抽象层，`forward_chat` 按模型名路由；
模型名未命中任何 provider 时转发到兜底通道（`--default-provider`，默认 codebuddy）。

### 1. CodeBuddy Provider（`codebuddy_provider/`）

| 接口 | 说明 |
| --- | --- |
| `GET /v1/models` | 模型列表（`models_config.json` 本地配置） |
| `POST /v1/chat/completions` | OpenAI 对话（`stream_upstream` 流式 / `collect_upstream` 非流式） |
| `POST /v1/responses` | Codex CLI Responses 协议适配 |
| `POST /v1/messages` | Claude Code Anthropic Messages 协议适配 |
| `forward_chat(body, "openai"/"codex"/"anthropic")` | 按协议转换 + 按模型路由到对应 provider |
| `GET /ui/api/codebuddy/accounts` | 多账号状态（纯本地不触网，不含秘密）；`order`/`rename`/`delete` 管理顺位、别名、删除 |
| session / 多账号 | 隔离的 session 文件，`--session-file` 指定 |

### 2. 豆包 Provider（`doubao_provider.py` + `doubao/cdp_client.py`）

| 接口 | 说明 |
| --- | --- |
| `CDPDoubaoClient.start()` | 确保 CDP：① 复用已有调试口；② `open -a DoubaoWork --args --remote-debugging-port=9223` 让主 App 以调试模式起来（最多等 30s 调试口）；③ 只有前两步都不行、且主 App 已退出时才起独立 Helper（起之前先清理 App 身份启动的残留进程，避免抢单例锁把 App 锁在门外） |
| `CDPDoubaoClient.chat_completion()` | 页面内 fetch `/chat/completion`（自动带 a_bogus 签名），流式 yield SSE；`model_spec` 传入时走 agent 管线（`_build_agent_payload`，App 同款 `model_item_key` 路由），否则经典管线 |
| `DoubaoProvider.forward()` | 流式 / 非流式转换，返回 OpenAI 标准格式 |
| 模型 | 经典：`doubao`（快速）/ `doubao-pro`（旧别名）/ `doubao-think`（深度思考）/ `doubao-expert`（专家）；agent（App 菜单同款）：`doubao-auto` / `doubao-2.1-turbo` / `doubao-2.1-pro` / `orange-5.0` / `gemini-3.7-flash` / `gpt-5.6-sol` |
| 依赖 | 纯 Python 标准库，无 Playwright / chromium |

### 3. Trae Provider（`trae/` 子包）

实现已从单体 `trae_provider.py`（3400+ 行）拆分为 `trae/` 子包，按职责分模块：
`config`（常量/版本头/模型映射）、`auth_storage`（IDE 存储解密）、`benefits_api`（签到/权益）、
`credentials`（凭证与请求头）、`leak_guard`（预设泄漏防护）、`text_protocol`（教学注入/请求改写，
含文本协议解析兜底）、`native_tools`（原生 function calling）、`transport`（HTTP 发送）、
`sse`（SSE 解析/Anthropic 包装）、`provider`（编排入口）、`cli`（`trae-cli` 账号工具）、
`aes_pure`（零依赖 AES 兜底实现）、`pat_provider` + `pat/`（PAT 底层模型通道：多账号、撞码分级冷却、
4031 快速失败）。
`trae_provider.py` 保留为兼容 shim，历史导入与 `python -m buddy_proxy.trae_provider` 不受影响。

| 类别 | 接口 | 说明 |
| --- | --- | --- |
| 认证 | `auth_storage.decrypt_auth_data()` / `find_auth_data()` | 解密 Trae IDE 本地 `storage.json`（AES-128-CBC + SHA-512 派生） |
| 认证 | `credentials._auth()` / `_load_work_cred()` | 凭证加载：`.env`（`TRAE_TOKEN`/`TRAE_USER_ID`）→ Work 凭证 `~/.buddy-proxy/trae_work.json` → 本地解密 |
| 认证 | `auth/trae_work_login.py` / `auth/trae_work_login_server.py` | Work 登录（ExchangeToken 换 token，回调自动捕获） |
| Chat（原生） | `native_tools._send_native_chat()` | **默认通道**：`function=chat_v3` 直通 + 原生 tools（`parameters` 需 JSON 字符串），结构化 `tool_calls` 与 `role:"tool"` 历史回放，带真实 usage；上游按 `X-Ide-Version-Code` 逐模型门控（`WB_TRAE_IDE_VERSION_CODE` 可覆盖） |
| Chat（兜底） | `transport.send_trae_chat()` / `_send_trae_work_chat()` | 文本协议通道（IDE 3 级端点回退 / Work `solo_work_lite`）；原生通道 4001 拒绝时自动回落（`WB_TRAE_NATIVE_TOOLS=0` 可强制全走此路） |
| 解析 | `text_protocol` 内的工具调用解析 / 流式切分器 | 文本协议工具调用解析（教学格式 + 泄漏闸门），仅兜底路径使用 |
| PAT 通道 | `pat_provider` + `pat/`（`cooldown`/`store`/`config`/`models`/`keeper`） | `traepat/<模型>` 底层模型通道：多账号 failover、撞码分级冷却（首次 5 分钟换号，反复撞才升级到次日）、4031 通道级快速失败（`429`，5 分钟后自动重探）、standard 池用量被动采集 |
| 签到 | `benefits_api.fetch_checkin_status()` / `claim_checkin_credits()` | 查询/领取签到积分（`/trae/api/v2/ug/checkin_credits/*`） |
| 权益 | `benefits_api.fetch_ent_usage()` | 查询积分总额 / 已用量 / 权益包 |
| 双区域 | `config.TRAE_REGIONS` / `model_tables(region)` / `work_function_override(region)` | CN/global 两套取址（chat 网关 / UG 域 / 额度版本 v2 vs v1）、两套模型目录与按区分表的 function 绑定（海外 `messages[].content` 需内容块数组，见 `transport._intl_content_blocks`） |
| 海外额度 | `provider._quota_items` dollar 分支 | 只展示 Basic 美元用量（`usage.basic_usage_amount` 真实已用 / `basic_usage_limit`）；`premium_model_fast_request_limit` 是能力位不是额度、不单列；`is_hide` 的 UI 不展示包过滤 |
| 模型 | `config._map_model()` | 模型目录与外部名别名 |
| 错误 | `sse._trae_error_text()` | 14+ 个官方错误码 → 中文文案（4011 今日额度 / 1005 plan 权益不足等） |
| 账号工具 | `cli`（`trae-cli status` / `claim` / `usage` / `chat`） | 命令行查询/领取签到、看权益、发测试对话 |

### 4. ZCode Provider（`providers/zcode.py`）

| 接口 | 说明 |
| --- | --- |
| 凭据 | `ZCODE_API_KEY` → `~/.buddy-proxy/zcode_api_key`（目录可用 `BUDDY_PROXY_STATE_DIR` 整体挪走）→ `~/.zcode/v2/config.json`（ZCode CLI 同款配置） |
| 端点 | 智谱 GLM Coding Plan 的 Anthropic 兼容端点直通；`ZCODE_OPENAI_BASE` 可覆盖 |
| 模型 | 以 `zcode/<id>` 寻址（如 `zcode/glm-5.3`），经 `/v1/models` 一并列出 |
| 登录 | `buddy login zcode`：**没有可自动化的浏览器登录**（凭据是智谱控制台签发的 coding-plan API key，得人工点），所以这条命令负责把「去哪领 key、怎么配」打印清楚 |

领 key：<https://bigmodel.cn/usercenter/proj-mgmt/apikeys>（还没套餐先开通 <https://bigmodel.cn/glm-coding>），
配到本机任选一种：`export ZCODE_API_KEY=<key>` / 写进 `~/.buddy-proxy/zcode_api_key` / 在本机 ZCode CLI 登录
coding-plan，然后 `buddy restart`。

写文件时用 `>` **覆盖**、不要用 `>>` 追加：解析只认第一个非空行，追加会让旧 key 继续生效
（换了 key 却毫无察觉）。目录不存在时先 `mkdir -p ~/.buddy-proxy`。

额度面板读的是 `/api/monitor/usage/quota/limit`。它的 `limits[]` 各条**共用同一个 `type`
（`CREDIT_LIMIT`）**，窗口靠 `unit` + `number` 区分而不是靠重置时间：`unit=3`/`number=5` 是
5 小时档，`unit=6`/`number=1` 是月档。窗口名必须从 `unit`/`number` 算——按「`nextResetTime`
还有多远」去推会**两档全错**：月档重置只在几天后（于是被叫成「每周窗口」），而 5 小时档上游
**压根不给 `nextResetTime`**（于是名字退化成裸的 `CREDIT_LIMIT`）。条目按窗口**由小到大**排，
5 小时档在前；若改回按 `nextResetTime` 排，没有时间戳的 5 小时档会被甩到末位，标题行（取
第一条）就显示了月档而不是更紧迫的那档。另外这两档是**各自独立的额度**，要分开看，不能相加。

**ZCode Start Plan 错误响应**——`zcode-start/glm-5.3-flash` 的成功响应必须是 Anthropic Message（流式首个有效事件为 `message_start`）。上游即使返回 HTTP 200，若实际是额度/错误 JSON 或没有有效首事件，也不再向 Claude Code 伪报 200：额度错误（含 `1308`）转成 HTTP 429，其他无效响应转成 HTTP 502。若配置了模型顺序，未提交的失败可继续换档。OpenAI 兼容流式转换也支持上游以 LF 或 CRLF 分帧的 SSE（包括换行符跨网络块）。

**GLM 官方渠道（`providers/glm.py`，可选）**——`GlmProvider` 是 `ZcodeProvider` 的子类，
上游/直通转发/模型表/额度端点全部继承，唯一差异是凭据链：只认 `GLM_API_KEY` /
`~/.buddy-proxy/glm_api_key`，**绝不读** `~/.zcode`（两条 key 是独立购买的两个套餐，
串读会把 A 套餐的用量算到 B 头上、额度卡也对不上）。注册在 zcode 之后——两通道同时
启用时裸名 `glm-*` 仍先落 zcode，显式 `glm/<模型>` 前缀才定向官方 key。`buddy login glm`
负责打印领 key/配 key 指引（同 zcode：key 得在智谱控制台人工签发，无可自动化的登录）。
套餐权限 2026-10-06 实测（lite 档）：`glm-5.3` / `glm-5.3-flash` / `glm-5-turbo` 可用；
`glm-5.3-flashx` 仍 `1311 套餐暂未开放`，升级套餐后无需改代码即可用（预备接入）。


### 5. MiMo Provider（`mimo/` 子包）

小米 **MiMo**（platform.xiaomimimo.com），以 `mimo/<id>` 寻址（`mimo-auto`、`mimo-pro`）。

| 接口 | 说明 |
| --- | --- |
| 凭据（方式一） | API key：`MIMO_API_KEY`（配 `MIMO_BASE_URL` 可切 billing/token-plan）、`~/.mimocode/auth.json`（MiMo 桌面「API Key」模式写入）、`~/.buddy-proxy/mimo_api_key.json` |
| 凭据（方式二） | **小米 SSO**：`buddy login mimo` 登录，凭据落 `~/.buddy-proxy/mimo_account.json`；没登录过则回退复用本机 MiMo 桌面登录态（读其账号 cookie），复刻桌面端的「两阶段换 `serviceToken`」，过期自动刷新、被拒自动重试一次 |
| 端点 | OpenAI 形态 `/chat/completions` 直通（流式/非流式） |
| **Anthropic（`/v1/messages`）** | 上游无 Anthropic 原生端点，故**响应反向转换**为 Anthropic 事件（`message_start`/`content_block_delta`/`message_stop`，含 `thinking` 与 `tool_use` 块），供 Claude Code 使用 |
| 登录 | `buddy login mimo` —— 打开浏览器用小米账号登录，命令行自动接住结果（同 `buddy login qoder`） |

#### 登录（`buddy login mimo`）

打开浏览器登录小米账号即可，**不用回终端做任何事**，也不用手工复制 cookie：

```bash
buddy login mimo       # 打开浏览器 → 登录 → 命令行自动继续
```

流程与 `buddy login qoder` 同构（device flow 那套）：生成登录链接 → 打开浏览器 →
长轮询等结果 → 落盘 `~/.buddy-proxy/mimo_account.json`（`0600`）。

> **为什么不用本地回调服务**（像 `buddy login trae` 那样）：小米的 `callback` 参数是
> 服务端**带签名**生成的，只认它自己白名单内的域。自建 `http://127.0.0.1:xxxx/cb`
> 会被直接拒——`{"code":10025,"desc":"Callback连接不合法"}`；拿它自己生成的
> `https://account.xiaomi.com/sts` 去试同样被拒（签名每次现算，外部无法伪造）。
> 所以走官方给第三方留的 `longPolling/loginUrl`（**不传 callback**，用 ticket 机制）。

两个踩出来的细节（2026-09 实测）：

- **`loginUrl` 是 API 端点，不是给人看的页面。** 浏览器直接开只会看到一段
  `{"code":70016,"desc":"登录验证失败"}` 的 JSON。真正的登录页（一个 SPA，二维码
  由 JS 渲染）藏在同一个响应的 `location` 字段里（`account.xiaomi.com/fe/service/login?...`），
  CLI 会跟一次跳转把它取出来。
- **必须用浏览器 UA。** 用客户端 UA 请求会直接 302，拿不到带 `location` 的那个响应体。

ticket 有效期 **300 秒**（Qoder 的 device flow 有 10 分钟）。CLI 的轮询截止时间锚定
链接自带的 `expires_in`，而不是拍脑袋的常量——ticket 过期后长轮询**依然挂住不返回任何
错误**，除了自己掐表没有别的信号可依赖。

凭据读取优先级：**登录落盘的文件优先**，没有才回退桌面 cookie 库——这样**新机器上不装
MiMo 桌面也能用**，装了桌面的老机器行为不变。API key 仍然优先于两者（配了 key 就别登录了，
登录了也不生效）。

管理页的额度面板给两行：**周额用量**与**套餐有效期**。注意这俩是**不同周期**——
额度窗口是「以订阅 `startTime` 为锚点的 7 天」，而套餐期限通常是 **30 天**。
上游 `percent` 字段是**剩余**百分比，面板已换算成「已用」。

```bash
buddy start
```

### 6. Qoder Provider（`qoder/` 子包）

阿里 **Qoder** IDE（国际版 qoder.com / 国内版 qoder.com.cn），以 `qoder/<id>` 寻址。

| 接口 | 说明 |
| --- | --- |
| 聊天面 | `/algo/api/v2/service/pro/sse/agent_chat_generation`（官方 IDE 同一个端点，也是**唯一**提供 Qwen3.8 的入口） |
| 签名 | **COSY 签名纯 Python 复刻**（无额外依赖、不打包官方 wasm）：`Authorization: Bearer COSY.<payload>.<sig>` + 必需的 `Cosy-User` 头，body 走 Qoder 私有字母表编码。国际版与国内版**都要签名**，区域只影响**取 token 的方式** |
| 凭据 | `buddy login qoder`（设备码流程 PKCE S256，可选区域）；也支持 `QODER_TOKEN` 等环境变量 |
| 额度 | 管理页额度面板：订阅额度 + 加油包 + 专属资源包（活动赠送，如「Qwen 专属积分」；各自带到期时间）（含套餐等级）；同步网络查询在线程池等待，不阻塞管理 API |
| **Anthropic（`/v1/messages`）** | 上游无 Anthropic 原生端点，故**响应反向转换**为 Anthropic 事件（`message_start`/`content_block_delta`/`message_stop`，推理内容转 `thinking` 块），供 Claude Code 使用 |

**三条上游怪癖**在 `_build_upstream` 里逐条消息归一化（不这么干整个请求会被拒；三条都是
Qoder 专属，公共转换器保持不动）：

- **`developer` role 在反序列化阶段就被拒** → 改写成 `system`，同时摘掉它可能带的
  `tool_calls`：`tool_calls` 只能挂在 `assistant` 上，`system` 带 `tool_calls` 会把后面
  那条 `tool` 回复一起带挂。
- **带 `tool_calls` 的消息，`content` 不能是 `null`**。Anthropic 的纯 tool_use
  回合转出来正是 `content: null`，于是**用过工具**的会话全挂、而裸问一句能成。上游对
  这个的报错文案**误导**——它说 `Messages with role 'tool' must be a response to a
  preceding message with 'tool_calls'`，害人往 tool 配对代码里白查——所以这里改写成 `""`。
  判据是**有没有 `tool_calls`**、不绑 role，且必须排在摘 `tool_calls` **之前**
  （顺序反了条件就永远不成立）；普通 assistant 的 `content: null` 是合法的，
  改成 `""` 反而凭空造一条空回复。

上游带内错误把真因放在 `details` 字段里（顶层 `message` 只有一句 `Error in upstream
response`）；`_describe_upstream_error` 会把它挖出来，并且失败按 Anthropic 的
`{"type":"error","error":{...}}` 形状返回，好让 Claude Code 认出这是终止性错误、而不是
对着同一个请求重试十次。

**模型名对外统一为「小写真实名」**（上游内部代号 `qmodel_38max` 这类名字看不出是什么模型）：

| 对外 id | 上游 key | 备注 |
| --- | --- | --- |
| `qoder/qwen3.8-max` | `qmodel_38max` | 推理 + 读图 |
| `qoder/qwen3.8-flash` | `qfmodel` | 推理 + 读图 |
| `qoder/glm-5.3` / `qoder/glm-5.3-flash` | `gmodel` / `gfmodel` | |
| `qoder/kimi-k3.1-pro` | `kmodel_latest` | |
| `qoder/minimax-m3.1-flash` | `mmodel` | |
| `qoder/deepseek-v4-pro` | `dmodel` | |
| `qoder/auto` | `auto` | 平台路由档位——上游实际**只有这一个档位**；当前也被上游关了（目录 `enable=false`） |

旧模型（Qwen3.7 系列、GLM-5.2、Kimi-K2.8-Preview、Cantus、Sonus、DeepSeek-Flash）
**不列在列表里但仍可点名调用**——列表少一点，反而好找要用的。三种写法都接受：
对外 id、官方显示名（`Qwen3.8-Flash`）、上游内部 key（`qfmodel`）；内部 key 会在
`/v1/models` 里以 `upstream_key` 回显，便于排障对照。

**模型目录是账号级的**——全量账号报 14 个模型，受限账号可能只剩 Qwen3.8 两档（风控；
门是账号级的，且实测会漂移）。因此 `refresh_models` 遍历**全部**账号取并集（「至少一个
账号支持」）；启动时后台预热、之后每 `CACHE_TTL_S` 刷新一次（目录纯内存，没有预热的话
重启后列表会回落到打包的静态兜底表——界面曾因此只剩 2 个 qoder 模型）。个别账号缺个别
模型时，per-model 白名单放在随包的 `qoder/models.json`：条目
`{"id": "glm-5.3", "accounts": ["<uuid>", ...]}` 列出**支持**该模型的账号 id（UUID，
账号卡上可见）；未列出的模型、或 `accounts: "all"`，表示所有账号都支持。转发时**跳过**
不支持所请求模型的账号（不冷却、不算失败——账号没坏，只是没这个权益）；所有账号都不
支持时直接 404 快速失败，不再为不支持组合白付一次慢超时/带内 400。打包的兜底目录按
全量账号实测重建（三方模型恢复、倍率实测修正）；管理页给受限模型打「部分账号」标签
（悬浮显示支持账号），qoder 账号卡也提示受限情况（完整模型名单收进悬浮 tooltip，
免得十来个名字把卡撑高）。改 JSON 需重启（只加载一次，刻意保持静态）。

**Qoder 海外版（`qoderintl`）**：启动 Buddy 时已启用 `--qoder` 且存在 Global 区账号，
就会注册独立通道，管理页有单独的额度卡和签到行；额度与每日活动签到复用 Qoder provider
实现。海外额度卡是只读的，不会误调仅服务 CN 账号的重排/删除接口；账号卡标题用**账号名**
（alias 优先），多账号时分得清谁是谁。若 Buddy 已运行后才登录 Global 账号，需重启
Buddy 才会注册 `qoderintl`。

```bash
buddy start
```

#### 单次积分消耗

Qoder 上游**直接给出实扣积分**，不需要估算：每条 SSE 流末尾的 usage chunk 里除了 token
数，还有 `credits` / `original_credits` / `billable`。代理把它透传出来，管理页**积分**列
直接显示（2 位小数，完整精度仍在 `logs/metrics.jsonl` 里）。

注意这个字段在这里拼作 **`credits`（复数）**，而 CodeBuddy 是 **`credit`（单数）**——
只认一个就会把另一家的数据整条丢掉，这正是 Qoder 此前一直记 `credit: null` 的原因。
`core/metrics._credit_field` 两种都收，另以 `original_credits` 兜底。

**缓存命中几乎不计费**，所以输入列很大也可能只扣一点点。真实账号实测：

| 输入 | 命中缓存 | 未命中 | 输出 | 积分 |
|---:|---:|---:|---:|---:|
| 164,166 | 163,968 | 198 | 525 | 0.124 |
| 12,030 | 0 | 12,030 | 505 | 0.312 |

所以「输入 13 万多、只扣 0.07~0.13」是正常现象而不是 bug：约 99% 的输入命中了缓存。

#### 每日活动权益（签到）

Qoder 有个**「每日领 100 Credits」活动**（桌面端一启动就弹的那个）。它不在聊天面上，
而是走 `{openapi}/sash/api/v1/me/campaigns`；和 `/algo/**` 不同，这个面**不需要 COSY 签名**——
裸 `Authorization: Bearer <dt-token>` 就行（桌面端是在 Electron 主进程里调的，
日志里记作 `requestSource: "native_main"`）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/sash/api/v1/me/campaigns` | 列出活动 + 领取状态 |
| `GET` | `/sash/api/v1/me/campaigns/{id}/reward` | 发奖结果 |
| `POST` | `/sash/api/v1/me/campaigns/{id}/claim` | **领取** |

Qoder 通道声明了 `supports_checkin`，因此会和 CodeBuddy 一起出现在管理页
**「打卡 & 额度」**面板里，并纳入自动打卡。多账号时逐个检查各自活动并显示账号明细，
避免首账号只有 `VIEW_DETAILS` 时把后续账号的签到奖励漏掉；手动领取会按顺位领取第一个可领账号。
若上游只返回 `VIEW_DETAILS` 等不可领取活动，界面会明确显示「有活动，暂无可领签到奖励」，
不会误报「今日无签到活动」或对其发送 claim。

**签到活动按机器指纹定向发放（2026-10-07 实锤，CN 与全球区一致）**：上游会根据
请求头里的 `Cosy-Machine*` 指纹决定要不要把当天的 `CLAIM_BENEFIT` 条目发给你——
签到条目只发给上游「认识」的设备。只带静态 `machine_id` 时，从未被代理领过的账号
（如只在桌面端签到的号）返回的活动列表里没有签到条目，界面误报「有活动，暂无可领
签到奖励」；桌面端同一账号却能看到。代理现在复刻桌面端的做法：调用 App 内置的
`runtime-info` 原生二进制（`/Applications/Qoder.app` / `Qoder CN.app` 的
`Contents/Resources/umid/` 下）生成每账号指纹，带上真实
`Cosy-MachineToken/Code/Type` 再请求，签到条目即正常出现。二进制缺失（没装桌面端）
或生成失败时回退纯静态头——已被代理领过的账号静态指纹也能看到条目（上游已登记），
新号则看不到。路径可用 `QODER_UMID_BIN` / `QODER_UMID_BIN_CN` 环境变量覆盖。
还有一层只读兜底处理进程会话定向：直连 `/sash` 仍没有 `CLAIM_BENEFIT` 时，代理只读
已安装桌面端最新 `main.log` 的末尾 2 MiB，仅接纳 payload `uid` 与账号逐字一致、日志足够
新且当前仍在 `startAt/endAt` 窗口内的签到条目；直连已有签到永远优先，别的 uid、坏行、
未来/过期窗口全部忽略，同 id 的桌面签到只替换直连降级出的 `VIEW_DETAILS` 占位。

三个值得知道的行为（均为 2026-09 真实账号实测）：

- **按天轮换，每天是新的活动 id。** 窗口就是 `10:00 → 次日 09:59`（UTC+8），
  `campaignKey` 每天自增（`act-20260923-556` → `-557`），`campaignId` 每天是新 UUID。
  永远重新拉列表，别把 id 缓存过天。
- **领取是幂等的。** 对已领过的活动再 POST 会返回 `200 {"status":"CLAIMED","replayed":true}`
  ——那是**重放**，不是新发奖（`claimedAt` 是过去那次的时间）。只有 `replayed: false`
  才代表真发了 Credits。
- **别信顶层 `claimable`** 来判断能不能领：它把 `VIEW_DETAILS` 类活动也算进来了。
  真正的判据是**逐条**看 `actionType == "CLAIM_BENEFIT" && claimStatus == "CLAIMABLE"`。

### 7. Gemini Provider（`gemini/` 子包）

Google **Gemini CLI** 的免费额度（Code Assist individuals），挂 `gemini/` 前缀：

```bash
buddy login gemini   # Google OAuth（PKCE + 本地回调）
buddy start
```

网关走的是与真实 gemini CLI 相同的 `v1internal:generateContent` 端点，请求指纹
（UA / `x-goog-api-client` / 不发 safetySettings）逐项对齐本机真 CLI 0.33.1——
对齐表与防封号注意事项见 `src/buddy_proxy/gemini/README.md`。

**缓存命中会透出**——上游 `usageMetadata.cachedContentTokenCount` 映射为 OpenAI 的
`prompt_tokens_details.cached_tokens`（`prompt_tokens` 口径已含缓存命中，这里不扣减）。
Anthropic 协议出口会把它转成 `cache_read_input_tokens` 并从 `input_tokens` 里扣除，
请求日志的缓存列随之有数。gemini 与 antigravity 两条通道共用这套转换，一起生效。

**登录与本机 gemini CLI 双向互通**（两边是同一个 OAuth client，凭证互认）：

- `buddy login gemini` 成功后，凭证按 CLI 的格式回写 `~/.gemini/`
  （`oauth_creds.json` 合并写 0600、`settings.json` 补
  `security.auth.selectedType=oauth-personal`、`google_accounts.json`
  记 active 邮箱）——写完 `gemini` 命令直接有登录态，不用再登一次。
- 反过来，`~/.gemini/oauth_creds.json` 已有可用登录态时，`buddy login gemini`
  会先问「直接使用它吗？」（默认 yes，跳过浏览器授权）；access token 过期
  会用同一 client 自动刷新，onboarding 自动补跑。

模型（免费层社区实测：flash 系约 250 请求/天、2.5-pro 约 100 请求/天，超了上游
429 原样透传）：`gemini/gemini-2.5-flash` / `-pro` / `-flash-lite`、
`gemini-3-pro-preview` / `gemini-3-flash-preview`。模型表在
`src/buddy_proxy/gemini/models.json`（`verified` 跑通后手工置 true）。

免费层的 prompt 会被 Google 审查用于训练（onboarding 响应里明说）——敏感内容
别走这条通道。

> **注意（2026-10）：** Google 已于 2026-06-18 产品性下线 Gemini CLI 免费个人层
> （onboarding 直接 `UNSUPPORTED_CLIENT`；官方 CLI 0.33.1/0.62.0 实测同样）。
> 免费个人账号请改用下面的 Antigravity 通道；本 gemini 通道继续适用于
> standard-tier（付费 / 绑了 `GOOGLE_CLOUD_PROJECT`）的场景。

> `--gemini` 只在想用这条通道时才需要；不加则 provider 不注册，`gemini/...`
> 模型名落到兜底通道。

### 8. Antigravity Provider（`antigravity/` 子包）

Google **Antigravity** 的额度（Gemini CLI 免费层的官方继任者；个人免费层与
Google AI Pro 订阅层都走这里），挂 `antigravity/` 前缀。一次 OAuth 登录同时
解锁 **Gemini 3.x、Claude Sonnet/Opus 和 GPT-OSS**；配额是两个独立池
（Gemini 组 / Claude+GPT 组），组内各模型共享 weekly + 5h 双池：

```bash
buddy login antigravity   # Google OAuth（PKCE + 本地回调）；检测到本机 agy 登录态可直接导入
buddy start
```

**登录支持从本机 `agy`（官方 Antigravity CLI）导入**——agy 把 OAuth token 存
系统 keyring（macOS `security find-generic-password -s gemini -a antigravity`），
`buddy login antigravity` 检测到会先问「直接使用它吗？」（默认 yes，跳过浏览器
授权；access token 过期自动用同一 OAuth client 刷新，onboarding 自动补跑）。
导入是**只读**的——agy 没有明文配置文件可回写，刷新后的新 token 只存我们自己的
`~/.buddy-proxy/antigravity_oauth.json`。

网关走 `cloudcode-pa.googleapis.com` 的 `/v1internal:streamGenerateContent`
端点（daily 端点优先、prod 兜底），请求指纹按 Antigravity 客户端逐项对齐
（`X-Client-Name`、身份 systemInstruction、envelope 形态——社区验证过的形态，
详见 `src/buddy_proxy/antigravity/README.md`）。上游只认
`fetchAvailableModels` 列表里的变体名（gemini 3 系裸名会被 429 伪装拒绝），
所以 `reasoning_effort` 按模型表声明的档位映射后缀（`models.json` 的
`efforts`/`default_effort`——如 `gemini-3.1-pro` 默认 `-low`、
`gemini-3.8-flash` 实发 `gemini-3.8-flash-tiered`、`gpt-oss-120b` 实发
`gpt-oss-120b-medium`）。模型页手动刷新会遍历所有可用账号、取
`fetchAvailableModels` 并集，只裁掉已知 effort 后缀（绝不泛化裁掉 `-thinking` /
`-image` / `-agent` 这类模型身份），已知条目保留人工 metadata，新发现项立即可路由。
Kimi 的同一刷新按钮走官方 `GET /v1/models`；两者目录都归 provider 实例持有，整轮失败
保留旧快照，不污染其它实例。

**thoughtSignature 穿透协议转换**——gemini 系在每个 `functionCall` part 上
返回 `thoughtSignature`，多轮工具调用时上游强制要求原样带回（缺失 → `400
Function call is missing a thought_signature`）；claude/gpt-oss 系则要求
`functionCall.id` 与 `functionResponse.id` 成对回传（缺 → 400）。签名只在
OpenAI 协议的扩展字段里有容身之处，Anthropic 客户端（Claude Code 等）转换
时会把未知字段丢掉——所以走两条协议都忠实回传的通道：tool call id。响应
方向自造唯一 id 并把签名+函数名记进进程内 LRU（`gemini/thought_signature.py`），
请求方向凭 id 还原签名、把 `fc.id`/`fr.id` 配对，缓存未命中（如网关重启后）
时回落到上游接受（实测 200）的哨兵值 `skip_thought_signature_validator`。
三种行为均于 2026-10-03 对真实上游实证（矩阵见模块 docstring）。

模型（真实账号实测通过；表在 `src/buddy_proxy/antigravity/models.json`）：
`antigravity/gemini-3.1-pro`（默认 `-low`，可显式 `-high`）、
`gemini-3.6-flash`（默认 `-medium`，可 `-low`/`-high`）、`gemini-3.8-flash`
（tiered 自动档）、`gemini-3.1-flash-image`（生图：走 `chat/completions`，图片在
`choices[0].message.images[]`；Anthropic 出口转成 `image` content block）、
`claude-sonnet-4-6`、`claude-opus-4-6-thinking`、`gpt-oss-120b`。

管理面板配额区在 `fetchAvailableModels` 可达时按组显示真进度条（组内取最紧
水位）：剩余量用千分制展示（如 `989.9 / 1000`，小数看着直观）并带下次重置
时间；拿不到（未登录/接口失败）退化为静态说明。

**多账号 + 自动 failover**——换一个 Google 账号再跑一次
`buddy login antigravity` 即追加为备用账号（相同邮箱登录 = 更新该账号凭据、
顺位不变）。账号按 priority 主备降级（初始为登录顺序，**管理页可调**：
Antigravity 面板每张账号卡片悬停出 ▲▼（多账号时），点按即提交完整的
账号 id 顺序到 `POST /ui/api/antigravity/accounts/order`，priority 重写为
0..n-1——列表不全/重复一律 400，added_at 保留不动；quota 缓存键带
priority，重排后旧额度快照自动失效换新顺位；界面随即换位——写后刷新会等
在飞请求落定再补发真重取，不被写前的旧响应顶回旧顺序）。每张账号卡右上角
还有 **↻ 刷新**（kimi 面板同款）：`POST /ui/api/benefits/refresh` 作废本通道
额度缓存立即重查（`benefits.invalidate_quota`，按键前缀删、不波及他通道与
签到状态），不用等 5 分钟 TTL。卡片标题行的 **✎ 改名**（四通道同款：
qoder/kimi/antigravity + trae 签到明细行）弹窗改**显示名**（`alias` 字段，
`POST /ui/api/{qoder|kimi|antigravity|trae}/accounts/rename`）：只改管理页
显示，凭据与顺位都不动；清空提交即恢复默认名（邮箱/官方昵称）。alias 是
账号索引里的独立字段：重登（upsert 只碰 nickname/name/email 具名键）与
索引自愈重写（`kept.append(AccountRef(...))` 是唯一从 entry 重建字段的点）
都不会丢；显示名全链路同一套（额度卡标题、trae 签到明细行都走
alias > name/email/nickname > id）。主账号撞 429（额度）/ 403 /
凭据失效时进内存冷却（429 尊重 `Retry-After`，默认 5 分钟；普通 403/凭据问题
60 秒；**403「Verify your account to continue.」= Google 风控拉黑**——账号
能登录但上游一律拒，按 6 小时档冷却，面板副标题标注「疑似拉黑」）并
自动换下一个账号重放请求；业务 4xx（模型名写错等）原样透传不换号。冷却只放
内存不落盘，重启即清零（每账号重新探测一次）。全部账号都在冷却中时转发直接
429 快速失败；全部账号都试败时报错里逐账号说明当前状态（谁在额度冷却剩几分钟、
谁疑似被拉黑），不再只有一句「最后错误 HTTP xxx」。**上游超时分级**（实测
依据：成功请求首字节 p50 4.8s / p90 13s / p99 57s，非流式成功最长 16s，而
旧的 600s read 让挂死调用白等 10 分钟）：流式 90s（相邻两次读的上限，首事件
前卡死与流中卡死都按它断）、非流式 120s（= 总时长上限）、连接 15s。超时与
网络错误不再打断 failover（过去直接 504/502 穿透整个账号循环）：当前账号
进 60s 短冷却换下一个重试（机器级断网时全账号一起短冷却 = 通道级快速失败，
到点自动重探）；干净 EOF 仍不冷却。failover 另有 180s **尝试期预算**——前面
账号把时间烧完后不再开新尝试（已提交的流不受限，流中卡死由 read 超时管）。
流式请求绝不重复计费：
首个上游事件先压住，语义内容开始前的
带内 429/403 错误会换号重放；语义事件一旦出现就绝不重放。闸门对响应流只开
一次迭代器（httpx 流一次性消费，重开会抛 StreamConsumed 静默丢光剩余事件，
claude 流式曾因此完全空流），缓冲行补放与换号前排空都续跑这同一个迭代器。

凭据存放在 `~/.buddy-proxy/antigravity/`（`index.json` + 每账号一份 0600
JSON，上限 8 个）。旧的单账号文件 `~/.buddy-proxy/antigravity_oauth.json`
首次访问自动迁移为账号 #1（旧文件保留作备份）。删除账号走管理页卡片上的
✕ 按钮（`POST /ui/api/antigravity/accounts/delete`，弹窗确认——不走系统
`confirm()`，与站内弹窗组件同款）：索引
条目、cred 文件与冷却标记一并移除，比手删 JSON 文件干净（手删也会被索引
自愈兜住）。管理页有独立的 Antigravity 面板（Trae PAT 同款布局）：
每账号一块（标题=账号名——✎ 改过的别名优先、否则邮箱，副标题=冷却 /
疑似拉黑 / 缺 project 状态），
下面是它自己的各组进度条；登录新账号后 quota 快照随即失效重查（缓存键带
账号指纹），不会顶着旧单账号数据满 TTL。

> `--antigravity` 只在想用这条通道时才需要；不加则 provider 不注册，
> `antigravity/...` 模型名落到兜底通道。

### 10. 火山方舟 Provider（`providers/ark.py`）

`ArkProvider` 只做三件事：**读本地配置 → 选上游端点 → 按客户端协议翻译**。它**不带任何
默认模型表、不含任何 key**——方舟按自定义推理接入点（`ep-...`）计费，接入点 id 是账号私有
资源，写进代码既越权又没法维护。

| 接口 | 说明 |
| --- | --- |
| 配置 | 模型表：`~/.buddy-proxy/ark.json`（`{"base_url"?, "models":[{"id","endpoint"\|"model",...}]}`）；key：`ARK_API_KEY` > `~/.buddy-proxy/ark_api_key`（首行裸 key，0600）；base_url：`ARK_BASE_URL` > 文件 > 北京区默认值 |
| 配置容错 | 文件缺失/JSON 坏掉/条目非法（缺 `id`、缺接入点、`id` 重复）一律**跳过而不抛**，模型表可能为空；可操作提示留给 `ensure_auth()`（401 + 指向这两个文件的文案） |
| 上游选择 | `ARK_UPSTREAM_PROTOCOL=auto`（默认）先打 `/responses`，只有 `404 + InvalidEndpointOrModel*` 才降级 `/chat/completions` 并在进程内记忆；`responses` / `chat` 可强制。400/401/403/429 **不降级** |
| 请求 | 一律 `stream: true`；chat 上游自动补 `stream_options.include_usage`（不开就拿不到 usage）；responses 上游经 `codex.projection.chat_to_responses_request` 投影 |
| 响应（`openai`） | 上游 chat → 原样透传；上游 responses → `ResponsesToChatStream` 折叠成 chat chunk。非流式输出 `chat.completion` |
| 响应（`responses`） | 上游 responses → 原样透传；上游 chat → `ResponsesStreamConverter` 反向转换。非流式输出 `response` 对象 |
| 响应（`anthropic`） | 复用 `protocols.anthropic_adapter.sse_to_anthropic_stream`（与 dumate 同一份事件机：thinking / tool_use / usage 扣缓存），非流式回整条 Message |
| 首事件闸门 | `_iter_upstream_events`（解析路径）与 `_pass_through_stream` → `_gate_inband_error`（透传路径）**各一道**；上游 200 带内报错还原成 `HTTPException` |
| 错误透传 | 上游 ≥400 先 `aread()` 再 `aclose()`（否则拿不到真实错误体），状态码与错误体原样回客户端；网络异常 → 502，超时 → 504 |
| 对外模型名 | 恒为 `ark/<id>`（`message_start.model`、`response.model` 都是它）；`ep-...` 与接入点背后的平台模型真名都不外泄 |
| 额度 | `quota()` 恒 `None`——无公开余额接口，不出卡、不编数字 |

配置示例（**占位符**，真实接入点与 key 只写在本地）：

```json
{
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "models": [
    {"id": "ds-flash", "endpoint": "ep-xxxxxxxxxxxxxxxx", "name": "DeepSeek Flash"}
  ]
}
```

> `--ark` 只在想用这条通道时才需要；不加则 provider 不注册，`ark/...` 模型名落到兜底通道。

## 免责声明

本项目仅供学习与研究使用，请遵守 CodeBuddy 的服务条款，使用风险自负。
