"""全局测试夹具。

各通道凭证相关的写盘路径必须**默认隔离**，不许测试碰真实用户配置：

- `GEMINI_OAUTH_JSON`（buddy 侧 gemini 凭据文件）和 `GEMINI_CLI_HOME`
  （本机 gemini CLI 配置目录，cli_bridge 的读写都认它）。曾经吃过亏——
  test_resume_onboarding_uses_saved_token 只隔离了前者，互通功能给
  resume_onboarding 挂上「回写 ~/.gemini」后，跑一轮全量测试就把假登录态
  （token "tok"、邮箱 a@b.c）写进了真实 ~/.gemini，`buddy login gemini`
  差点把假 token 当真用。
- antigravity 多账号状态（`ANTIGRAVITY_STATE_DIR`，index.json + 每账号
  cred 文件都在里面）与历史单账号文件（`ANTIGRAVITY_OAUTH_JSON`，迁移源）
  同理：登录/onboarding/quota 测试都会落盘。
- kimi 多账号状态（`KIMI_STATE_DIR`）同理：登录/导入/quota 测试都会落盘。
- qoder 多账号状态（`QODER_STATE_DIR`，index.json + 每账号 cred 文件）与
  历史单账号迁移源（`QODER_AUTH_FILE`）同理：登录/quota/迁移测试都会落盘。
- codebuddy 多账号状态（`CODEBUDDY_STATE_DIR`，index.json + 每账号 cred 文件）
  与历史单账号迁移源（`CODEBUDDY_LEGACY_SESSION`，即旧 ~/.codebuddy-session.json）
  同理：登录/quota/签到/迁移测试都会落盘或读盘，必须隔离防串真实账号。
- codex 多账号状态（`CODEX_STATE_DIR`，index.json + 每账号 cred 文件）同理：
  登录/导入/刷新/quota/转发测试都会落盘。注意它**不**读本机 ~/.codex/auth.json
  （那份只在显式导入时读），所以这里只需要隔离我们自己的目录。

个别测试想自定路径时在测试体内再 setenv 覆盖即可（autouse fixture 先跑，
测试体里的 monkeypatch.setenv 后生效）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_gemini_state_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_OAUTH_JSON", str(tmp_path / "gemini_oauth.json"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini-cli-home"))
    monkeypatch.setenv("ANTIGRAVITY_STATE_DIR", str(tmp_path / "antigravity"))
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "antigravity_oauth.json"))
    monkeypatch.setenv("KIMI_STATE_DIR", str(tmp_path / "kimi"))
    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    monkeypatch.setenv("QODER_AUTH_FILE", str(tmp_path / "qoder_auth.json"))
    # trae work 多账号状态（`TRAE_WORK_STATE_DIR`，index.json + 每账号 cred
    # 文件）与历史单账号迁移源（`TRAE_WORK_CRED_PATH`，即旧 trae_work.json）
    # 同理：迁移/签到/额度/转发测试都会落盘或读盘，必须隔离防串真实账号。
    monkeypatch.setenv("TRAE_WORK_STATE_DIR", str(tmp_path / "trae"))
    monkeypatch.setenv("TRAE_WORK_CRED_PATH", str(tmp_path / "trae_work.json"))
    # codebuddy 多账号状态与历史单账号迁移源（见模块 docstring）
    monkeypatch.setenv("CODEBUDDY_STATE_DIR", str(tmp_path / "codebuddy"))
    monkeypatch.setenv("CODEBUDDY_LEGACY_SESSION",
                       str(tmp_path / "codebuddy_legacy_session.json"))
    # codex 多账号状态（见模块 docstring）
    monkeypatch.setenv("CODEX_STATE_DIR", str(tmp_path / "codex"))
