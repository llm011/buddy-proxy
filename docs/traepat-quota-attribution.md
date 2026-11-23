# Trae PAT 额度归属与费率实测（2026-11-24）

结论先行：**PAT 的 40 个模型分三档计费**，其中 19 个国产模型扣的是**同一个
Trae 账号的 work 余额**（不是 PAT 独立额度），4 个 GPT 扣各自的日包，6 个完全
免费。模型页的标签列已经按这份结论标注。

本文记的是**怎么测出来的**和**为什么是这个结论**，因为下次上游一变，得有人
能重跑一遍。

## 1. 三档归属

| 档位 | 模型 | 扣什么 |
|---|---|---|
| 日包 | `gpt-5.6-sol` / `gpt-5.6-sol-max` | 日包（GPT-5.6 Sol）80 点 |
| 日包 | `gpt-6-astra-max` | 日包（GPT-6）58 点 |
| 日包 | `gpt-6.1-sol` | 日包（GPT-6.1）80 点 |
| **Trae 账号余额** | 19 个国产模型（glm / deepseek / kimi / qwen / doubao / mimo / minimax / step） | 同一个 Trae 账号的 work 余额 |
| **免费** | `gpt-5.4`、`gpt-5.5-max`、`gpt-5.6-luna-max`、`gpt-5.6-terra-max`、`gpt-5.2`、`gemini-3-flash` | 什么都不扣 |

日包每账号 4 个（80/58/80/80），与 `available_model_key` `gpt_56_sol` /
`gpt_6` / `gpt_6_1` / `gpt_6_2` 一一对应。

### 1b. 那 6 个「免费」是怎么确认的

2026-11-24 逐个复核，每个模型发一条 ~300 字 prompt，**前后各拉一次两个计数器**：

```
gpt-5.4             余额Δ=0.0  包Δ={}
gpt-5.5-max         余额Δ=0.0  包Δ={}
gpt-5.6-luna-max    余额Δ=0.0  包Δ={}
gpt-5.6-terra-max   余额Δ=0.0  包Δ={}
gpt-5.2             余额Δ=0.0  包Δ={}
gemini-3-flash      余额Δ=0.0  包Δ={}
```

**这一档最容易标错**，因为它们三个特征全都指向「在花钱」：

- 都走 **plus 网关**（`copilot-cn.bytedance.net`）
- 响应里的 `account_type` 报 `ptu` 或 `paygo`
- 其中 `gpt-5.4` / `gpt-5.5-max` / `gpt-5.6-sol` 共用 `gpt-5.6-sol` 这个
  `provider_model_name`

但两个计数器一次都没动。**网关 ≠ 扣费，`account_type` 也不等于扣费。**

### 1c. 为什么说「Trae 账号余额」而不是「PAT 额度」

因为 PAT 和 `trae` 通道**用的是同一个账号**（uid 相同），账是同一本。证据：

```
plus 网关（copilot-cn）返回的 5 个包
→ 国产模型一个都不动（连发 3 次，basic_usage_amount 全程为 0）

trae work 接口（api.trae.com.cn）的 balance_summary
→ 连发 3 次：glm-5.3 -2.12 / kimi-k3 -12.39 / deepseek-v4.1-flash -0.19
```

所以模型页标「Trae 账号余额」，管理页 `trae` 通道的那条总额度就是它们的钱包。
之前标过「CN 共享余额」，没说清是谁的余额，容易误以为 PAT 有一份独立额度。

## 2. 费率倍数（glm-5.3 = 1.0）

实测单次扣费（同一条 ~300 字 prompt，credits）除以 glm-5.3 的 0.94：

| 模型 | 倍数 | 单次 |
|---|---|---|
| `kimi-k3` | **6.649** | 6.25 |
| `doubao-seed-evolving` | 2.915 | 2.74 |
| `Doubao-Seed-2.1-Pro` | 2.723 | 2.56 |
| `kimi-k2.8-preview` | 2.021 | 1.90 |
| `deepseek-v4-pro` | 2.011 | 1.89 |
| `glm-5.2` / `step-5-preview` | 1.16 | 1.09 |
| **`glm-5.3`（基准）** | **1.0** | 0.94 |
| `qwen3.8-max` | 0.851 | 0.80 |
| `kimi-k2.7-code` | 0.447 | 0.42 |
| `qwen-3.7-plus` | 0.426 | 0.40 |
| `glm-5.3-flashx` | 0.415 | 0.39 |
| `minimax-m3` | 0.245 | 0.23 |
| `Doubao-Seed-Code` | 0.17 | 0.16 |
| `mimo-v2.6-pro` / `deepseek-v4-flash` | 0.128 | 0.12 |
| `glm-5.3-flash` | 0.121 | 0.114 |
| `deepseek-v4.1-flash` | 0.104 | 0.098 |
| `qwen3.8-flash` | 0.083 | 0.078 |

**为什么不拿最便宜的 deepseek 当基准**：以 `deepseek-v4.1-flash`（0.098）为 1，
`kimi-k3` 会显示成 **×64**，看着像天价而没人敢用。以主力模型 glm-5.3 为 1，
倍数落在 0.08–6.6，一眼能看出量级。

数值落在 `web/models_config.json` 的 `credit_rate` 字段（`0` = 免费，
`null` = 走日包），由 `pat/models.py` 热加载成 `PAT_MODEL_CREDIT_RATES`。

## 3. 测量方法（要重测时照这个做）

### 3.1 前提：必须绕开本地代理

`urllib.request.urlopen` 默认读 `HTTP(S)_PROXY`。本机 `getproxies()` 指着
`127.0.0.1:7890`，代理连上了却转发不过去——**PAT 请求会 30s 超时，而且
`_gateway_reachable` 的 TCP 预检（不走代理）会通过，看起来像上游挂了**。

探测脚本一律用 `pat.net.open_direct()`。2026-11-24 就是踩了这个坑，第一轮
"网关不可达"完全是假的。

### 3.2 两次快照做差

```python
def bal():     # Trae work 余额（api.trae.com.cn）
    r = fetch_ent_usage(token=..., region="cn")
    return r["balance_summary"][0]["total_balance"]

def packs():   # PAT 侧额度包（copilot-cn.bytedance.net）
    r = json.loads(open_direct(req, timeout=20).read())
    return {p["entitlement_base_info"]["entitlement_id"]: p["usage"]["basic_usage_amount"] ...}

before = (bal(), packs())
ask(model, PROMPT)
after = (bal(), packs())
```

### 3.3 低费模型要连发 5 次取均值

余额只报两位小数。`mimo-v2.6-flash` 单次 0.042——一次采样会被 0.01 的量化
误差吃掉近一半。连发 5 次再除，误差降到 ±0.002。

实测对照（300 字 prompt）：

| 模型 | 单次采样 | 5 次均值 |
|---|---|---|
| `deepseek-v4.1-flash` | 0.09 | **0.098** |
| `qwen3.8-flash` | 0.06 | **0.078** |
| `glm-5.3-flash` | 0.10 | **0.114** |
| `mimo-v2.6-flash` | 0.04 | **0.042** |

### 3.4 陷阱

- **公共模型走 `trae-api-cn.mchost.guru`，plus 模型走 `copilot-cn.bytedance.net`**。
  发错网关一律 `4001 param is invalid`，看起来像模型不存在。
- **小写名不一定能用**。`deepseek-v4-pro` 直接发会 4001，要走 `MODEL_MAP` 的
  `DeepSeek-V4-Pro`。同一批探测里差点因此误删。
- `1005 extra.plan=1` = 名字有效但账号档位不足（`kimi-k3.1-pro` /
  `kimi-k3.1-pro-highspeed` / `qwen4-max`），**不是模型不存在**。
- `gemini-3-flash` 能调通，但响应里 `provider_model_name` 是
  `ep-20261019181941-f5knh`，`model` 字段报 `deepseek-v4.1-flash`——名字对不上
  实际模型。保留（有用），但在 `name` 里注明了「实为 DeepSeek-V4.1-Flash」。

## 4. 已下线的额度包

`free_weekly_*`（weekly，300 点）**实测从未被任何模型消耗**。它对应
`openrouter/es1_orange_*` 那条上游线，而 openrouter 的 5 个模型已于 2026-11-23
确认全线撤除（`4001`，见 PR #309）。

额度还在上游账上，但没有模型会花它。留在额度页会让人以为还有 300 点可用，
所以从展示里过滤掉了——**用显式名单** `DEAD_QUOTA_PACKS`（`quota.py`），
不是 `"weekly" in entitlement_id` 这种子串判定：上游将来新加一个 weekly 池
会被静默吃掉，用户看不到、我们也不会知道。

哪天这个池改绑到活的模型线，把前缀从名单里删掉即可。

## 5. 相关代码位置

| 关注点 | 位置 |
|---|---|
| 归属 + 费率数据 | `web/models_config.json` 的 `quota_pack` / `credit_rate` |
| 热加载 | `trae/pat/models.py` → `PAT_MODEL_QUOTA_PACKS` / `PAT_MODEL_CREDIT_RATES` |
| 出站直连 | `trae/pat/net.py`（`BUDDY_PAT_USE_PROXY=1` 可切回代理） |
| 额度条目构造 | `trae/pat/quota.py` → `_quota_items` |
| 死包名单 | `trae/pat/quota.py` → `DEAD_QUOTA_PACKS` |
| UI 渲染 | `web/static/chart_log.js` → `_creditRateTag`（在 `renderGroups` 内） |
| 一致性测试 | `tests/test_trae_pat_failover.py` 的 `test_credit_rate_table_*` / `test_quota_pack_*` |

## 6. 已知缺口

- **没有和上游目录的长期一致性测试**。`MODEL_IDS` 与 CN 目录之间只有按日期
  切片的历史快照测试，全靠人肉定期拉目录。做不成离线单测：目录是活的，且需要
  真凭证。
- **`gemini-3-flash` 的路由错配没有上游解释**。目前只是标注，没查清是临时
  调整还是长期行为。
