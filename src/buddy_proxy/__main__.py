"""Local OpenAI/Responses/Anthropic compatible proxy for CodeBuddy.

High-performance async implementation with FastAPI + httpx.

Features:
- High concurrency: 1000+ concurrent requests
- Low memory footprint: ~5KB per request
- Robust timeout handling with async iterators

Usage with uv:
    uv run python -m buddy_proxy
    uv run python -m buddy_proxy --desensitize
    uv run python -m buddy_proxy --host 0.0.0.0 --port 8787

模块结构（2026-09 拆分）：
- ``logging_setup``  日志配置与运行时信息工具
- ``state``          全局状态（ProxyState、app）
- ``model_list``     模型列表构建（本地配置 + Codex 格式）
- ``metrics``        请求指标收集（/ui 图表数据源）
- ``settings``       管理页设置持久化（默认启用模型等）
- ``codebuddy_client`` 默认 CodeBuddy 上游客户端
- ``codebuddy_provider`` 默认 CodeBuddy 上游与核心转发逻辑
- ``routes``         所有 /v1/* 代理路由
- ``ui``             /ui 管理页与管理接口
- ``__main__``       本文件：仅保留启动入口 main()
"""

from __future__ import annotations

import argparse
import os
import pathlib

import uvicorn
from fastapi import HTTPException

from buddy_proxy.codebuddy_provider.client import CodeBuddyClient
from buddy_proxy.providers.base import BaseProvider
from buddy_proxy.doubao.provider import DoubaoProvider
from buddy_proxy.core.logging_setup import setup_logging, setup_json_logging
from buddy_proxy.core.metrics import MetricsCollector
from buddy_proxy.benefits import BenefitsManager
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core.paths import ensure_state_dir
from buddy_proxy.core.state import ProxyState, app
from buddy_proxy.core.settings import normalize_default_model

# 导入 routes / ui 以注册所有 @app 路由（副作用导入）
from buddy_proxy.web import routes as _routes  # noqa: F401
from buddy_proxy.web import ui as _ui  # noqa: F401


_DEFAULT_ENV = pathlib.Path(__file__).resolve().parents[2] / ".env"


def _load_dotenv(path: pathlib.Path | str | None = None) -> None:
    """读取仓库根 .env（若存在）注入环境变量，已有值不覆盖。

    默认路径锚定在仓库根（由模块位置推导，与启动时 CWD 无关）；
    也可用参数显式指定。用于 PAT 通道等本地私有配置（密钥/端点不入库，
    见 .token.md）。格式：KEY=VALUE，支持 # 注释与引号包裹值；
    VALUE 以 ``[``/``{`` 开头时连续收集后续行直到括号闭合，可写多行 JSON
    （如 TRAE_PAT_BEARER_PROFILES）。
    注意：仅在 main() 里、所有模块 import 完成后调用——仅 import 期读取的
    环境变量不受本函数影响。
    """
    f = pathlib.Path(path) if path else _DEFAULT_ENV
    if not f.exists():
        return
    pairs: list[tuple[str, str]] = []
    pending_key: str | None = None
    pending_value: list[str] = []
    openers, closers = "[{", "]}"
    depth = 0
    in_string = False  # 是否处于 JSON 双引号字符串内（跨行保持）
    escaped = False     # 上一个字符是否为字符串内的反斜杠转义

    def scan_depth(text: str, depth: int) -> int:
        """按 JSON 词法累计括号深度：只数字符串**外**的 []{}，忽略串内的括号。

        token（如 bearer）值里含 } / { 时不再误判闭合；转义 \\" 不当作串结束。
        """
        nonlocal in_string, escaped
        for ch in text:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in openers:
                depth += 1
            elif ch in closers:
                depth -= 1
        return depth

    def flush() -> None:
        nonlocal pending_key, pending_value, depth, in_string, escaped
        if pending_key is not None:
            pairs.append((pending_key, "\n".join(pending_value)))
        pending_key, pending_value, depth = None, [], 0
        in_string, escaped = False, False

    for line in f.read_text("utf-8").splitlines():
        line = line.strip()
        if pending_key is not None:
            # 续行原样收集（串内可能含 #，不能按注释跳过）；深度按词法累计。
            pending_value.append(line)
            depth = scan_depth(line, depth)
            if depth <= 0 and not in_string:
                flush()
            continue
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value and value[0] in openers:
            pending_key, pending_value = key, [value]
            depth = scan_depth(value, 0)
            if depth <= 0 and not in_string:
                flush()
            continue
        if key:
            pairs.append((key, value))
    flush()
    for key, value in pairs:
        if key and key not in os.environ:
            os.environ[key] = value


async def _qoder_model_refresh_loop(provider) -> None:
    """qoder 模型目录常驻刷新循环：先刷一次再按 CACHE_TTL_S 节奏重复。

    失败只 log 不退出——下一轮目录刷新自会补上；循环随进程生命周期常驻。
    """
    import asyncio
    import logging

    from buddy_proxy.qoder.catalog import CACHE_TTL_S

    log = logging.getLogger(__name__)
    while True:
        try:
            models = await provider.refresh_models()
            log.info("qoder 模型目录已刷新（%d 个模型）", len(models))
        except Exception as exc:  # noqa: BLE001 - 目录刷新失败不影响转发
            log.warning("qoder 模型目录刷新失败: %s", exc)
        await asyncio.sleep(CACHE_TTL_S)


def _install_qoder_model_warmup(provider) -> None:
    """把目录预热挂到应用启动（fire-and-forget 常驻任务，随进程生命周期）。"""
    import asyncio

    async def _start() -> None:
        asyncio.create_task(_qoder_model_refresh_loop(provider))  # noqa: RUF006

    app.router.on_startup.append(_start)


def main():
    import buddy_proxy.core.state as _state

    _load_dotenv()
    # 首次运行先把状态目录建出来（~/.buddy-proxy/）：新用户装完没有任何
    # 状态落点，容易误以为没装好；后续 settings/凭证/客户端名都写这里。
    state_path = ensure_state_dir()

    parser = argparse.ArgumentParser(description="CodeBuddy local API proxy")
    parser.add_argument("--host", default=os.getenv("BUDDY_PROXY_HOST", "127.0.0.1"),
                        help="监听地址")
    parser.add_argument("--port", type=int, default=int(os.getenv("BUDDY_PROXY_PORT", "8787")),
                        help="监听端口")
    parser.add_argument("--endpoint", default=os.getenv("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com"),
                        help="CodeBuddy 后端地址")
    parser.add_argument("--session-file", type=pathlib.Path,
                        help="会话文件路径")
    parser.add_argument("--mock-dir", type=pathlib.Path,
                        help="只使用指定目录中的真实响应 fixture，不访问 CodeBuddy 后端")
    # 默认日志目录：项目根下的 logs/（相对 __file__ 定位，部署后路径稳定）
    _default_log_dir = pathlib.Path(__file__).resolve().parents[2] / "logs"
    default_log_file = pathlib.Path(
        os.getenv(
            "BUDDY_PROXY_LOG_FILE",
            str(_default_log_dir / "buddy-proxy.jsonl"),
        )
    ).expanduser()
    parser.add_argument(
        "--log-file",
        type=pathlib.Path,
        default=default_log_file,
        help="记录完整请求/响应的 JSONL 文件（默认 <项目根>/logs/buddy-proxy.jsonl，可用 BUDDY_PROXY_LOG_FILE 覆盖）",
    )
    parser.add_argument("--desensitize", action="store_true",
                        help="启用脱敏处理，对 system 消息中的敏感词插入零宽空格（缓解审核误拦）")
    parser.add_argument("--optimize-context", action="store_true",
                        help="启用消息压缩优化（仅 /v1/responses），大幅减少 token 使用（适用于 Codex CLI 等长上下文场景）")
    parser.add_argument("--login", action="store_true",
                        help="启动时执行浏览器登录/账户查询")
    parser.add_argument("--no-browser", action="store_true",
                        help="登录时不自动打开浏览器")
    parser.add_argument("--verbose-llm", action="store_true",
                        help="输出扩展安全诊断（绝不记录请求/响应体、token 或 UID）")
    parser.add_argument("--default-model", default=None,
                        help="默认启用模型，形如 zcode/glm-5.3（或裸 glm-5.3 按路由自动匹配通道）；"
                             "客户端请求未带 model 字段时使用。"
                             "首次启动时写入设置文件，此后以 ~/.buddy-proxy/settings.json（管理页可改）为准")
    parser.add_argument("--doubao", action="store_true", default=os.getenv("DOUBAO_ENABLED", "") == "1",
                        help="启用豆包 provider（自研内联，直连豆包工作 App CDP）")
    parser.add_argument("--trae", action="store_true", default=os.getenv("TRAE_ENABLED", "") == "1",
                        help="启用 Trae provider（解密 Trae IDE 登录态，直连底层模型）")
    parser.add_argument("--zcode", action="store_true", default=os.getenv("ZCODE_ENABLED", "") == "1",
                        help="启用 Zcode provider（智谱 GLM，Anthropic 端点直通；"
                             "凭据取 ZCODE_API_KEY / secrets / ~/.zcode/v2/config.json）")
    parser.add_argument("--glm", action="store_true", default=os.getenv("GLM_ENABLED", "") == "1",
                        help="启用 GLM 官方 provider（智谱 BigModel Coding Plan 官方 key，"
                             "Anthropic 端点直通，与 zcode 同上游但凭据独立；"
                             "凭据取 GLM_API_KEY / ~/.buddy-proxy/glm_api_key）")
    parser.add_argument("--mimo", action="store_true", default=os.getenv("MIMO_ENABLED", "") == "1",
                        help="启用 MiMo provider（小米 MiMo 桌面端；"
                             "凭据取 MIMO_API_KEY 或复用桌面端小米账号登录态）")
    parser.add_argument("--qoder", action="store_true", default=os.getenv("QODER_ENABLED", "") == "1",
                        help="启用 Qoder provider（Qwen3.8-Max/Flash、DeepSeek、GLM、Kimi 等；"
                             "凭据跑 `buddy login qoder` 或设 QODER_TOKEN，"
                             "海外账号走独立通道 qoderintl；QODER_REGION 仅作默认区域回退）")
    parser.add_argument("--gemini", action="store_true", default=os.getenv("GEMINI_ENABLED", "") == "1",
                        help="启用 Gemini provider（Google Code Assist 免费通道，OAuth 登录；"
                             "凭据跑 `buddy login gemini`）")
    parser.add_argument("--antigravity", action="store_true", default=os.getenv("ANTIGRAVITY_ENABLED", "") == "1",
                        help="启用 Antigravity provider（Google Antigravity 免费通道，Gemini 3.x / Claude / GPT-OSS；"
                             "凭据跑 `buddy login antigravity`）")
    parser.add_argument("--kimi", action="store_true", default=os.getenv("KIMI_ENABLED", "") == "1",
                        help="启用 Kimi provider（Kimi Code 订阅通道，kimi cli 同款 OAuth；"
                             "凭据跑 `buddy login kimi` 或在管理面板导入 token JSON）")
    parser.add_argument("--codex", action="store_true", default=os.getenv("CODEX_ENABLED", "") == "1",
                        help="启用 Codex provider（ChatGPT 订阅通道，OpenAI OAuth 登录，上游 Responses 协议；"
                             "凭据跑 `buddy login codex` 或在管理面板导入 ~/.codex/auth.json）")
    parser.add_argument("--dumate", action="store_true", default=os.getenv("DUMATE_ENABLED", "") == "1",
                        help="启用 DuMate provider（百度搭子/千帆桌面端本地代理；需本机已安装并登录 DuMate.app 且在运行）")
    parser.add_argument("--zcode-start", action="store_true", default=os.getenv("ZCODE_START_ENABLED", "") == "1",
                        help="启用 ZCode Start Plan (Trust Build) provider（glm-5.3-flash，凭据自动从 ~/.zcode/cli/config.json 读取）")
    parser.add_argument("--default-provider", default=os.getenv("PROXY_DEFAULT_PROVIDER", "codebuddy"),
                        help="兜底通道：模型名未命中任何 provider 时转发到哪个通道 "
                             "（codebuddy/zcode/trae/doubao/mimo/qoder，默认 codebuddy；可用 PROXY_DEFAULT_PROVIDER 覆盖）")
    args = parser.parse_args()
    args.log_file = args.log_file.expanduser()
    # provider 别名归一：workbuddy 是 codebuddy 的旧称，用户配置/命令行都可能敲
    args.default_provider = {"workbuddy": "codebuddy"}.get(args.default_provider, args.default_provider)

    # 设置日志
    log_dir = args.log_file.parent if args.log_file else pathlib.Path("logs")
    logger = setup_logging(log_dir)
    json_logger = setup_json_logging(args.log_file)

    # 初始化客户端
    client = CodeBuddyClient(args.endpoint, session_file=args.session_file)

    # 处理登录
    if args.login:
        client.login(open_browser=not args.no_browser)

    # 初始化额外 provider（ZCode/豆包/Trae 等）
    providers: dict[str, BaseProvider] = {}
    # ⚠️ zcode 必须**最先**注册：trae 的 T1 tier 也提供 glm-5.3 / glm-5.3-flash
    # （小写 id），若 trae 先注册，forward_chat 自动匹配会先命中 trae，glm-*
    # 请求就落不到 zcode coding-plan 通道。providers dict 按插入序遍历匹配。
    if args.zcode:
        from buddy_proxy.providers.zcode import ZcodeProvider

        zcode = ZcodeProvider()
        try:
            zcode.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("zcode provider 认证未就绪: %s", exc.detail)
        providers[zcode.id] = zcode
        logger.info("Zcode provider enabled (%s)", zcode.health().get("base_url"))
        print("[Zcode] Enabled (BigModel GLM)")
    else:
        print("[Zcode] Disabled (pass --zcode or ZCODE_ENABLED=1 to enable)")

    # glm 与 zcode 同上游同模型表：注册在 zcode 之后，裸 glm-* 自动匹配仍
    # 先落 zcode（两条通道都能接），显式 glm/<模型> 前缀才定向到官方 key。
    if args.glm:
        from buddy_proxy.providers.glm import GlmProvider

        glm = GlmProvider()
        try:
            glm.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("glm provider 认证未就绪: %s", exc.detail)
        providers[glm.id] = glm
        logger.info("Glm provider enabled (%s)", glm.health().get("base_url"))
        print("[GLM] Enabled (BigModel Coding Plan 官方 key)")
    else:
        print("[GLM] Disabled (pass --glm or GLM_ENABLED=1 to enable)")

    if args.mimo:
        from buddy_proxy.mimo.provider import MimoProvider

        mimo = MimoProvider()
        try:
            mimo.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("mimo provider 认证未就绪: %s", exc.detail)
        providers[mimo.id] = mimo
        logger.info("Mimo provider enabled (%s)", mimo.health().get("auth_mode"))
        print("[MiMo] Enabled (Xiaomi MiMo Desktop)")
    else:
        print("[MiMo] Disabled (pass --mimo or MIMO_ENABLED=1 to enable)")

    if args.qoder:
        from buddy_proxy.qoder.provider import QoderProvider

        qoder = QoderProvider()
        try:
            qoder.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("qoder provider 认证未就绪: %s", exc.detail)
        providers[qoder.id] = qoder
        _qh = qoder.health()
        logger.info("Qoder provider enabled (%s / %s 个账号)",
                    _qh.get("base_url"), len(_qh.get("accounts") or []))
        print(f"[Qoder] Enabled ({qoder.region().label} / {len(_qh.get('accounts') or [])} 个账号)")

        # 模型目录预热 + 周期刷新：目录是账号级分桶且纯内存（重启即丢），不预热
        # 的话启动后列表回落到本地兜底表、首次转发前 catalog 一直是空的。
        _install_qoder_model_warmup(qoder)

        # 海外通道（有 global 区账号时启用）：qoder 的双区域机制本就完整，这里
        # 只是把 global 钉成一个**独立 id** 的通道，让它与 CN 同时在线、各自
        # 路由（``qoderintl/模型``）、/ui 各一张额度卡——CN 与海外的签到、目录、
        # 计费互不混淆。条件注册见 qoder.intl_provider.intl_enabled()。
        from buddy_proxy.qoder.intl_provider import QoderIntlProvider, intl_enabled
        if qoder.region().key == "global":
            # QODER_REGION=global 时主通道**已经**是海外版，再挂一个 qoderintl
            # 就是两个同区域通道抢同一批账号（额度卡也会重复）。
            print("[Qoder Intl] Skipped (主通道已是海外版：QODER_REGION=global)")
        elif intl_enabled():
            qoder_intl = QoderIntlProvider()
            try:
                qoder_intl.ensure_auth()
            except HTTPException as exc:
                logger.warning("qoderintl provider 认证未就绪: %s", exc.detail)
            providers[qoder_intl.id] = qoder_intl
            logger.info("Qoder 海外版 provider enabled")
            print("[Qoder Intl] Enabled (qoderintl/*)")
    else:
        print("[Qoder] Disabled (pass --qoder or QODER_ENABLED=1 to enable)")

    if args.gemini:
        from buddy_proxy.gemini.provider import GeminiProvider

        gemini = GeminiProvider()
        try:
            gemini.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("gemini provider 认证未就绪: %s", exc.detail)
        providers[gemini.id] = gemini
        _gh = gemini.health()
        logger.info("Gemini provider enabled (%s / %s)",
                    _gh.get("email"), _gh.get("project_id"))
        print(f"[Gemini] Enabled ({_gh.get('email') or '未登录'} / {_gh.get('tier') or '?'})")
    else:
        print("[Gemini] Disabled (pass --gemini or GEMINI_ENABLED=1 to enable)")

    if args.antigravity:
        from buddy_proxy.antigravity.provider import AntigravityProvider

        antigravity = AntigravityProvider()
        try:
            antigravity.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("antigravity provider 认证未就绪: %s", exc.detail)
        providers[antigravity.id] = antigravity
        _ah = antigravity.health()
        logger.info("Antigravity provider enabled (%s / %s)",
                    _ah.get("email"), _ah.get("project_id"))
        print(f"[Antigravity] Enabled ({_ah.get('email') or '未登录'} / {_ah.get('tier') or '?'})")
    else:
        print("[Antigravity] Disabled (pass --antigravity or ANTIGRAVITY_ENABLED=1 to enable)")

    if args.kimi:
        from buddy_proxy.kimi.provider import KimiProvider

        kimi = KimiProvider()
        try:
            kimi.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("kimi provider 认证未就绪: %s", exc.detail)
        providers[kimi.id] = kimi
        _kh = kimi.health()
        logger.info("Kimi provider enabled (%s / %s)",
                    _kh.get("nickname") or _kh.get("user_id"), _kh.get("base_url"))
        print(f"[Kimi] Enabled ({_kh.get('nickname') or '未登录'} / {len(_kh.get('accounts') or [])} 个账号)")
    else:
        print("[Kimi] Disabled (pass --kimi or KIMI_ENABLED=1 to enable)")

    if args.codex:
        from buddy_proxy.codex.provider import CodexProvider

        codex = CodexProvider()
        try:
            codex.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("codex provider 认证未就绪: %s", exc.detail)
        providers[codex.id] = codex
        _ch = codex.health()
        logger.info("Codex provider enabled (%s / %s / %s)",
                    _ch.get("email"), _ch.get("plan_type"), _ch.get("accounts"))
        print(f"[Codex] Enabled ({_ch.get('email') or '未登录'} / "
              f"{_ch.get('plan_type') or '?'} / {_ch.get('accounts') or 0} 个账号)")
    else:
        print("[Codex] Disabled (pass --codex or CODEX_ENABLED=1 to enable)")

    if args.doubao:
        doubao = DoubaoProvider()
        providers[doubao.id] = doubao
        logger.info("Doubao provider enabled (CDP)")
        print("[Doubao] Enabled (CDP)")
    else:
        print("[Doubao] Disabled (pass --doubao or DOUBAO_ENABLED=1 to enable)")

    if args.dumate:
        from buddy_proxy.dumate.provider import DumateProvider

        dumate = DumateProvider()
        providers[dumate.id] = dumate
        logger.info("Dumate provider enabled (local proxy)")
        print("[DuMate] Enabled (local proxy)")
    else:
        print("[DuMate] Disabled (pass --dumate or DUMATE_ENABLED=1 to enable)")

    if args.zcode_start:
        from buddy_proxy.providers.zcode_start import ZCodeStartPlanProvider

        zcode_start = ZCodeStartPlanProvider()
        try:
            zcode_start.ensure_auth()  # 启动时校验凭证，给出清晰的配置提示
        except HTTPException as exc:
            logger.warning("zcode-start provider 认证未就绪：%s", exc.detail)
        providers[zcode_start.id] = zcode_start
        logger.info("ZCode Start Plan provider enabled (%s)", zcode_start.health().get("base_url"))
        print("[ZCode Start Plan] Enabled (glm-5.3-flash)")
    else:
        print("[ZCode Start Plan] Disabled (pass --zcode-start or ZCODE_START_ENABLED=1 to enable)")

    if args.trae:
        from buddy_proxy.trae.provider import TraeProvider

        # 主通道钉死 CN：海外版由下面条件注册的 traeintl 独立承担（各自 id、
        # 各自额度卡）。TRAE_REGION 只作 login / legacy 凭据的默认区域回退，
        # 不再切换主通道——否则会与 traeintl 重复出两个海外通道。
        trae = TraeProvider(region="cn")
        providers[trae.id] = trae
        logger.info("Trae provider enabled")
        print("[Trae] Enabled")

        # 海外通道（有 global 区账号时启用）：chat 网关 / 模型目录 / 额度口径
        # 全按 global，无签到。条件注册见 intl_provider.intl_enabled()。
        from buddy_proxy.trae.intl_provider import TraeIntlProvider, intl_enabled
        if intl_enabled():
            trae_intl = TraeIntlProvider()
            try:
                trae_intl.ensure_auth()  # 同 qoderintl：认证未就绪只告警不拦注册
            except HTTPException as exc:
                logger.warning("traeintl provider 认证未就绪: %s", exc.detail)
            providers[trae_intl.id] = trae_intl
            logger.info("Trae 海外版 provider enabled")
            print("[Trae Intl] Enabled (traeintl/*)")

        # PAT 独立通道（配置了 TRAE_PAT_BEARER 时启用）：服务账号的扩展模型目录，
        # 与个人 trae 通道分开路由/计额（/ui 额度 Tab 各自展示）
        from buddy_proxy.trae.pat import pat_enabled
        if pat_enabled():
            from buddy_proxy.trae.pat_provider import TraePatProvider

            trae_pat = TraePatProvider()
            try:
                trae_pat.ensure_auth()  # 本地配置校验 + 拉起后台凭证自愈循环
            except HTTPException as exc:
                logger.warning("trae PAT provider 认证未就绪: %s", exc.detail)
            providers[trae_pat.id] = trae_pat
            logger.info("Trae PAT provider enabled")
            print("[Trae PAT] Enabled (traepat/*)")
    else:
        print("[Trae] Disabled (pass --trae or TRAE_ENABLED=1 to enable)")

    # codebuddy 海外通道（有 global 区账号时启用）：与 CN 共用同一账号 store
    #（账号落 region 标），只是换 host 登录/转发——www.codebuddy.ai 与
    # copilot.tencent.com 的 /v2/plugin 协议逐字节同构。条件注册见
    # codebuddy_provider.intl_provider.intl_enabled()。codebuddy 主通道
    # 常开（默认 provider），故这里无条件尝试检测，不挂在某个 flag 下。
    from buddy_proxy.codebuddy_provider.intl_provider import (
        CodeBuddyIntlProvider,
        intl_enabled as _cb_intl_enabled,
    )
    if _cb_intl_enabled():
        cb_intl = CodeBuddyIntlProvider()
        try:
            cb_intl.ensure_auth()  # 同 qoderintl/traeintl：认证未就绪只告警不拦注册
        except HTTPException as exc:
            logger.warning("codebuddyintl provider 认证未就绪: %s", exc.detail)
        providers[cb_intl.id] = cb_intl
        logger.info("CodeBuddy 海外版 provider enabled")
        print("[CodeBuddy Intl] Enabled (codebuddyintl/*)")

    # 管理页设置文件里的兜底通道优先于 CLI/环境变量（UI 改动跨重启生效）
    saved_settings = settings_mod.load_settings()
    if saved_settings.get("default_provider"):
        saved_default = normalize_default_model(saved_settings["default_provider"])
        # 两处配置不一致时必须喊出来。这个覆盖是**静默**的：plist/proxy.sh 里写着
        # ``--default-provider trae``、settings.json 里写着 codebuddy 时，实际生效的
        # 永远是后者，而启动横幅上只看得到结果、看不出「CLI 那份被丢掉了」。
        # 2026-10-21 排查「切不到模型」时正是被这处不一致带偏过。
        if args.default_provider and args.default_provider != saved_default:
            logger.warning(
                "default-provider 不一致：CLI/环境变量为 '%s'，settings.json 为 '%s'；"
                "按「管理页设置优先」采用后者。请把启动参数也改成 '%s'，否则两处会持续漂移。",
                args.default_provider, saved_default, saved_default,
            )
        args.default_provider = saved_default

    # 校验兜底通道：必须是 codebuddy 或已启用的 provider id
    if args.default_provider != "codebuddy" and args.default_provider not in providers:
        logger.warning(
            "default-provider '%s' 未启用，回退为 codebuddy（已启用: %s）",
            args.default_provider, list(providers) or "无",
        )
        args.default_provider = "codebuddy"
    print(f"[Router] Default provider: {args.default_provider}")

    # 创建全局状态
    # 默认模型：CLI --default-model 提供初始值；设置文件里已有的值优先
    default_model = normalize_default_model(args.default_model or "")
    if "default_model" in saved_settings:
        default_model = normalize_default_model(saved_settings.get("default_model") or "")
    if default_model and "default_model" not in saved_settings:
        settings_mod.save_settings({"default_model": default_model})
    if default_model:
        print(f"[Default Model] {default_model} (settings: {settings_mod.settings_path()})")
    print(f"[State] {state_path}")

    # 已停用模型：settings.json 持久化的 "provider/model" 列表，命中即拒绝转发
    disabled_models = {
        settings_mod.model_key(*k.split("/", 1)) if "/" in k else settings_mod.model_key("codebuddy", k)
        for k in (saved_settings.get("disabled_models") or [])
        if isinstance(k, str) and k.strip()
    }
    if disabled_models:
        print(f"[Disabled Models] {len(disabled_models)} 个已停用: {', '.join(sorted(disabled_models))}")

    # 隐藏模型：只从模型目录与管理页摘除，直接点名仍可调用。与 disabled_models
    # 同用 provider/裸名口径，避免 qoder 这类目录 id 自带 provider 前缀时写出双前缀。
    hidden_models = {
        settings_mod.model_key(*k.split("/", 1)) if "/" in k else settings_mod.model_key("codebuddy", k)
        for k in (saved_settings.get("hidden_models") or [])
        if isinstance(k, str) and k.strip()
    }
    if hidden_models:
        print(f"[Hidden Models] {len(hidden_models)} 个已隐藏: {', '.join(sorted(hidden_models))}")

    # 整通道停用：settings.json 的 provider id 列表，命中通道转发 403、/ui 各处
    # 隐藏或置灰（模型页开关写入，用户 2026-10-06 需求）
    disabled_providers = {
        k.strip() for k in (saved_settings.get("disabled_providers") or [])
        if isinstance(k, str) and k.strip()
    }
    if disabled_providers:
        print(f"[Disabled Providers] {len(disabled_providers)} 个通道已停用: "
              f"{', '.join(sorted(disabled_providers))}")

    # 限时可用模型：settings.json 的 {"provider/model": {"windows": [["HH:MM","HH:MM"],...]}}，
    # 窗口外拒绝转发。键规范化 + 窗口校验，非法窗口丢弃、空窗口不入表。
    model_schedules: dict[str, list] = {}
    raw_schedules = saved_settings.get("model_schedules")
    if isinstance(raw_schedules, dict):
        for raw_key, value in raw_schedules.items():
            if not (isinstance(raw_key, str) and raw_key.strip()):
                continue
            key = (settings_mod.model_key(*raw_key.split("/", 1)) if "/" in raw_key
                   else settings_mod.model_key("codebuddy", raw_key))
            windows = value.get("windows") if isinstance(value, dict) else value
            normalized = settings_mod.normalize_windows(windows)
            if normalized:
                model_schedules[key] = normalized
    if model_schedules:
        print(f"[Model Schedules] {len(model_schedules)} 个限时模型: "
              f"{', '.join(sorted(model_schedules))}")

    # 候选上游顺序：settings.json 的 {"provider/model": ["zcode/glm-5.3", ...]}，
    # 命中时按序尝试、未提交即失败才换下一个（见 forward.py）。键归一成 model_key
    # 口径；通道未启用的目标直接丢弃并告警——留着只会变成一条永远被静默跳过的条目。
    model_order: dict[str, list[str]] = {}
    raw_order = saved_settings.get("model_order")
    if isinstance(raw_order, dict):
        for raw_key, value in raw_order.items():
            if not (isinstance(raw_key, str) and raw_key.strip()):
                continue
            key = settings_mod.normalize_order_key(raw_key)
            targets = []
            for target in settings_mod.normalize_order(value):
                pid = target.split("/", 1)[0] if "/" in target else "codebuddy"
                if pid != "codebuddy" and pid not in providers:
                    print(f"[Model Order] {key} 的目标 {target} 通道未启用，已忽略")
                    continue
                targets.append(target)
            if key and targets:
                model_order[key] = targets
    if model_order:
        print(f"[Model Order] {len(model_order)} 个模型配置候选顺序: "
              f"{', '.join(sorted(model_order))}")

    metrics = MetricsCollector(args.log_file.parent / "metrics.jsonl")

    _state.proxy_state = ProxyState(
        client=client,
        mock_dir=args.mock_dir,
        log_file=args.log_file,
        enable_desensitize=args.desensitize,
        enable_optimize_context=args.optimize_context,
        verbose_llm=args.verbose_llm,
        logger=logger,
        json_logger=json_logger,
        providers=providers,
        default_provider=args.default_provider,
        default_model=default_model or None,
        disabled_models=disabled_models,
        hidden_models=hidden_models,
        disabled_providers=disabled_providers,
        model_schedules=model_schedules,
        model_order=model_order,
        metrics=metrics,
        interactive_login=args.login,
    )
    # 打卡管理器要引用 state 本身，构造后挂上
    _state.proxy_state.benefits = BenefitsManager(
        args.log_file.parent / "checkin.jsonl", _state.proxy_state
    )
    _state.proxy_state.write_log(
        "startup",
        host=args.host,
        port=args.port,
    )
    logger.info(
        "Runtime: app_version=%s system_version=%s python_version=%s machine=%s",
        _state.proxy_state.runtime_info["app_version"],
        _state.proxy_state.runtime_info["system_version"],
        _state.proxy_state.runtime_info["python_version"],
        _state.proxy_state.runtime_info["machine"],
    )

    # 启动信息输出到 stdout
    print(f"CodeBuddy proxy listening on http://{args.host}:{args.port}")
    print("Endpoints: /ui /v1/models /v1/chat/completions /v1/responses /v1/messages /health")
    print(f"Admin UI: http://127.0.0.1:{args.port}/ui")

    # 同时记录到日志
    logger.info(f"CodeBuddy proxy listening on http://{args.host}:{args.port}")
    logger.info("Endpoints: /ui /v1/models /v1/chat/completions /v1/responses /v1/messages /health")

    # 启动 uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
