# AGENTS.md

给在本仓库工作的 AI agent（以及人）的约定。**动手前先读这一页**——这里记的
都是踩过坑之后定下来的规矩，不是风格偏好。

---

## 工作流：改动一律走 PR

**不允许直接提交到 `main`**。流程固定：

```bash
git checkout -b <type>/<简短主题>    # feat/ fix/ refactor/ docs/
# ... 改代码 ...
.venv/bin/python -m pytest tests/ -q     # 必须全绿
.venv/bin/python -m ruff check src tests # 必须干净
git commit -m "<type>(<scope>): <一句话>"
git push -u origin <分支>
gh pr create --base main
gh pr checks <PR号> --watch              # 等 4 个 job 全绿
gh pr merge <PR号> --squash --delete-branch
git checkout main && git pull --ff-only
./buddy restart                          # 让改动生效
```

commit body 写**中文**，说清「为什么改」和「踩过什么坑」，不是复述 diff。

---

## 改动范围：超过 1 个文件就开 worktree

**预判一次改动会碰到 2 个及以上文件时，在 git worktree 里做，不要直接动主工作区。**

```bash
git worktree add ../buddy-proxy-<主题> -b <type>/<主题>
cd ../buddy-proxy-<主题>
# 改完、测完、提 PR、合并
cd ../buddy-proxy && git worktree remove ../buddy-proxy-<主题>
```

**为什么**：主工作区经常同时有别人的未提交改动（这个仓库尤其常见——测一个通道
时顺手改了另一个）。直接在主工作区改，`git add` 很容易把别人的半成品带进自己
的 commit，或者 `git stash` 把别人正在测的东西收走。worktree 是你的独立沙箱，
两个人的改动互不干扰。

**例外**（不用开 worktree）：

- 只改 **1 个文件**；
- 纯文档 / 注释 / 文案（`*.md`、docstring、注释）；
- 配置文件（`pyproject.toml`、`.github/workflows/*.yml`、`models_config.json` 等）。

**判断标准是「预判」**：动手前看一眼就知道要碰几个文件。改到一半发现要扩到第
2 个文件？**就地开 worktree 重来**，别硬着头皮在主工作区继续——已经改了的那点
东西用 `git stash push -- <具体文件>` 挑出来带走。

---

## 提交前：别把别人的改动带进来

`git add -A` **不要用**。这个仓库的工作区经常有别人的在途改动。一律显式列出
自己的文件：

```bash
git add src/buddy_proxy/kiro/provider.py tests/test_kiro_provider.py
git status --short          # 确认暂存区里只有自己的文件
```

同理 `git stash` 也别裸用——`git stash push -u -m "xxx wip (not mine)"` 明确
说明这是别人的东西时，恢复回来才知道该还原给谁。

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -q          # 全量（~2700 条，80 秒左右）
.venv/bin/python -m pytest tests/test_xxx.py -q  # 单文件，改的时候反复跑
.venv/bin/python -m ruff check src tests
```

**新通道 / 新功能必须带测试**，而且要想清楚「哪个断言能在实现退化时立刻失败」。

反面教材（真的发生过）：kiro 通道第一版写了 49 条测试，**全是单轮对话**。
结果 `history` 压根没发给上游，模型完全失忆——用户聊两句就发现了，49 条测试
一条都没拦住。后来补的 `test_messages_to_conversation_returns_history` 直接锁
函数签名，改回旧形状当场红。

---

## 接入新 provider 的检查清单

按 `src/buddy_proxy/kiro/` 或 `src/buddy_proxy/antigravity/` 的结构抄，别从零
发明。**这七项少一项就是半成品**：

1. **单轮能通** —— 最基本的 chat 往返
2. **多轮记得上下文** —— `history` 真的进了请求体（**最容易漏**，见上）
3. **工具调用** —— agent 类客户端（dsh / Claude Code / Codex）全靠它；
   `tools` 丢掉的表现是「跑一次就停」，不是报错
4. **流式 + 非流式** —— 两条路都要测
5. **三种协议** —— `openai` / `anthropic` / `responses`（上游没有的要在
   501 里说清楚改走哪条）
6. **token 与积分** —— usage 字段要能被 `metrics_protocol.normalize_usage`
   读到，否则日志的 token/积分列永远是空
7. **认证过期后的行为** —— 刷新失败要给可操作提示（「去 IDE 重新登录」），
   不是裸异常

---

## 注释：只写「为什么」

代码里凡是看起来「可以更简单」的地方，通常都有原因。**把原因写下来**，否则下
一个人会「优化」掉它，然后重新踩一遍坑。

值得写的：

- 上游的反直觉约束（`User-Agent` 必须带 `Kiro/1.4.121`，否则 403 说「订阅不
  支持」；请求体带 `editorState` 回 400）
- 顺序依赖（先 `_demote_developer_role` 再 `_neutralize_leading_system`）
- 标定值从哪来（`_KIRO_SYSTEM_OVERHEAD_TOKENS = 4233` 是四点实测标定的，
  改之前要重跑标定）
- **不要**写「这个函数返回 xxx」——名字和类型标注已经说了

反面教材：kiro 的 `_messages_to_conversation` 原来的 docstring 写着「其余的
按序进 history」，但代码里 **history 根本没返回**。描述了一个不存在的实现，
比没有注释更误导。

---

## 日志与凭据

- 凭据一律 0600，走 `core/account_store`（原子写 + flock），别自己造轮子
- **日志里不许出现 token / secret / cookie**。要区分账号就记 hash 前缀或
  account_id
- 上游错误要落 `upstream_error`（截断 + sha256），别整段 body 打出来

---

## UI 改动

静态资源（`web/static/*`）改完**必须重启**才生效，而且浏览器可能还缓存着旧
JS——`web/ui/page.py` 的 `_stamp_assets` 会给 `/ui` 的资源加 `?v=<mtime>`，但
手工打开的旧标签页仍需硬刷新。

改 `index.html` 里的面板顺序时，`tests/test_benefits_panel_order.py` 会拦你：
**新增面板必须同步那个 EXPECTED_ORDER**，否则顺序契约测试红。

---

## 提交前自检清单

- [ ] `pytest tests/ -q` 全绿
- [ ] `ruff check src tests` 干净
- [ ] `git status --short` 的暂存区只有自己的文件
- [ ] 新功能带测试，且测试能在实现退化时失败
- [ ] 反直觉的地方写了「为什么」注释
- [ ] 多轮 + 工具调用都手工验过（如果动了 provider）
- [ ] PR body 说清根因和踩坑，不只是 diff 摘要
