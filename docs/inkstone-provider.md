# 书生·端砚（inkstone）通道调研与实现记录

> 实测日期 2026-11-29，书生·端砚 0.2.13（bundle id `cn.internai.inkstone`），
> 生产网关 `discovery.intern-ai.org.cn`。
> 本文记录**逆向结论**与**实现取舍**，代码见 `src/buddy_proxy/inkstone/`。

## 1. 它是什么

上海人工智能实验室（Intern AI / OpenXLab）2026 年发布的桌面 Agent，Electron
应用。和 Loomy 不同，它的**模型调用协议是自研的**，不是 OpenAI 兼容——这是本
通道比 Loomy 复杂一倍的根本原因。

主进程代码在 `app.asar` 里可读（74MB，用 `@electron/asar` 可解包）。

## 2. 三个网关前缀（别混）

```
推理/会话/目录  https://discovery.intern-ai.org.cn/api/orbit/v1
套餐额度        https://discovery.intern-ai.org.cn/api/tokenplan/v1
SSO             https://sso.openxlab.org.cn
```

配置来自 asar 里的 `MAIN_VITE_*` 常量：

```json
{"MAIN_VITE_API_BASE_URL":       "https://discovery.intern-ai.org.cn/api",
 "MAIN_VITE_CORE_API_BASE_URL":  "https://discovery.intern-ai.org.cn/api/orbit/v1",
 "MAIN_VITE_AUTH_API_BASE_URL":  "https://discovery.intern-ai.org.cn/api/user-center/v1",
 "MAIN_VITE_SCPHUB_API_BASE_URL":"https://discovery.intern-ai.org.cn/api/moce/v1"}
```

**踩过的坑**：额度接口不在 `/api/orbit/v1` 下。按 orbit 前缀拼
`/tokenplan/v1/credits/balance` 一律 `404 page not found`——那是 nginx 层没这个
路由，不是路径写错，光看 404 很容易以为接口改名了。

## 3. 登录态：Electron safeStorage（不是明文）

Loomy 把 session 明文写 `auth-session.json`；端砚走 Electron 的
`safeStorage`：

```
~/Library/Application Support/inkstone-desktop/credentials/credential_<uuid>.bin
```

文件是 macOS 的 `v10` 格式：3 字节 magic + AES-128-CBC 密文。口令存在系统
钥匙串条目 **`书生·端砚 Safe Storage`**，用 `security find-generic-password -s
<name> -w` 取。派生方式与 Chromium 一致：

```python
key = PBKDF2-HMAC-SHA1(password, b"saltysalt", 1003, 16)
plain = AES-128-CBC(key, iv=b" " * 16).decrypt(blob[3:])
```

解出来：

```json
{"version":1,
 "credential":{"token":"eyJ0eXBlIjoiSldUIiwiYWxnIjoiSFM1MTIifQ...",
               "environment":"production",
               "expiresAtMs":1797100526000,
               "user":{"ssoUid":"24000318","username":"...","nickname":"..."}}}
```

`token` 是 JWT（`iss=OpenXLab`，`rol=ROLE_REGISTER`，`jti` 就是 `ssoUid`），
**有效期 14 天**（`exp - iat == 1209600`）。

**为什么不自动登录**：SSO 是微信扫码/手机号，要复刻 OpenXLab OAuth + 客户端
签名，而 token 14 天才续一次。重开端砚一次的成本远低于维护登录链路，且授权本
就该发生在官方客户端里（与 Loomy 通道同口径）。

## 4. 模型目录（12 个，实测）

```
GET {base}/local-agent/models?supports_output_resource_limits=true
```

注意是**网关 envelope**（`code`/`data`），不是 OpenAI 的 `{"data":[...]}`。

| id | canonical_id | 上下文 | 输出 | tags |
|---|---|---|---|---|
| Auto | sage-s2.1-flash | 524288 | 131072 | 默认配置 |
| 书生-Agents-A1 | agents-a1 | 262144 | 16384 | 深度检索 |
| 书生-A1.5 | sage-a1.5 | 256000 | 16384 | 工具调用、科学推理 |
| 书生-S2 | intern-s2 | 256000 | 32768 | 科学专精 |
| 书生-Atria-Dawn | atria-dawn | 512000 | 32768 | 长程任务 |
| Qwen3.7-Plus | qwen3.7-plus | 1000000 | 16384 | 工具调用 |
| Qwen3.8-Flash | qwen3.8-flash | 1000000 | 16384 | 多模态 |
| Deepseek-v4.1-Flash-1017 | deepseek-v4.1-flash-1017 | 1000000 | 65536 | 极速推理 |
| GLM-5.2 | glm-5.2 | 1000000 | 32768 | 编程专精、长程任务 |
| GLM-5.3 | glm-5.3 | 1000000 | 131072 | 复杂任务 |
| Kimi-K2.7-Code | kimi-k2.7-code | 262144 | 16384 | 编程专精、长上下文编程 |
| 书生-S2.1-Flash | sage-s2.1-flash | 524288 | 131072 | 科学专精 |

`id` 和 `canonical_id` **两个都能当 model 传**（实测都 200），这里把
`canonical_id` 当别名放进 `/v1/models` 的 `aliases`。

`answer_transport` 有三个取值（`tool_arguments` / `native_terminal_content` /
`strict_eof`），但上游在 SSE 里已经把三种都归一成 `content_delta` 了，所以这个
字段只做展示/排查用，不改变转发行为。

## 5. 推理协议：三段式，只支持流式

```
POST {base}/chat/new                      建会话 -> data.id
POST {base}/chat/{id}/local-turns         注册 turn（每次推理一个）
POST {base}/local-agent/messages          发推理（**只支持 SSE**）
```

### 请求体是严格 schema

少一个字段就 422，而且**只报第一个缺的**——所以下面这份字段表是逐轮试出来的：

```json
{"protocol_version":"2.0",        // 必填，字面量
 "chat_id":2754185,               // 必填
 "turn_id":"<uuid4>",             // 必填，**小写规范 UUID**
 "request_id":"<uuid4>",          // 必填
 "inference_kind":"turn",         // 必填，turn | detached
 "model":"书生-S2.1-Flash",       // 必填
 "thinking":{"enabled":false},    // 必填，**对象**
 "system":["你是一个助手"],        // 必填，**字符串数组**
 "tools":[],                      // 必填，空数组也要给
 "tool_choice":{"type":"auto"},   // 必填，**对象**
 "metadata":{"locale":"zh",       // 必填；只能 zh 或 en（**不是 zh-CN**）
             "client_version":"0.2.13"},  // 必填
 "messages":[{"role":"user",
              "content":[{"type":"text","text":"hi"}]}]}
```

几个反直觉的点，都是实测撞出来的：

| 给错了会怎样 | 正确写法 |
|---|---|
| `thinking: false` → `thinking must be an object` | `{"enabled":false}` |
| `thinking:{"type":"disabled"}` → 缺字段 | `{"enabled":false}` |
| `system:[{"type":"text",...}]` → `invalid JSON request` | `["纯字符串"]` |
| `metadata.locale:"zh-CN"` → `must be zh or en` | `"zh"` |
| `tool_choice:"auto"` → `must be an object` | `{"type":"auto"}` |
| `messages[].content:"hi"` → `must be an array` | `[{"type":"text","text":"hi"}]` |
| `turn_id:"t1"` → `must be a lowercase canonical UUID` | `str(uuid.uuid4())` |
| `tool_result.content:[块数组]` → `invalid JSON request` | `content:"字符串"` + `is_error:false` |

注意 `system` 给错时的报错是 **`invalid JSON request`**，和「字段缺失」完全
不同——形状错了会被误导成「我的 JSON 拼坏了」，这个最费时间。

### 只支持流式

```
Accept: application/json   -> 406 SSE_ACCEPT_REQUIRED
Accept: text/event-stream  -> 200
```

所以非流式请求也在内部走流式、聚合完再返回（`aggregate_stream`）。

### SSE 事件

```
{"type":"message_start","response_id":"resp_...","model":"..."}
{"type":"thinking_delta","text":"..."}          # 思考链，可能没有
{"type":"content_delta","text":"..."}           # 正文
{"type":"tool_use_start","id":"call_...","name":"get_weather"}
{"type":"tool_input_delta","id":"call_...","partial_json":"{\"city\":"}
{"type":"tool_use_end","id":"call_..."}
{"type":"usage","usage":{"input_tokens":161,"output_tokens":50}}
{"type":"message_end","stop_reason":"end_turn"|"tool_use"}
```

映射：`thinking_delta` → `delta.reasoning_content`，`content_delta` →
`delta.content`，`message_end` → `finish_reason` + `usage`。

### 会话复用

`chat_id` 可以复用（实测同一 chat 连发 3 个 turn 都正常），但每次推理都要新
注册一个 turn。实现里按「一个 chat 用 4 次」池化——端砚的会话列表**用户可见**，
不能每个请求都 `/chat/new` 一次。

## 6. 计费：Token Plan 双窗口

```
GET {billing}/credits/balance
```

```json
{"code":0,"data":{
  "available_credits":"9999.851",        // 字符串！不是数字
  "usage_windows":{
    "5h":{"enabled":true,"limit_credits":"10000.000","used_credits":"0.149",
          "remaining_credits":"9999.851","next_recover_at":"2026-11-29 07:36:34"},
    "7d":{"enabled":true,"limit_credits":"50000.000","used_credits":"0.149",
          "remaining_credits":"49999.851","next_recover_at":"2026-12-06 02:36:34"}},
  "calculated_at":"2026-11-29 03:06:17"}}
```

两个窗口是**并存的约束**（5 小时滚动窗 + 7 天窗），不是「套餐 + 每日」那种
相加关系——所以额度卡**不能设 `sum_items`**，否则界面会把两个窗口的剩余量加
起来显示成一个根本不存在的总额。

时间字段是无时区标记的北京时间字符串，按 `+08:00` 解析；解析不出来就给
`None`（不显示倒计时），不要退回当前时间——那会显示「0 秒后重置」这种假倒计时。

这两个时间是 `reset_ts`（周期回满），**不是** `expire_ts`（权益作废）。混用会
让到期横幅误报「5 小时后到期」。

## 7. 没有签到

端砚的免费额度是**月度自动发放**，没有点击签到：

```
POST {billing}/users/free-grant   -> {"next_action":"none","has_received":true,
                                      "pkg_id":"pkg_free_monthly_base",
                                      "received_at":"2026-11-29 02:36:34"}
```

所以 `supports_checkin = False`，签到卡不渲染（与 traepat 同口径）。

## 8. 实现取舍

| 决策 | 原因 |
|---|---|
| 只支持 macOS 解密 | 端砚只发了 macOS 版；Linux 是 libsecret、Windows 是 DPAPI，各一套算法，现在做没有意义 |
| 不做自动登录 | 见第 3 节，14 天续一次，重开客户端更划算 |
| 非流式内部走流式 | 上游 406 拒绝 json Accept，没有别的路 |
| `canonical_id` 当别名 | `sage-s2.1-flash` 和 `书生-S2.1-Flash` 是同一个模型的两个名字，两种写法都该能用 |
| 图片块直接丢弃 | 端砚 content 块**不支持** image（各种字段名都 422），且 12 个模型全是 `supports_attachments=false` |
| 不设 `sum_items` | 5h/7d 是并存约束，不是相加关系 |
| 时钟按 +08:00 | 上游给的是无时区北京时间 |

## 9. 验证方式

```bash
# 1. 登录态 + 目录 + 额度
PYTHONPATH=src .venv/bin/python -c "
from buddy_proxy.auth.login import _login_inkstone
raise SystemExit(_login_inkstone())"

# 2. 起一个只开 inkstone 的实例
PYTHONPATH=src .venv/bin/python -m buddy_proxy \
  --port 8792 --inkstone --default-provider inkstone

# 3. 三种协议
curl -s localhost:8792/v1/chat/completions -d '{"model":"inkstone/书生-S2.1-Flash",
  "messages":[{"role":"user","content":"只回复两个字：你好"}]}'
curl -sN localhost:8792/v1/chat/completions -d '{"model":"书生-S2.1-Flash",
  "stream":true,"messages":[{"role":"user","content":"数到二"}]}'
curl -sN localhost:8792/v1/messages -H "anthropic-version: 2023-06-01" \
  -d '{"model":"inkstone/书生-S2.1-Flash","max_tokens":100,"stream":true,
       "messages":[{"role":"user","content":"说 hi"}]}'

# 4. 工具调用
curl -s localhost:8792/v1/chat/completions -d '{"model":"inkstone/GLM-5.3",
  "messages":[{"role":"user","content":"北京天气？用工具查"}],
  "tools":[{"type":"function","function":{"name":"get_weather",
    "parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]}'
# 期望 finish_reason=tool_calls，arguments={"city": "北京"}
```

实测 2026-11-29 全部通过：非流式（system 生效）、流式（含 usage + `[DONE]`）、
Anthropic（`message_start` → `content_block_*` → `message_delta` → `message_stop`）、
工具调用（`finish_reason=tool_calls`）、额度卡（5h/7d 双窗口）、`buddy login
inkstone` 四行输出。
