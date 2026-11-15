# Antigravity 通道：模型速度、限频与「免费最优选」实测

> 实测日期 2026-11-15，两个 Google 账号（下文以 `eth` / `slay` 指代，均为免费层），
> 直连上游 `cloudcode-pa.googleapis.com`，绕过网关。
> 复测脚本：`tools/bench_antigravity.py`（用法见文末）。

## 0. 一页结论

| 需求 | 单选 |
|---|---|
| **免费/低成本首选（速度 + 额度都够）** | `gemini-3.5-flash-lite`（上游名同，未在网关目录里暴露）——TTFT ≈1 s，20 req/s 打 100 次 100% 成功 |
| 网关已暴露的等价快速档 | `gemini-2.5-flash`（TTFT ≈0.8–1.2 s，burst 20/s 未触限） |
| 要强推理、还想首字快 | `claude-opus-5-5-medium`（≈3.7 s）/ `claude-sonnet-5-5-medium`（≈1.7 s）——走 Claude/GPT 池，与 Gemini 池互不挤占 |
| 要最强推理、能忍受 9–14 s 首字 | `gemini-4-hydrogen-xhigh` |
| 要较强推理、首字 7–10 s | `gemini-3.8-flash`（实发上游 `gemini-3.8-flash-tiered`） |
| **别再用** | `gemini-3.1-pro`（404）、`gemini-3.6-flash`（404 / 429 / 挂起）、`claude-sonnet-4-6`、`claude-opus-4-6-thinking`（200 空壳 "no longer available"）——2026-11-15 已从 `models.json` 删除 |

**限频**：`gemini-2.5-flash` 与 `gemini-3.5-flash-lite` 在 **20 req/s 持续 5 秒（共 100 次）**
全部 200，**没探到天花板**。也就是说单条通道的瓶颈是排队/思考时间，不是请求速率。

**配额水位不可信**：`fetchAvailableModels` 的 `remainingFraction` 是**池级水位**，
Gemini 组读到 ≈1.0 时 `gemini-3.8-flash` 照样要排队 30–70 s 出首字。判断"快不快"
只能实测，不能看配额条。水位读数还**按连接缓存**（见 §3.1），跨连接比较差值没有意义。

## 1. TTFT / 总耗时（n=3，串行，同一 prompt）

prompt 固定为「用中文简单解释什么是 TCP 三次握手，100 字以内」，`maxOutputTokens=4096`，
`temperature=0`。TTFT = 第一个带 text 的 SSE 事件到达时刻。

### 1.1 Gemini 4 / 3.8（主力推理档）

| 模型（上游名） | 账号 | 结果 | TTFT p50 | TTFT p90 | 总耗时 p50 | thoughts tokens |
|---|---|---|---|---|---|---|
| `gemini-4-hydrogen-xhigh` | eth | 3/3 200 | 10.88 s | 11.44 s | 11.29 s | 1986 |
| `gemini-4-hydrogen-xhigh` | slay | 3/3 200 | 9.23 s | 13.78 s | 9.67 s | 1557 |
| `gemini-3.8-flash-tiered` | eth | 3/3 200 | 6.84 s | 8.36 s | 7.21 s | 701 |
| `gemini-3.8-flash-tiered` | slay | 3/3 200 | 7.78 s | 10.10 s | 8.27 s | 946 |
| `gemini-4-argon-medium` | eth（n=1） | 1/1 200 | 86.34 s | — | — | — |
| `gemini-4-argon-low` / `-high` | eth（n=1） | 1/1 200 | 80.06 s / 82.72 s | — | — | — |

**`gemini-4-argon` 是独立周池**（上游 `resetTime` 单列，比 Gemini 主池晚一周），
三个档位都能跑通但首字 80–86 s，而且**会排在其他请求后面**——同一天串行测试，
后跑的 `-high` 比先跑的 `-low` 还慢 2 s。只在「能等一分半、且确实要重推理」时才用。

### 1.1b 3.8-flash 各档与 Claude / GPT（eth，n=1）

清理模型目录时对 `models.json` 每个默认档做了一次可用性验证，全部 200：

| 模型（上游名） | 耗时 |
|---|---|
| `gemini-3.8-flash-tiered`（默认）/ `-low` / `-high` | 12.81 s / 7.44 s / 14.78 s |
| `claude-opus-5-5-medium`（默认）/ `-low` / `-high` | 3.69 s / 4.05 s / 3.30 s |
| `claude-sonnet-5-5-medium`（默认）/ `-low` | 1.70 s / 1.84 s |
| `gpt-oss-120b-medium` | 1.43 s |
| `gemini-3.1-flash-image` | 4.52 s |

结论：**`gemini-3.8-flash` 不是只有 `-tiered`**（旧结论已被推翻，`-low`/`-medium`/
`-high` 都可用，`-tiered` 依旧是最稳的默认档）；claude/gpt-oss 族在上一轮
长延迟测完之后明显变快（opus 首字 3–4 s、sonnet 1.5–2 s）。

**两账号差异**：同一天、同一时段，eth 与 slay 差 1–2 s，属于正常抖动。
早先测到 slay 上 3.8-flash 有 68 s p50、eth 上 18 s p50 的极端数据，复测后回到
7–10 s——那是**上游排队**，不是账号质量问题，也不是限频（同时段 burst 测试全 200）。

### 1.2 hydrogen 四档对比（eth，早先批，n=5）

| 档位 | 结果 | TTFT p50 | TTFT p90 | thoughts |
|---|---|---|---|---|
| `-xhigh` | 5/5 200 | 9.93 s | 13.95 s | 1633 |
| `-high` | 5/5 200 | 13.90 s | 17.24 s | 1112 |
| `-medium` | 5/5 200 | 14.19 s | 23.49 s | 1508 |
| `-low` | 5/5 200 | 12.25 s | 15.06 s | 1932 |

**档位越高不一定越慢、越低不一定越快**：思考量（thoughtsTokenCount）才是耗时主因，
而且不随档位单调变化。要稳定快，别指望调低档位，换 hydrogen 以外的模型更有效。

### 1.3 快速档（slay，n=5）

| 模型 | 结果 | TTFT p50 | TTFT p90 |
|---|---|---|---|
| `gemini-2.5-flash` | 5/5 200 | 1.18 s | 1.24 s |
| `gemini-2.5-flash-lite` | 5/5 200 | 1.41 s | 1.86 s |
| `gemini-3-flash` | 5/5 200 | 1.11 s | 1.85 s |
| `gemini-3.5-flash-lite` | 5/5 200 | 0.96 s | 1.32 s |

### 1.4 Claude / GPT（`claude-gpt` 池）

只做了每个默认档一次的点测（见 §1.1b）：opus-5-5 ≈3.7 s、sonnet-5-5 ≈1.7 s、
gpt-oss-120b ≈1.4 s。未做 n=5 系统测量；可用性见第 4 节。

## 2. 突发频率（burst）

| 模型 | 速率 × 时长 | 成功 | TTFT p50 | 备注 |
|---|---|---|---|---|
| `gemini-2.5-flash` | 20/s × 5 s（100 次） | 100/100 200 | 0.83 s | 未触限 |
| `gemini-3.5-flash-lite` | 20/s × 5 s（100 次） | 100/100 200 | 0.92 s | 未触限 |
| `gemini-2.5-flash` | 1/2/5/10/20/s 各档（早先批） | 全部 100% 200 | 1.16 s @20/s | 未触限 |

**没探到限频天花板**。免费层的限制以**配额（token/请求量）**为主，不是请求速率；
突发 20 req/s 时 TTFT 也没恶化，说明上游对这类模型做了充分的并发排队。

> 注意：长时间高强度打，消耗的是池子里的 credit，会体现在 `remainingFraction`
> 下降与 5h 窗口 reset 时间上，而不是瞬时 429。

## 3. 成本

| 项 | 结论 |
|---|---|
| 免费层额度 | `fetchAvailableModels.quotaInfo.remainingFraction` 按**组**（`gemini` / `claude-gpt`）聚合，组内共享 **weekly + 5h 双池**；水位只在使用**接近**上限时下降 |
| 2.5 / 3 / 3.1-flash-lite / 3.5-flash-lite | **官方账号不暴露**（`fetchAvailableModels` 不返回），不在网关目录里；直连能用，但不在 UI 配额条覆盖范围内，**无法从面板确认其是否计入池子**。单次 burst 后水位无可见变化；**但不能据此断言 0 成本**，需长时段验证 |
| 3.8 / 4-hydrogen | 在 `gemini` 组内共享（5 小时池），正常计 credit |
| 4-argon | **独立周池**（`resetTime` 单列），计 credit 但不挤占 3.8 / hydrogen |
| claude-opus-5-5 / claude-sonnet-5-5 / gpt-oss-120b | `claude-gpt` 组共享另一个 5 小时池 |

请求日志的 Credit 列对 antigravity 是**水位落差换算的估算**（`—` 表示这条没结算出数字），
口径与限制见 §3.1。

### 3.1 水位落差属于哪一条请求（2026-11-16 实测）

网关要在日志里给每条请求记一个耗时消耗，唯一可观测的信号就是这条 pool 水位。问题是
「水位差」能不能挂到某一**条**请求上。`tools/bench_antigravity.py watermark` 三段实测
（`--account eth`，`gemini-3.8-flash-tiered`）：

| 段 | 测什么 | 结果 |
|---|---|---|
| 1. 固定连接 | 同一条 client 连读 5 次 | `0.7539876 ×5`，**稳定** |
| — | 每次新建连接读 | `0.7551521 / 0.7562798 / 0.7592398` 随机出现，含「回升」 |
| 2. 逐条落差 | 固定连接上连发请求、各自收尾读一次 | 1 token → `0.0000015` / `0.0000017`；4393 token → `0.0008030` |
| 3. 空转 | 不发请求空转 30 s | `drop=-0.0014291`（**回升**，比一次大请求还多） |

另一轮固定连接连发 4 条：`0.7562798 → 0.75617707 → 0.7560655 → 0.7559592 →
0.7558411`，逐条单调下降。

**结论**：

1. 水位读数**按连接/接入节点缓存**——新建连接就读到另一份快照。这是早期「网关里看到
   水位窄带跳动、直连却单调」的全部原因。积分探测因此复用一条长连接
   （`provider._fetch_watermark`），面板路径仍每次新建（只看当前值，不需要稳定）。
2. 固定连接上落差是**逐条单调**的，「上一条 → 这一条」的差值可以归到本条请求。
3. 但**精度只够看量级**：水位 4 位有效数字，同样规模的一条请求实测 0.11 / 0.12，
   也出现过 6.41（约 60 倍）；上游还会不规律回升（第 3 段），回升那条记 `—` 并把基线
   跟到新读数（否则下一次落差虚增）。换算系数 1000 是**标定值**，不是官方口径。

## 4. 可用性清单：哪些别再碰

| 模型 | 现象 | 判断 |
|---|---|---|
| `gemini-3.1-pro` | `-low` 404、`-high` 400 INVALID_ARGUMENT（早先批）；本次复测 `-low/-high` 均 404、正文 `Requested entity was not found.` | **已下架**；2026-11-15 从 `models.json` 删除，`gemini-3.1-pro` 进入 `blocked_prefixes`（刷新不会灌回） |
| `gemini-3.6-flash` | 2026-11-15 首轮：`-medium` 两账号全部 429，**无 system 也 429**，1 req 即 429；复测裸名 404，`-medium/-high/-low` 均**挂起超时**（>5 min 无响应） | **已下架 / 池内被摘**，不是内容拦截、不是瞬时排队；同日从 `models.json` 删除，`gemini-3.6-flash` 进入 `blocked_prefixes` |
| `claude-sonnet-4-6` | HTTP 200，正文 "Claude Opus 4.6 is no longer available. Please switch to Claude Opus 5.5." | **已下架**（200 空壳）；2026-11-15 删除 |
| `claude-opus-4-6-thinking` | 同 `claude-sonnet-4-6`：200 空壳 "…no longer available…" | **已下架**；2026-11-15 删除 |
| `claude-sonnet-5-5` / `claude-opus-5-5` | 裸名 404；加 `-low/-medium/-high` 后 200 | **可用**，2026-11-15 已按 `claude-<name>-5-5` + effort 加入目录 |
| `gemini-4-carbon-disabled` | `generateContent` 404 `Requested entity was not found.`；`fetchAvailableModels` 照常回传且是唯一没有 `quotaInfo` 的条目 | **上游占位条目，不可路由**；不在 `models` 里，按 `-disabled` 后缀挡在刷新之外（否则点刷新就多出一条死模型） |
| `gemini-3.5-flash` | stub（在 `blocked_prefixes` 里，不暴露） | 概念残留，别用 |
| `gemini-2.5-*` / `gemini-3-flash*` / `gemini-3.1-flash*` | 仍可直连（见 §1.3），但被 `blocked_prefixes` 挡在 `/v1/models` 之外 | 能用，网关不暴露；要暴露得改 `models.json`（`blocked_prefix_exceptions` 按精确基名放行 + 加 verified 条目） |

## 5. 两个必知的坑

### 5.1 `maxOutputTokens` 太小 → 空正文

这些模型默认开思考。`max_tokens: 512 / 64` 时 thinking 会把预算吃光，上游返回
`finishReason: MAX_TOKENS` + `continuationToken` + **空 text**。走 Anthropic 协议时
表现为 `content: []`，看着像网关 bug，实为**额度不够**。

> 2026-11-15 实测：`claude-opus-4-6`（Anthropic Messages 非流式，该模型现已下架，
> 结论对 claude-5-5 族同样成立）`max_tokens=64` 返回 `content: []`；改到 4096 后
> `content: [{"type":"text","text":"可以"}]` 正常。
> **至少 4096**；带工具调用的 agent 场景建议 8192+。

### 5.2 `fetchAvailableModels.remainingFraction` ≠ 排队深度

它是池级水位（只在接近上限时下降），**不反映瞬时排队**。Gemini 组 rem≈1.0 时
3.8-flash 仍可能等 30–70 s。要判断"现在能不能用、快不快"，用 §7 的脚本实测。
另外它**按连接缓存**（见 §3.1）：跨连接读到的是不同快照，比较差值会得到假结论。

## 6. 免费/低成本最优选

| 场景 | 推荐 | 理由 |
|---|---|---|
| 高频轻量调用（分类、抽取、小工具） | `gemini-2.5-flash`（网关已暴露） | TTFT ≈1 s，20 req/s 不触限，池内成本极低 |
| 更强一点、仍然要快 | `gemini-3.5-flash-lite`（需手动加入 `models.json`） | TTFT ≈1 s，实测 100/100 成功；**注意未经长时段验证** |
| 日常编码 / agent | `gemini-3.8-flash` | 首字 7–10 s，思考 700–950 tokens，质量/速度平衡点 |
| 强推理但不想等 | `claude-opus-5-5`（默认 medium） | 首字 3–4 s，走独立的 Claude/GPT 池，不占 Gemini 池额度 |
| 长上下文便宜档 / 批量 | `claude-sonnet-5-5` | 首字 1.5–2 s，同属 Claude/GPT 池 |
| 硬推理（复杂重构、算法） | `gemini-4-hydrogen-xhigh` | 思考 1500–2000 tokens（带 system 的 agent 请求），首字 9–14 s，值得等 |
| 能等一分半的超重推理 | `gemini-4-argon`（默认 medium） | 独立周池，首字 80–86 s，且会排队 |
| **不要用** | `gemini-3.1-pro` / `gemini-3.6-flash` / `claude-*-4-6` | 见 §4（已从目录删除） |

### 下一步待办

- [ ] **AA（Artificial Analysis）排名核对**：把上述"快档 vs 强档"与第三方 MMLU / 编码评测
      对齐，确认 `gemini-3.8-flash` 相对 `2.5-flash` 的代差是否值得 7 倍 TTFT。
      *（本次未完成：AA 站点需交互式查询，留给下一轮）*
- [ ] 把 `gemini-3.5-flash-lite` / `gemini-2.5-flash` 是否真"0 成本"用长时段（≥24h）
      水位变化验证，而非单次 burst 推断。
- [x] ~~`gemini-3.6-flash` 两账号全 429 / 复测挂起的根因~~：2026-11-15 复测裸名 404，
      确认为下架；已从 `models.json` 删除并加进 `blocked_prefixes`（见 §4）。
- [ ] `gemini-4-argon` 的 80–86 s 首字是否稳定：是否只是当日排队，需要换个时段复测。

## 7. 复测方法

```bash
# 单个模型 TTFT（n=5，串行）
uv run python tools/bench_antigravity.py ttft gemini-3.8-flash-tiered --account eth --n 5

# 一组模型依次跑
uv run python tools/bench_antigravity.py sweep \
    --models gemini-3.8-flash-tiered,gemini-4-hydrogen-xhigh --account slay --n 3

# 突发频率（20 req/s，持续 5 秒 = 100 次）
uv run python tools/bench_antigravity.py burst gemini-2.5-flash --rate 20 --secs 5 --account slay

# 水位落差归属（固定连接稳定性 / 逐条落差 / 空转回升，见 §3.1）
uv run python tools/bench_antigravity.py watermark --account eth --idle 30
```

脚本要点：
- **必须用上游真名**：`gemini-3.8-flash-tiered`、`gemini-4-hydrogen-xhigh`（裸名 404；
  已下架的模型裸名给 404，claude-4-6 反而是 200 空壳——**验新模型要看正文**）。
- `maxOutputTokens` 固定 4096（见 §5.1）。
- 直连上游，不经过网关——测的是上游真实表现；网关自身开销只有毫秒级。
- `--account` 用账号 id 前缀（`eth` / `slay`）筛选。

**注意**：每次调用都消耗上游额度，`sweep` 默认每模型 5 次。跑 burst 100 次时消耗不小。

## 8. 复现说明

精确到小数点的数字都可以用 §7 的命令原样复现（上游排队有抖动，重复跑出来的
绝对值和本文可能差几秒，量级应一致）。本文所有结论均来自 2026-11-15 /
2026-11-16 的实测输出，没有引用第三方评测。
