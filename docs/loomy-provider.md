# Loomy 通道调研与实现记录

> 实测日期 2026-11-29，Loomy 0.9.39，生产网关 `loomyad.xunfei.cn`。
> 本文记录**逆向结论**与**实现取舍**，代码见 `src/buddy_proxy/loomy/`。

## 1. 它是什么

科大讯飞 2026-03 发布的桌面 Agent（Electron）。内核是 **OpenCode**
（`@opencode-ai/sdk` + `@ai-sdk/openai-compatible`），模型调用走标准 OpenAI
兼容协议。主进程代码在 `app.asar` 里完全可读（只有 `.env.prod` 做了
AES-GCM 混淆，口令内嵌在 `electron/utils/env-file-crypto.js`）。

## 2. 模型网关

```
baseURL  = https://loomyad.xunfei.cn/api/v1
/models  = GET  {baseURL}/models
chat     = POST {baseURL}/chat/completions
生图      = POST {baseURL}/images/generations
识图      = POST {baseURL}/chat/completions（多模态）
embedding= POST {baseURL}/embeddings
```

### 一个必须记住的坑：两套 baseURL

安装包 `.env.prod`（解密后）写的是**测试环境**：

```
LOOMY_POINTS_BASE_URL=https://ossptest.voicecloud.cn/loomy/integration
```

而正式版**运行期**用的是生产环境，值在
`~/.config/loomy-opencode/opencode.json` 的
`provider.imodel.options.baseURL`：

```
https://loomyad.xunfei.cn/api/v1
```

拿测试地址去验生产签发的 session，会恒回 `100002 登录已失效`——**很容易被
误判成「登录过期」**（本次调研就踩了）。所以 `credentials.resolve_base_url()`
的优先级是：`LOOMY_BASE_URL` 环境变量 → 运行期配置 → 生产兜底常量。

### 鉴权：两个端点认的头不一样

| 端点 | `token:` | `Authorization: Bearer` |
|---|---|---|
| `POST /chat/completions` | ✅ | ✅ |
| `GET /models` | ✅ | ❌ 回「缺少 token」 |
| `GET /api/v1/points/*` | ✅ | ❌ 回「缺少 token」 |

统一都带 `token` 是最省心的写法（chat 再额外带 Bearer）。

**登录态失效是 HTTP 200 + 业务码**：

```json
{"code":"100002","desc":"登录已失效，请重新登录","trace_id":"...","data":{}}
```

只看状态码会把过期当成功，然后把空 JSON 交给客户端。

### 登录态

```
macOS:   ~/Library/Application Support/loomy/auth-session.json
Windows: %APPDATA%/loomy/auth-session.json

{"session":"<32 hex>","userid":"...","phone":"...","updatedAt":1795887162698}
```

有效期 **14 天**（登录接口 `expire = 14*24*3600`）。本通道**不做自动登录**：
那需要复刻讯飞账号的短信/微信/密码三条链路（含 `generateRequestHeaders`
签名 + RSA 加密），而 14 天手动续一次的成本远低于维护它；且登录授权本就该
发生在官方客户端里。失效时提示「去 Loomy 桌面端重新登录」。

## 3. 模型目录（14 个，实测）

`/models` 是**两层 data**（网关 envelope 套 OpenAI 的 `data` 数组）：

```json
{"data": {"data": [ {...}, ... ]}}
```

单个模型的关键字段：

```json
{"id":"spark-x2.6",
 "name":"Spark-X2.6 正式版(x2.5)",
 "type":"chat",                    // chat / image
 "protocol":"openai_chat",         // 实测全部 openai_chat
 "context_length":1048576,
 "max_output_tokens":131072,
 "is_default_model":true,
 "reasoning_efforts":["none","low","medium","high","xhigh"],
 "default_reasoning_effort":"low",
 "capabilities":{"reasoning":true,"vision":false,
                 "function_calling":true,"streaming":true,
                 "input_modalities":["text"]}}
```

| id | 显示名 | 倍率 | context |
|---|---|---|---|
| `spark-x2.6` | Spark-X2.6 正式版 | x2.5 | 1,048,576 |
| `DeepSeek-V4.1-Flash` | DeepSeek V4.1 Flash | x2.0 | 1,048,576 |
| `deepseek-v4-flash-1017` | DeepSeek V4.1 Flash 1017 | x2.0 | 1,048,576 |
| `Hy4-Flash` | Hy4 Flash | x0.5 | 1,000,000 |
| `GLM-5.3-Flash` | GLM 5.3 Flash | x0.8 | 1,048,576 |
| `qwen3.8-flash` | qwen 3.8 flash | x0.8 | 1,000,000 |
| `Hy-Image-3.5-preview` | Hy image 3.5 preview | — | image |
| `doubao-seedream-5-lite/5.5` | Doubao seedream | — | image |
| `qwen-image-3.0-pro` | qwen image 3.0 pro | — | image |
| `seedance-2-0-fast/2-0/2-5` | Seedance | — | video |
| `auto` | 自动 | — | 服务端选（实测选了 Hy4-Flash） |

### 倍率只能从显示名里正则抽

上游**没有**独立倍率字段（`multiplier` / `ratio` / `credit_rate` 全试过）。
倍率只以 `(x2.5)` / `（x0.8）` 中英括号混排的形式嵌在 `name` 末尾。所以
`catalog.parse_multiplier()` 用正则抽，抽不到返回 `None`——**不默认成 1.0**，
否则「未知倍率」和「倍率恰好是 1」在 UI 上无法区分。

## 4. 计费：积分

真实扣分在 `usage.points_consumed`（服务端算）：

```json
{"prompt_tokens":17,"completion_tokens":20,"total_tokens":37,
 "prompt_tokens_details":{},
 "completion_tokens_details":{"reasoning_tokens":20},
 "points_consumed":1}
```

实测规律：**单次最低扣 1 分**，与 prompt 长度基本无关（20 → 2013 prompt
token 都是 1 分）。

余额与账单：

```
GET {origin}/api/v1/points/balance
    -> {"balance":5500,"dailyBalance":4969,"availableBalance":10469}
GET {origin}/api/v1/points/records?pageNo=1&pageSize=10
    -> data.list[] 含 modelName / pointsActual / balanceBefore / balanceAfter
```

三个余额是**不同口径**：`balance` 套餐总额度、`dailyBalance` 每日额度、
`availableBalance` 可用合计。相加会重复计算，所以额度卡不置 `sum_items`。

`normalize_usage` 原本只认 `credit` / `credits` / `original_credits`，Loomy 的
`points_consumed` 名字不同 → 已加进识别列表，否则用量日志的 credit 列恒为
`null`，面板上只剩 token 没有花费。

## 5. 「签到」：其实是 pet-work

Loomy **没有**传统的每日点击签到（20 多个积分端点全翻过，前端 dist 里也搜
不到「签到」二字）。替代品是 **pet-work（宠物打工）**，按当天真实使用量发分：

```
GET  {origin}/api/v1/pet-work          -> 今日进度快照
POST {origin}/api/v1/pet-work/rewards  -> 申请已达成的档位
```

### 档位分数是本地算的，不是积分

这是最容易搞错的一点。分数由客户端
`electron/pet/pet-work-service.js` 的 `calculatePetWorkMetrics` 算：

```
sessionIndex = min(activeSessionCount / sessionBaseline, maxSessionIndex)
depthIndex   = min(extraValidTurnCount / depthBaseline,   maxDepthIndex)
score        = round((sessionIndex * sessionWeight + depthIndex * depthWeight) * 100)
```

权重 `sessionWeight=0.4` / `depthWeight=0.6`，`maxSessionIndex=maxDepthIndex=2`
→ 分数范围 0–200。档位 `tier_1=60` / `tier_2=100` / `tier_3=150` 比的就是
这个**分数**，而 `earnedPointsToday` 是**积分**（另一回事，每日上限 1000）。

服务端 `/pet-work` 快照只回积分，不回分数。所以实现里分数从客户端本地状态
`~/Library/Application Support/loomy/pet-work.json` 的
`petWork.users[<userKey>].daily.petWorkScore` 读，且必须校验
`businessDate` 与服务端一致（否则是昨天的分数，不能领今天的奖）。

读不到分数时 `score_source='none'`，`highest_unclaimed_tier()` 返回 `None`，
**不发请求**——宁可今天少领一次，也不拿错的分数去撞服务端校验。

## 6. 实现取舍

| 决策 | 原因 |
|---|---|
| 只实现 OpenAI 协议，Anthropic 由路由层转 | 实测目录里全部 `openai_chat`；`/v1/messages` 直连上游回 404 |
| `max_tokens` 兜底 8192（仅调用方没给时） | 思考链会吃光预算：`max_tokens=20` 时 GLM-5.3-Flash 的 `content` 是空串、`finish_reason=length` |
| 不指望 `reasoning_effort: none` 关思考 | 实测传了之后 `reasoning_tokens` 仍有 75，它只让上游少想一点 |
| 目录空列表不覆盖旧缓存 | 上游灰度/限流偶发返回空表，覆盖会让目录整个消失 |
| 登录态失效映射成 401 + 可操作提示 | 裸异常对用户没用；要告诉「去桌面端重新登录」 |

## 7. 验证方式

```bash
# 单测（完全离线，36 条）
.venv/bin/python -m pytest tests/test_loomy_provider.py -q

# 真实链路（需 Loomy 已登录）
.venv/bin/python -m buddy_proxy --loomy --port 8791 --default-provider loomy
curl -s localhost:8791/v1/models | jq '.data[] | select(.providers|index("loomy"))'
curl -s localhost:8791/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"loomy/GLM-5.3-Flash","messages":[{"role":"user","content":"hi"}]}'
curl -s localhost:8791/v1/messages -H 'Content-Type: application/json' \
  -d '{"model":"loomy/GLM-5.3-Flash","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'
```

实测已通过：单轮、多轮、工具调用（`finish_reason=tool_calls`）、流式 SSE、
Anthropic `/v1/messages`（含 `thinking` 块映射）、积分落 metrics（`credit=1.0`）。
