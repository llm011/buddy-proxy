# 豆包签到 (`landing/checkin`) 故障排查全记录

> 状态：**未解决**。签到领取接口恒回 `710010702 系统错误`，代理和豆包自己的前端点按钮都一样失败。
> 记录时间：2026-11-16
> 排查人：AI agent（接手的同学建议先读「结论」和「已经排除的可能」，别重复劳动）

---

## 一、结论先行

**读接口正常，写接口正常，只有签到这一个接口坏。**

| 接口 | 调用方式 | 结果 |
|---|---|---|
| `POST /alice/ug/campaign/v3/detail/` | 查签到状态 | ✅ `code:0`，能正确读出「今天未签、可领 400」 |
| `POST /alice/ug/campaign/item/reward/claim/` | 领里程碑奖励 | ✅ 能通，返回业务语义错误（`biz_error.code=2/3`） |
| `POST /alice/ug/campaign/landing/checkin` | **领每日签到** | ❌ 恒回 `710010702 系统错误` |

关键点：

1. **认证没问题。** 同一个页面上下文里，另一个写接口能拿到业务级响应；去掉 cookie 时两个接口都会明确回 `710012001 登录已过期`。所以 token / session / cookie 都是好的。
2. **参数不是问题。** 试过 App 真实的全量参数、精简参数、带签名、不带签名，结果一样。
3. **豆包自己的前端也失败。** 用真实鼠标事件点页面上的「立即签到」按钮，页面的官方代码发出请求，同样是 `710010702`，界面弹「签到失败，请稍后重试」。

因此问题在**上游服务端**或**账号风控**，不在代理的参数/上下文/签名。继续调参数没有意义。

### 两个候选假设（未能在接口层区分）

- **A. 服务端该接口逻辑故障 / 灰度中**：官方前端自己都失败，符合。
- **B. 该账号被风控**：13 天里代理重试了 318 次（平均 24.5 次/天），频率本身可疑。若能拿到一个干净账号（从未高频重试过）做对照，可区分 A/B。

**建议下一步**：用一个**没有高频重试过**的账号，在干净环境下调用一次 `landing/checkin`。成功 → 是 B（风控），失败 → 是 A（服务端）。这是唯一还没做的判别实验。

---

## 二、为什么之前「修了三次都没修好」

这是本次排查最有价值的发现，接手的同学务必知道。

历史上有三次修复都声称成功，但**验证依据是错的**：

| 时间 | 修复 | 验证依据 | 问题 |
|---|---|---|---|
| 11-08 | 改走主 App 同源子 frame | 抓包看到签到请求 | 后来的抓包推翻了这个结论（2.36.3 改回主聊天页） |
| 11-12 | 改回主聊天页 + 绝对 URL | `code:0, checked_in:true` | 见下 |
| 11-13 | aid 从 497858 改成 1044603 | **`code: 0, idempotent: true`** | ⚠️ 致命：见下 |

### ⚠️ `idempotent: true` 是假阳性

`landing/checkin` 在**当天已经签过到**的时候，会返回：

```json
{"code": 0, "data": {"checked_in": true, "idempotent": true}}
```

`idempotent: true` 的语义是「**重复提交，今天早就签过了，这次什么也没做**」。

11-13 那次修复的验证记录原文是：

> 修复后同一台机器、**同一个已签到账号**，`landing/checkin` 从 `710010702` 变成 `code: 0, idempotent: true`

在一个**已经签到的账号**上验证领取接口，拿到的是幂等空操作成功。**这不是领取成功的证据。**

这就是「每次都说修好了、隔天又不行」的机制：当天验证时账号已签到 → 返回 idempotent 成功 → 以为修好了；第二天真需要领取时 → `710010702`。

**接手的同学注意：验证签到必须用「今天确实未签到」的账号，并且要确认随后 `detail` 里 `checked_in_today` 真的从 `false` 变成 `true`。只看 `code:0` 或 `idempotent:true` 都会被骗。**

---

## 三、真实成功记录（数据来源：`logs/checkin.jsonl`）

```
真实领取成功：仅 2 次
  2026-11-02  签到成功  credits=400
  2026-11-03  签到成功  credits=200

11-04 起至今：318 次失败，0 次成功
  平均 24.5 次/天（自动打卡每 30 分钟重试）

那 12 条「已签到（上游记录）」不是代理领的
  —— 是用户自己在 App 里手签后，代理查询时补记的
```

⚠️ 界面上看着一片绿的那几天，**几乎全是「上游记录」补记**，不是代理真领到的。判断代理是否真能签到，只能看 `message == "签到成功"` 的记录。

---

## 四、已排除的可能（都试过，别再试）

### 4.1 页面上下文

| 上下文 | 结果 |
|---|---|
| `doubaowork://doubaowork-chat/chat`（主聊天页） | ❌ 710010702 |
| `https://www.doubao.com/drive-iframe/drive/home/`（同源 iframe） | ❌ |
| `https://www.doubao.com/pc/agent-explore`（落地页） | ❌ |
| 全新 `https://www.doubao.com/chat/` 页面 | ❌ |
| 浏览器 `Target.createTarget` 新建后台页 | ❌ |

### 4.2 渠道参数

| 参数集 | 结果 |
|---|---|
| `aid=1044603` + `doubao_device_platform=desktop` + `web_platform=desktop`（App 真实值） | ❌ |
| `aid=497858` + `web` / `work_desktop`（旧值） | ❌ |
| 从 App `performance` 里取的全量参数（含 `device_id`、`web_id`、`tea_uuid`、`fp`、`channel=mac_official`、`chromium_version`、`web_tab_id`） | ❌ |

### 4.3 签名

| 方式 | 结果 |
|---|---|
| 不带 `msToken` / `a_bogus` | ❌ |
| 复用 App 真实 `detail` 请求的完整签名 query | ❌ |
| 用 `window.bdms.frontierSign({url})` 现场签名 | ❌ |
| 用 `window.byted_acrawler.frontierSign` | ❌ |

补注：`fetch` / `XMLHttpRequest` 发请求时，页面 mssdk 会把签名自动加到 `performance` 记录里，但 **`Runtime.evaluate` 里构造的请求拿不到同样的自动签名**。不过带上签名也一样失败，所以签名不是根因。

### 4.4 请求头

`Agw-Js-Conv: str`、`Origin`、`Referer`、`x-tt-logid`、`x-csrf-token`、`x-tt-passport-csrf-token`、`X-Requested-With` —— 全部无效。

### 4.5 请求 body

```json
{"scene": "pc_incentive_task_mvp_v3"}                      ← 官方前端实际发的
{"scene": "...", "biz_params": {"card_id": "..."}}
{"scene": "...", "card_event": {...}}
{"scene": "...", "AgwCommonParam": {}}
{}  /  {"scene": ""}  /  {"scene": "bogus_group"}          ← 错误码完全一样
```

注意最后一行：**连空 body、乱填 scene 都返回同一个 `710010702`**。这说明该接口对参数几乎不校验，一进去就抛系统错误 —— 进一步支持「服务端问题」的判断。

### 4.6 其它接口（试图绕过 `landing/checkin`）

| 接口 | 结果 |
|---|---|
| `landing/get`（活动页配置） | ✅ 能读到卡片「今日签到领 400 额度」 |
| `landing/report`（签到埋点） | ✅ `code:0`，但之后 checkin 依然失败 |
| `item/reward/claim`（奖励领取） | ⚠️ 见下 |

`item/reward/claim` 的坑：它对**任何** `campaign_item_code`（包括不存在的 `bogus_item_xyz`）都返回：

```json
{"code":0,"data":{"biz_error":{"code":3,"message":"领取结果确认中，请稍后重试"},"task_status":0}}
```

官方前端定义的成功码是 `["0","4","6"]`，`3` 不在其中 —— 这是**不执行任何操作的占位响应**，不能用来签到。试过多次，`detail` 状态毫无变化。

### 4.7 其它接口路径（均 404）

```
/alice/ug/campaign/v3/checkin/          → 404
/alice/ug/campaign/v3/daily_checkin/    → 404
/alice/ug/campaign/v3/signin/           → 404
/alice/ug/campaign/v3/task/checkin/     → 404
/alice/ug/campaign/landing/check_in     → 404
/alice/ug/campaign/landing/checkin_v2   → 404
```

---

## 五、可复用的抓包工具

**这是本次留下的最有价值的东西** —— 以后不用再从零摸索。

```bash
# 只读探查（不点按钮、安全）
.venv/bin/python tools/capture_doubao_checkin.py

# 额外真实点击一次页面上的签到按钮（会在上游产生一次领取尝试）
.venv/bin/python tools/capture_doubao_checkin.py --click

# 指定输出
.venv/bin/python tools/capture_doubao_checkin.py --port 9223 --out /tmp/cap.json
```

产物是一份 JSON，含：

- 所有 CDP target 清单（页面/iframe/webview）与各自登录态三态
- 每个上下文里 `v3/detail/` 的原始响应（签到状态真相）
- **App 自己发出的 campaign 请求**（参数名集合 + 是否带签名 + aid）
- **我们复刻的领取请求的最终 URL**（判断签名有没有被自动补上）
- `--click` 时页面官方代码发出的请求与响应原文

脚本会自己给出 `conclusion` 字段判定当前属于哪类故障。

### 工具自身踩过的两个坑（已在代码注释里写明）

1. `Page.addScriptToEvaluateOnNewDocument` 注入的脚本**绑定在 CDP session 上**，reload 导致连接断开后脚本失效。必须**直接在当前连接上注入**，不要 reload。
2. 注入钩子后**必须校验** `typeof window.__buddy_rec`，否则「没有记录」会被误读成「页面没发请求」——这个假证据差点让我得出错误结论。

### 存档里的凭据处理

`msToken` / `a_bogus` / `fp` / `web_id` / `device_id` / `tea_uuid` / `web_tab_id` /
`sessionid` 等一律**不落明文**，只记录「有没有、参数名集合」。这是 AGENTS.md 的硬要求。

⚠️ 这里踩过一个坑：`--click` 分支抓到的是**页面原文**，早期版本直接 `json.dump`
就把标识符明文带进存档了。现在两条路径共用 `_sanitize_record()`，落盘前统一洗。
**改脚本后请重新扫描一次存档**：

```bash
grep -oEi "(msToken|a_bogus|sessionid|device_id|web_id|tea_uuid|web_tab_id)=[^\"&,]{8,}" \
  docs/captures/*.json    # 应该没有输出
```

原始存档见 `docs/captures/2026-11-16-landing-page-click.json`。

---

## 六、代码位置速查

| 文件 | 作用 |
|---|---|
| `src/buddy_proxy/doubao/checkin.py` | 签到实现主体：`CheckinPage`（CDP 求值上下文）、`fetch_checkin_status`、`claim_checkin` |
| `src/buddy_proxy/doubao/provider.py` | `checkin_status` / `checkin_claim`，`_run_checkin` 负责在独立事件循环里拉起 CDP |
| `src/buddy_proxy/benefits.py` | 自动打卡循环 `_tick`、失败跟踪 `_track_claim_failure`、历史落盘 |
| `logs/checkin.jsonl` | 打卡历史（判断「代理是否真领到」的唯一可靠来源） |

### 关键常量

```python
_CAMPAIGN_GROUP = "pc_incentive_task_mvp_v3"
_TASK = "daily_signin"
_DETAIL_PATH = "/alice/ug/campaign/v3/detail/"
_CLAIM_PATH  = "/alice/ug/campaign/landing/checkin"
_REPORT_PATH = "/alice/ug/campaign/landing/report"
```

---

## 七、本次已做的改动（止疼，不治病）

分支：`docs/doubao-checkin-capture`

1. **`tools/capture_doubao_checkin.py`（新增）** —— 抓包工具，见第五节。
2. **`src/buddy_proxy/benefits.py` 失败退避** —— 连续失败时指数退避（30min → 1h → 2h，封顶 4h），并超过 3 次后当天不再重试。

   动机：13 天 318 次失败，平均 24.5 次/天。若上游按账号/设备做风控，这个频率本身就是加重因素；同时 `checkin.jsonl` 被失败记录刷满，真正有信息量的行被埋掉。**这不会让签到成功，只会让它失败时少打几次。**

3. **`tests/test_checkin_claim_backoff.py`（新增 7 条）** —— 锁住退避的四个性质：首次退避、递增、封顶、成功后清零。已验证：故意改坏实现（恒定退避 / `_tick` 不查退避表 / 成功不清零）时，对应测试会立刻红。

---

## 八、给接手同学的建议

1. **先做那个判别实验**：干净账号 + 干净环境调一次 `landing/checkin`。这能区分「服务端坏了」和「账号被风控」，成本最低。
2. **验证签到成功时，只认 `detail` 里 `checked_in_today` 从 `false` 变 `true`。** 不要相信 `idempotent: true`，不要只看 `code: 0`。
3. 如果实验证明是风控：需要换账号，或者长期停止重试等风控解除（这版退避已经把这个方向做好了一半）。
4. 如果是服务端问题：只能等，或者去抓**手机端 App** 的请求做对照（手机端可能有不同的接口/参数）。
5. 不要再重复第四节列出的实验，全部试过了。

---

## 附录：原始日志证据

```
# 真实成功仅两次
2026-11-02 True 签到成功 400
2026-11-03 True 签到成功 200

# 11-04 起典型失败
2026-11-16 02:29:31 [INFO] doubao checkin: claim failed: http=200
body={"data":null,"code":710010702,"msg":"系统错误","message":"system error",
      "error":{"code":710010702,"message":"系统错误","locale":"zh"}}

# 连续失败告警
2026-11-15 12:28:45 [ERROR] 打卡连续失败 18 次（doubao）：
豆包签到领取失败（上游未确认） —— 请检查凭据/渠道参数是否过期
```

```
# 上游真实签到状态（2026-11-16 查询）
{"今天已签到": false, "连续天数": 8, "上次签到": "2026-11-15", "奖励": 400}
```
