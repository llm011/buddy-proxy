"""统一登录入口：为各上游 provider 执行一次交互式登录/授权。

用法（一般通过 proxy.sh 调用，也可直接运行）：

    python -m buddy_proxy.auth.login codebuddy           # CodeBuddy（腾讯）浏览器授权
    python -m buddy_proxy.auth.login workbuddy           # 同 codebuddy（workbuddy 是其别名）
    python -m buddy_proxy.auth.login trae                # Trae Work (SOLO)：浏览器登录后粘贴回调链接
    python -m buddy_proxy.auth.login traeintl            # 同 trae --region global（海外版；账号落盘后 traeintl 通道自动注册）
    python -m buddy_proxy.auth.login zcode               # 检查并打印 zcode 凭据配置指引（API key，无交互登录）
    python -m buddy_proxy.auth.login doubao              # 打印豆包（CDP）说明
    python -m buddy_proxy.auth.login mimo                # 小米账号浏览器登录（同 qoder 的 device flow）
    python -m buddy_proxy.auth.login gemini              # Google OAuth（Gemini 免费通道，与 ~/.gemini 登录态互通）
    python -m buddy_proxy.auth.login antigravity         # Google OAuth（Antigravity 免费通道，Gemini/Claude/GPT 多模型；检测到本机 agy 登录态可直接导入）
    python -m buddy_proxy.auth.login kimi                # Kimi Code device flow（浏览器授权；kimi cli 导出的 token JSON 可在管理面板导入）

可选参数：
    --no-browser    codebuddy 登录不自动打开浏览器，只打印授权链接

注意：登录态写入的凭据文件与网关共享（如 ~/.codebuddy-session.json）。
登录成功后若网关正在运行，需要 ``proxy.sh restart`` 才会加载新会话——
网关进程在启动时把 session 读进了内存，不会感知文件变化。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from buddy_proxy.core.browser import open_external_url

# 别名归一：workbuddy 是 codebuddy 的旧称/别称（腾讯 WorkBuddy 同一产品线），
# 用户两种名字都可能敲，这里统一映射。
PROVIDER_ALIASES: dict[str, str] = {
    "workbuddy": "codebuddy",
    "cb": "codebuddy",
    # Qoder 常被敲成 quoder/qodor/qder（用户拼写变体），一并归一。
    "quoder": "qoder",
    "qodor": "qoder",
    "qder": "qoder",
    "qodercn": "qoder",
    "qoder-cn": "qoder",
    # 通道 id 是 gemini-cli（/v1/models 前缀），登录命令两写等价。
    "gemini-cli": "gemini",
    # traeintl/qoderintl 是**服务端通道 id**（海外版 provider），不是登录入口——
    # 海外版没有独立账号体系（共用 work 账号池、按 region 落盘过滤）。但
    # 「buddy login traeintl」是最直觉的敲法，按别名接住：落到主 provider 并
    # 隐含 region=global（见 _resolve_provider_region 的冲突检查）。
    "traeintl": "trae",
    "qoderintl": "qoder",
    "codebuddyintl": "codebuddy",
}

KNOWN_PROVIDERS = ("codebuddy", "trae", "zcode", "doubao", "dumate", "mimo", "qoder", "gemini", "antigravity", "kimi")

#: ``--region`` 的取值归一：国内/海外的各种常见写法都认。
_REGION_ALIASES = {
    "cn": "cn", "china": "cn", "domestic": "cn", "guonei": "cn", "国内": "cn", "国内版": "cn",
    "global": "global", "intl": "global", "international": "global", "oversea": "global",
    "overseas": "global", "sg": "global", "海外": "global", "海外版": "global", "国际": "global",
    "国际版": "global",
}


def _pick_region(provider_label: str, cli_region: str | None, *, default_key: str) -> str:
    """决定登录哪个区域：``--region`` 显式 > 交互 prompt > 默认区域。

    CN 与海外账号**互不通用**（连错域必 401），而区域一旦选错，登录会拿到
    另一套 uid、额度/签到/chat 全打错域，事后很难察觉——所以交互 tty 下
    宁可多问一句。非交互（管道/CI）时不阻塞，直接用默认区域。
    """
    if cli_region:
        key = _REGION_ALIASES.get(cli_region.strip().lower(), cli_region.strip().lower())
        if key not in ("cn", "global"):
            print(f"[!] 未知区域 {cli_region!r}，按默认 {default_key} 处理", file=sys.stderr)
            return default_key
        return key
    if not sys.stdin.isatty():
        print(f"[{provider_label}] 区域: {default_key}（非交互终端，默认）")
        return default_key
    # default_key 可能是 cn 也可能是 global（qoder 的 QODER_REGION env），
    # 提示与「直接回车」的落点都要跟着它，别把默认写死成 cn 误导用户。
    cn_mark = "  ← 默认，直接回车即选它" if default_key == "cn" else ""
    gl_mark = "  ← 默认，直接回车即选它" if default_key == "global" else ""
    print(f"请选择 {provider_label} 登录区域（两区账号不通用）：")
    print(f"  1) 国内版 (cn){cn_mark}")
    print(f"  2) 海外版 (global){gl_mark}")
    try:
        answer = input("选择 [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default_key
    if answer in ("2", "global", "海外", "海外版"):
        return "global"
    if answer in ("1", "cn", "国内", "国内版"):
        return "cn"
    # 直接回车或无法识别的输入 → 默认区域（而非硬编码 cn）
    return default_key


def _login_codebuddy(open_browser: bool = True, region: str | None = None,
                     **_kwargs) -> int:
    """CodeBuddy 浏览器 OAuth 登录（region=cn/global，默认 cn）。

    区域经 ``_pick_region`` 决定（``--region`` 显式 > 交互 prompt > cn），
    与 trae/qoder 一致——交互终端不给 ``--region`` 会问一句，别静默按 cn
    走：想登海外版的用户拿到一套 CN uid 后很难察觉（两区账号连错域必 401）。
    海外版（www.codebuddy.ai）与 CN 协议同构，只是换 host + 独立 session
    文件：不能写进 CN 的历史 session 路径（~/.codebuddy-session.json），否则
    首次触达的 legacy 迁移会把海外账号并进 CN 区。
    """
    import json

    from buddy_proxy.codebuddy_provider.client import CodeBuddyClient, CodeBuddyError
    from buddy_proxy.codebuddy_provider.credentials import endpoint as cb_endpoint

    region = _pick_region("CodeBuddy", region, default_key="cn")
    endpoint = cb_endpoint(region)
    if region == "global":
        session_file = Path(os.getenv("CODEBUDDY_INTL_SESSION", "")
                            .strip() or str(Path.home() / ".codebuddy-session-global.json"))
        client = CodeBuddyClient(endpoint, session_file=session_file)
    else:
        client = CodeBuddyClient(endpoint)
    # login() 只落盘 session 文件、不更新内存 session，且浏览器超时会静默返回。
    # 因此以「文件前后快照是否变化」判定登录是否真的完成：直接读内存旧会话
    # 会把超时误判成成功（旧 session 里还留着死 token），或把首登误判成失败。
    try:
        before = client.session_file.read_bytes()
    except FileNotFoundError:
        before = b""
    try:
        client.login(open_browser=open_browser)
    except CodeBuddyError as exc:
        print(f"[!] 登录失败: {exc}", file=sys.stderr)
        return 1
    try:
        after = client.session_file.read_bytes()
    except FileNotFoundError:
        after = b""
    if not after or after == before:
        print("[!] 登录未完成：浏览器授权超时或被取消，请重试", file=sys.stderr)
        return 1
    try:
        session = json.loads(after)
    except json.JSONDecodeError:
        print("[!] 登录成功但 session 文件解析失败", file=sys.stderr)
        return 1
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    if not auth.get("accessToken"):
        print("[!] 登录流程结束但 session 中没有 accessToken", file=sys.stderr)
        return 1
    # 同步进多账号 store（幂等 upsert：同 uid 重登只更新凭据、顺位不变；
    # 新账号追加到 failover 末位）。session 文件本身保留——它是 CodeBuddyClient
    # 的历史落盘位置，也是迁移源，删了会破坏「老 session 首次触达自动迁移」。
    try:
        from buddy_proxy.codebuddy_provider import credentials as cb_creds

        cred = cb_creds.session_to_cred(session)
        cred["region"] = region
        ref = cb_creds.save_account_cred(cred)
        accounts = cb_creds.list_accounts()
        pos = next((i + 1 for i, a in enumerate(accounts) if a.id == ref.id), "?")
    except Exception as exc:  # noqa: BLE001 — store 写失败不该掩盖登录成功
        print(f"[!] 登录成功但写入多账号 store 失败: {exc}", file=sys.stderr)
        pos = None
    print()
    tag = "CodeBuddy 海外版" if region == "global" else "CodeBuddy"
    print(f"[OK] {tag} 登录成功（{endpoint}）")
    if account.get("nickname") or account.get("uid"):
        print(f"    账号: {account.get('nickname') or ''} uid={account.get('uid')}")
    if pos is not None:
        print(f"    failover 顺位: #{pos}（共 {len(accounts)} 个账号）")
    print(f"    会话已写入 {client.session_file}")
    return 0


def _login_trae(open_browser: bool = True, region: str | None = None, **_kwargs) -> int:
    """Trae Work (SOLO) 一键登录：自动起本地回调服务 → 打开登录页 → 自动落盘退出。

    trae.cn 授权页（auth_type=local）要求本机 18080 回调服务在线，否则页面报
    「登录失败 - 网络错误」。这里自动拉起 trae_work_login_server，回调服务会在
    **成功或失败时都写 RESULT_PATH**，CLI 立刻收割结果——不能只把「STATE 被删」
    当成功信号，那样任何失败（nonce 被冲掉 / 缺 refreshToken / 换 token 报错）
    都会让 CLI 干等到 15 分钟超时，浏览器那边却可能已经显示「登录成功」。
    手动粘贴模式保留：python3 -m buddy_proxy.auth.trae_work_login

    ``region``（cn/global）决定授权页 host；交互 tty 下未显式给值会先问一句
    （两区账号不通用，登错区拿到的是另一套 uid）。选定后写进共享 state，
    回调服务据此换 token、选 chat 网关、给落盘账号打 region 标。
    """
    import json
    import socket
    import subprocess
    import sys
    import time

    from buddy_proxy.auth.trae_work_login import (
        OUT_PATH,
        RESULT_PATH,
        STATE_PATH,
        STATE_TTL,
        build_login_url,
    )
    from buddy_proxy.trae.config import resolve_trae_region

    reg = resolve_trae_region(_pick_region("Trae", region, default_key="cn"))

    def _port_busy() -> bool:
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", 18080)) == 0

    def _stop(proc: subprocess.Popen) -> None:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _report_ok() -> int:
        try:
            from buddy_proxy.trae.credentials import list_accounts, load_account_cred
            accounts = [a for a in list_accounts() if a.region == reg.key] or list_accounts()
            if accounts:
                cred = load_account_cred(accounts[0].id) or {}
                expires = cred.get("expires_at", "")
                expires = expires[:10] if isinstance(expires, str) else expires
                n = len(accounts)
                suffix = f"（{reg.label} · 共 {n} 个账号）" if n > 1 else f"（{reg.label}）"
                print(
                    f"[OK] Trae Work 登录完成{suffix}：uid={cred.get('uid')} "
                    f"昵称={cred.get('nickname')} 有效期至={expires}"
                )
            else:
                print(f"[OK] Trae Work 登录完成（{reg.label}）")
        except Exception:
            print("[OK] Trae Work 登录完成")
        return 0

    url, _machine_id, _device_id, _port = build_login_url(region=reg.key)
    # 本次尝试的 nonce：只认跟它对得上的 RESULT，免得上一轮迟到的结果误杀本轮
    try:
        my_nonce = json.loads(STATE_PATH.read_text()).get("nonce") or ""
    except Exception:
        my_nonce = ""
    proc = None
    if _port_busy():
        # 端口被占不一定是我们的回调服务（也可能是别的程序或旧实例）。
        # 这种情况回调可能被黑洞掉，必须明说，别让人以为「已复用」就万事大吉。
        print("[!] 18080 已被占用，将直接复用（**请确认它确实是本项目的回调服务**；")
        print("    若不是，回调会被吞掉，登录会一直停在这里）")
    else:
        proc = subprocess.Popen(
            [sys.executable, "-m", "buddy_proxy.auth.trae_work_login_server"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        for _ in range(30):
            if _port_busy():
                break
            if proc.poll() is not None:
                print("[!] 本地回调服务启动失败，请检查 18080 端口占用", file=sys.stderr)
                return 1
            time.sleep(0.1)

    print("=" * 60)
    print(f"Trae Work (SOLO) 一键登录 · {reg.label}")
    print("=" * 60)
    print(f"浏览器将打开 {reg.auth_host} 授权页；完成登录后会自动回跳本机落盘凭证。")
    if open_browser:
        # webbrowser 失败时静默返回 False（SSH/无 GUI 会话等，见 core.browser）
        # ——必须把链接打出来，否则授权 URL 凭空消失，只能干等 15 分钟超时
        if not open_external_url(url):
            print(f"[!] 无法自动打开浏览器，请手动在浏览器打开：\n  {url}")
    else:
        print(f"请手动在浏览器打开：\n  {url}")
    print("\n[*] 等待登录完成（最长 15 分钟，Ctrl+C 取消）...")

    deadline = time.time() + STATE_TTL
    try:
        while time.time() < deadline:
            # RESULT 是权威终态（成功/失败都会写），必须最先看
            if RESULT_PATH.exists():
                try:
                    result = json.loads(RESULT_PATH.read_text())
                except Exception:
                    result = {"ok": False, "message": "登录结果文件损坏"}
                RESULT_PATH.unlink(missing_ok=True)
                # 结果带 nonce 标记：不是本轮的就丢掉继续等（上一轮迟到的结果）
                got_nonce = result.get("nonce") or ""
                if got_nonce and my_nonce and got_nonce != my_nonce:
                    continue
                STATE_PATH.unlink(missing_ok=True)
                if result.get("ok"):
                    # server 还会跑一下 Work 通道测试再退出，稍等收割它的输出；
                    # 极端情况（挂住）超时再强制清理
                    if proc is not None:
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            _stop(proc)
                    return _report_ok()
                _stop(proc)
                print(f"[!] Trae 登录失败：{result.get('message')}", file=sys.stderr)
                print(
                    "    可重试 buddy login trae；仍不行就用手动模式："
                    "python3 -m buddy_proxy.auth.trae_work_login",
                    file=sys.stderr,
                )
                return 1
            # 兜底：旧版 server 只删 STATE 不写 RESULT，仍按成功收割
            if not STATE_PATH.exists():
                if proc is not None:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _stop(proc)
                return _report_ok()
            if proc is not None and proc.poll() is not None:
                print("[!] 本地回调服务意外退出", file=sys.stderr)
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()

    _stop(proc)
    STATE_PATH.unlink(missing_ok=True)
    RESULT_PATH.unlink(missing_ok=True)
    print(
        "[!] 登录未完成（超时/取消）。若浏览器那边已经显示登录成功，说明回调"
        "没落到本机服务上——上面若有 [srv] 访问日志可对照；没有则表示浏览器"
        "压根没回跳（常见于授权页把回调 URL 的 query/path 改写了）。",
        file=sys.stderr,
    )
    print(
        "    可重试 buddy login trae，或手动模式："
        "python3 -m buddy_proxy.auth.trae_work_login",
        file=sys.stderr,
    )
    return 1


#: 智谱官网地址（取 key / 买套餐）。打印给人看，别写死在正文里。
ZCODE_CONSOLE_URL = "https://bigmodel.cn/usercenter/proj-mgmt/apikeys"
ZCODE_PLAN_URL = "https://bigmodel.cn/glm-coding"


def _login_zcode(**_kwargs) -> int:
    """zcode 无交互登录：凭据是 API key，这里检查配置并打印指引。

    zcode 没有可自动化的浏览器登录：它要的是智谱官网签发的 coding-plan
    API key，而签发入口在控制台里需要人工点（本项目的 key 也不算 OAuth
    凭据，没法用 device flow 换）。所以这个子命令的职责就是**把怎么拿到
    key 说清楚**——光打印一个文件路径，用户不知道该去哪儿取。
    """
    from buddy_proxy.providers.zcode import resolve_credentials, secret_file_path

    key, base = resolve_credentials()
    if key:
        print(f"[OK] zcode 凭据已配置: {key[:6]}***{key[-4:]}  base: {base}")
        print("     zcode 用 API key 认证，无需登录；换 key 改下面任意一处即可：")
        print("     1. 环境变量 ZCODE_API_KEY")
        print(f"     2. 文件 {secret_file_path()}（首行裸 key 或 name=value）")
        print("     3. 本机 ZCode CLI 登录 coding-plan（读 ~/.zcode/v2/config.json）")
        print(f"     控制台（换 key）: {ZCODE_CONSOLE_URL}")
        return 0
    print("[!] zcode 未配置凭据。它要的是智谱 coding-plan API key，不是账号密码。")
    print()
    print(f"    ① 领 key：{ZCODE_CONSOLE_URL}")
    print("       登录智谱账号 → 新建 API Key → 复制（形如 xxxxxxxx.yyyyyyyy 两段）")
    print(f"       还没有 coding-plan 套餐的话先开通：{ZCODE_PLAN_URL}")
    print("    ② 配到本机，任选一种：")
    print("       a) 环境变量（临时）：export ZCODE_API_KEY=<粘贴 key>")
    # 用 `>` 不用 `>>`：读取只认第一个非空行，追加会让旧 key 继续生效，
    # 用户换了 key 却毫无察觉。目录也一并建出来——这条命令不经过
    # __main__.main()，新机器上 ~/.buddy-proxy 可能还不存在。
    secret = secret_file_path()
    print(f"       b) 写文件（长期）: mkdir -p {secret.parent} && "
          f"echo '<粘贴 key>' > {secret}")
    print("          然后 chmod 600（key 是明文凭据）")
    print("          （`>` 是覆盖：重复配置时把旧 key 换掉，别用 `>>` 追加）")
    print("       c) 已装 ZCode CLI 的话，在 CLI 里登录 coding-plan 也行 "
          "（读 ~/.zcode/v2/config.json）")
    print("    ③ 让网关重新读取：buddy restart")
    return 1


def _login_glm(**_kwargs) -> int:
    """glm 官方渠道无交互登录：凭据是智谱控制台签发的 coding-plan API key。

    与 zcode 的差别只在凭据链：glm 不读 ``~/.zcode`` CLI 配置（两条 key 是
    独立购买的两个套餐，串了额度就对不上），所以这里指引的落点只有 env 和
    自有 key 文件两处。key 本身没有自动化签发/交换流程（控制台人工点），
    没有可实现的 OAuth/device flow。
    """
    from buddy_proxy.providers.glm import resolve_credentials, secret_file_path

    key, base = resolve_credentials()
    if key:
        print(f"[OK] glm 凭据已配置: {key[:6]}***{key[-4:]}  base: {base}")
        print("     glm 用 API key 认证，无需登录；换 key 改下面任意一处即可：")
        print("     1. 环境变量 GLM_API_KEY")
        print(f"     2. 文件 {secret_file_path()}（首行裸 key 或 name=value）")
        print(f"     控制台（换 key）: {ZCODE_CONSOLE_URL}")
        return 0
    print("[!] glm 未配置凭据。它要的是智谱 coding-plan API key，不是账号密码。")
    print()
    print(f"    ① 领 key：{ZCODE_CONSOLE_URL}")
    print("       登录智谱账号 → 新建 API Key → 复制（形如 xxxxxxxx.yyyyyyyy 两段）")
    print(f"       套餐购买/查看：{ZCODE_PLAN_URL}")
    print("    ② 配到本机，任选一种：")
    print("       a) 环境变量（临时）：export GLM_API_KEY=<粘贴 key>")
    secret = secret_file_path()
    print(f"       b) 写文件（长期）: mkdir -p {secret.parent} && "
          f"echo '<粘贴 key>' > {secret}")
    print("          然后 chmod 600（key 是明文凭据）")
    print("          （`>` 是覆盖：重复配置时把旧 key 换掉，别用 `>>` 追加）")
    print("    ③ 让网关重新读取：buddy restart")
    return 1


def _login_doubao(**_kwargs) -> int:
    """豆包走 CDP 直连豆包工作 App，无独立登录流程。"""
    print("豆包 provider 无独立登录：它通过 Chrome CDP 复用本机豆包工作 App 的登录态。")
    print("启动时加 --doubao（或 DOUBAO_ENABLED=1）即可，无需 proxy.sh login。")
    return 0


def _login_dumate(**_kwargs) -> int:
    """DuMate（百度搭子）直连本地代理，无独立登录流程。"""
    from buddy_proxy.dumate import discovery

    state = discovery.describe_state()
    print("DuMate（百度搭子）provider 无独立登录：它复用本机 DuMate.app 的登录态，")
    print("经 App 内置本地代理直连 dumate-svc.baidu.com 网关。")
    print()
    if state.get("ready"):
        print(f"[OK] DuMate 本地代理已就绪：port={state.get('port')} "
              f"pid={state.get('pid')} version={state.get('app_version') or '?'}")
        print("     启动时加 --dumate（或 DUMATE_ENABLED=1）即可启用。")
        return 0
    print(f"[未就绪] {state.get('hint')}")
    print("     需要先打开并登录百度搭子桌面端，再启用 --dumate。")
    return 1


def _login_mimo(open_browser: bool = True, **_kwargs) -> int:
    """mimo 登录：浏览器登录小米账号（官方 longPolling，免回调）。

    与 Qoder 同构——打开链接、用户登录、命令行轮询到结果自动继续，不需要
    本地回调服务（小米的 ``callback`` 要服务端签名，自建地址会被 10025 拒掉，
    见 ``mimo/login.py`` 的模块说明）。

    已经配好 API key 时不走登录：key 优先级高于 SSO，登录了也不会生效。
    """
    from buddy_proxy.mimo.credentials import resolve_api_key
    from buddy_proxy.mimo.login import LoginError, login_interactive
    from buddy_proxy.mimo.sso import load_account_cookies

    key, base = resolve_api_key()
    if key:
        print(f"[OK] mimo 凭据已配置(API key): {key[:4]}…{key[-2:]}  base: {base}")
        print("     API key 优先于 SSO 登录态；如需改用小米账号登录，请先清除：")
        print("     环境变量 MIMO_API_KEY / ~/.mimocode/auth.json / ~/.buddy-proxy/mimo_api_key.json")
        return 0

    account = load_account_cookies()
    if account is not None:
        print(f"[OK] mimo 已有可用凭据: userId={account.user_id}")
        print("     如需换号，重新运行本命令即可（会覆盖为新账号）。")

    try:
        cred, path = login_interactive(open_browser=open_browser)
    except KeyboardInterrupt:
        print("\n[Mimo] 已取消。")
        return 1
    except LoginError as exc:
        print(f"\n[X] mimo 登录失败: {exc}")
        return 1

    print(f"[OK] mimo 登录成功: userId={cred.user_id}")
    print(f"     凭据文件: {path}")
    print("     启动代理时加 --mimo（或 MIMO_ENABLED=1）即可启用该通道。")
    print("     若网关正在运行，需 `buddy restart` 才会加载新凭据。")
    return 0


def _login_qoder(open_browser: bool = True, region: str | None = None, **_kwargs) -> int:
    """Qoder 登录：官方 device flow（PKCE），全球版/CN 版通用。

    流程：打印（并尝试打开）授权链接 → 用户在浏览器里确认 → 轮询换回
    ``dt-`` device token，作为**新账号**落盘（按 email/refresh_token 命中
    时更新原账号顺位）。

    区域：``--region cn|global`` 显式 > 交互 prompt > ``QODER_REGION`` env >
    ``cn``。CN 与全球版账号不通用，连错域会 401，所以交互 tty 下会先问一句。
    """
    from buddy_proxy.qoder.config import REGIONS, default_region_key, resolve_region
    from buddy_proxy.qoder.credentials import (
        AuthError,
        account_cred_path,
        credential_to_cred,
        list_accounts,
        poll_device_flow,
        save_account_cred,
        start_device_flow,
    )

    region_key = _pick_region("Qoder", region, default_key=default_region_key())
    region = resolve_region(region_key)
    print(f"[Qoder] 区域: {region.label} ({region.key})  端点: {region.infer_base}")
    print(f"        可用 --region 切换区域：{', '.join(REGIONS)}")

    flow = start_device_flow(region)
    print()
    print("[Qoder] 请在浏览器中打开下面的链接，并用 Qoder 账号完成授权：")
    print()
    print(f"    {flow.auth_url}")
    print()
    if open_browser:
        # 静默失败统一交给 open_external_url 兜底（失败退 open 命令再回显链接）
        if open_external_url(flow.auth_url):
            print("[Qoder] 已尝试自动打开浏览器…")
        else:
            print("[Qoder] 自动打开浏览器失败，请手动复制上面的链接。")
    print("[Qoder] 等待授权中（最多 10 分钟，Ctrl-C 可取消）…")

    ticks = {"n": 0}

    def _tick() -> None:
        ticks["n"] += 1
        if ticks["n"] % 6 == 0:
            print(f"    …仍在等待授权（已等待约 {ticks['n'] * 5} 秒）")

    try:
        cred = poll_device_flow(flow, on_tick=_tick)
    except KeyboardInterrupt:
        print("\n[Qoder] 已取消。")
        return 1
    except AuthError as exc:
        print(f"\n[X] Qoder 登录失败: {exc}")
        return 1

    ref = save_account_cred(credential_to_cred(cred))
    # deviceToken 回包没有身份字段（实测）：登录后拉一次 userinfo 回填
    # name/email，UI 各处才能直接显示账号名而不是 UUID。
    from buddy_proxy.qoder.credentials import (
        backfill_identity,
        cred_to_credential,
        load_account_cred,
    )
    backfill_identity(ref.id, only_if_missing=False)
    cred = cred_to_credential(load_account_cred(ref.id) or {})
    print()
    who = cred.email or cred.name or ref.id
    print(f"[OK] Qoder 登录成功：{who}（{cred.describe()}）")
    print(f"     凭据文件: {account_cred_path(ref.id)}")
    accounts = list_accounts()
    if len(accounts) > 1:
        mine = next((i for i, a in enumerate(accounts) if a.id == ref.id), None)
        if mine is not None:
            print(f"     账号顺位: #{mine + 1}（共 {len(accounts)} 个账号，403/额度尽自动切换下一个）")
    print("     启动代理时加 --qoder（或 QODER_ENABLED=1）即可启用该通道。")
    return 0


def _ask_default_yes(question: str) -> bool:
    """问一句，默认 yes（直接回车、EOF、非交互终端都算 yes）。"""
    if not sys.stdin.isatty():
        print(f"{question}（非交互终端，默认是）")
        return True
    try:
        answer = input(question).strip().lower()
    except EOFError:
        return True
    except KeyboardInterrupt:
        print()
        return False
    return answer in ("", "y", "yes")


def _print_gemini_ready(cred: dict) -> None:
    print(f"[OK] gemini 登录成功: {cred.get('email') or '(未知邮箱)'}")
    print(f"     tier: {cred.get('tier')}  project: {cred.get('project_id')}")
    print("     凭据与本机 gemini CLI（~/.gemini）已互通，两边任一登录即可互用。")
    print("     启动代理时加 --gemini（或 GEMINI_ENABLED=1）即可启用该通道。")
    print("     若网关正在运行，需 `buddy restart` 才会加载新凭据。")


def _login_gemini(open_browser: bool = True, **_kwargs) -> int:
    """gemini 登录：Google OAuth（PKCE + 本地回调），免费 Code Assist 通道。

    流程与 gemini CLI 的 authWithWeb 一致：本地随机端口收回调 → code 换
    token → loadCodeAssist/onboardUser 拿托管项目 → 凭据落盘
    ``~/.buddy-proxy/gemini_oauth.json``。

    与本机 Gemini CLI 互通：两边用同一个 OAuth client，登录结果双向同步——
    进入时先读 ``~/.gemini/oauth_creds.json``，有可用登录态就问用户是否
    直接采用（默认 yes，省一次浏览器授权）；登录成功后也会把凭证按 CLI
    的格式写回 ``~/.gemini``。

    --no-browser 供远程 SSH：链接在本地浏览器打开后，把跳转的完整 URL
    粘回终端（也支持浏览器能回跳本机时直接等回调）。

    中间态（token 已保存、onboarding 没完成）不重开浏览器：直接重试
    onboarding——OAuth 授权已经成功过，再点一次纯浪费。
    """
    from buddy_proxy.gemini import cli_bridge
    from buddy_proxy.gemini.credentials import load_cred
    from buddy_proxy.gemini.login import LoginError, adopt_cli_login, login_interactive, resume_onboarding

    buddy_cred = load_cred()

    # 1) 本机 Gemini CLI 已有登录态 → 问一声，默认直接用（省一次浏览器授权）
    cli_creds = cli_bridge.load_cli_creds()
    if cli_creds is not None:
        usable, note = cli_bridge.cli_creds_usable(cli_creds)
        if usable:
            who = cli_bridge.cli_cached_email() or "未知账号"
            print(f"[Gemini] 检测到本机 Gemini CLI 已有登录态（{who}，{note}）。")
            if buddy_cred and buddy_cred.get("project_id") and buddy_cred.get("email") \
                    and buddy_cred["email"] != who:
                # 直接采用会覆盖 buddy 已有登录态——把当前账号亮出来，别让人
                # 回车之后才发现换号了
                print(f"         （当前 buddy 登录的是 {buddy_cred['email']}，"
                      f"直接采用会切换到 CLI 的账号）")
            if _ask_default_yes("         直接使用它吗？（跳过浏览器授权，Y/n）"):
                try:
                    cred = adopt_cli_login()
                except LoginError as exc:
                    print(f"\n[X] 采用 Gemini CLI 登录态失败: {exc}")
                    return 1
                _print_gemini_ready(cred)
                return 0
        else:
            print(f"[Gemini] 本机 Gemini CLI 登录态不可用（{note}），改走浏览器登录。")

    cred = buddy_cred
    if cred:
        if cred.get("project_id"):
            print("[OK] gemini 已有登录态；重新登录会覆盖（换号/刷新授权请继续）。")
            print(f"     当前账号: {cred.get('email') or '(未知)'}  project: {cred.get('project_id')}")
        else:
            # 上次 OAuth 成功但 onboarding 没走完：token 还在，先试免浏览器续跑
            print("[Gemini] 检测到已保存的 token 但缺 project（上次 onboarding 未完成），")
            print("         先尝试直接续跑 onboarding（不需要再点浏览器授权）…")
            try:
                cred = resume_onboarding()
            except LoginError as exc:
                print(f"[Gemini] 续跑失败: {exc}")
                print("         如已修好账号问题仍失败，可删掉凭据文件后重新走完整登录：")
                print(f"         rm ~/.buddy-proxy/gemini_oauth.json && buddy login gemini")
                return 1
            _print_gemini_ready(cred)
            return 0

    try:
        cred = login_interactive(open_browser=open_browser)
    except KeyboardInterrupt:
        print("\n[Gemini] 已取消。")
        return 1
    except LoginError as exc:
        print(f"\n[X] gemini 登录失败: {exc}")
        return 1
    _print_gemini_ready(cred)
    return 0


def _login_antigravity(open_browser: bool = True, **_kwargs) -> int:
    """antigravity 登录：Google OAuth（PKCE + 本地回调），Antigravity 免费通道。

    流程与 gemini 登录同构，client/scopes 用 Antigravity 自己的。支持多账号：
    相同邮箱重新登录=更新凭据（failover 顺位不变），新邮箱=追加为备用账号。
    与本机 agy（Antigravity CLI）互通是单向读取：agy 的登录态在系统 keyring，
    检测到就问用户是否直接采用（默认 yes，省一次浏览器授权）；keyring 只读，
    登录结果不回写（agy 没有明文文件可写）——keyring 只有一个账号坐标，第二个
    及以后的账号走浏览器授权。
    """
    from buddy_proxy.antigravity import cli_bridge
    from buddy_proxy.antigravity.credentials import list_accounts, load_account_cred
    from buddy_proxy.antigravity.login import LoginError, adopt_cli_login, login_interactive, resume_onboarding

    accounts = list_accounts()
    if accounts:
        print(f"[Antigravity] 已有 {len(accounts)} 个账号：")
        for i, a in enumerate(accounts):
            cred_i = load_account_cred(a.id) or {}
            ok = "✓" if cred_i.get("project_id") else "（缺 project，登录时可续跑 onboarding）"
            print(f"     #{i + 1} {a.email or a.id}  {ok}")

    # 1) 本机 agy 已有登录态（keyring）→ 问一声，默认直接用
    payload = cli_bridge.load_cli_creds()
    if payload is not None:
        usable, note = cli_bridge.cli_creds_usable(payload)
        if usable:
            who = cli_bridge.cli_cached_email(payload) or "未知账号"
            print(f"[Antigravity] 检测到本机 Antigravity CLI（agy）已有登录态（{who}，{note}）。")
            known = [a.email for a in accounts if a.email]
            if who in known:
                print("             （该邮箱已在账号列表里，采用它会更新对应账号的凭据）")
            elif accounts:
                print("             （这是新邮箱，采用后会追加为备用账号）")
            if _ask_default_yes("             直接使用它吗？（跳过浏览器授权，Y/n）"):
                try:
                    cred = adopt_cli_login()
                    _print_antigravity_ready(cred)
                    return 0
                except LoginError as exc:
                    print(f"\n[X] 采用 agy 登录态失败: {exc}")
                    print("             落回浏览器授权流程（与 gemini 通道同款兜底）。\n")
                    # adopt 可能已把 token 落盘（onboarding 阶段失败），刷新
                    # 后让下面的 resume 分支先试免浏览器续跑
                    accounts = list_accounts()
        else:
            print(f"[Antigravity] 本机 agy 登录态不可用（{note}），改走浏览器登录。")

    pending = next(
        (a for a in accounts
         if not (load_account_cred(a.id) or {}).get("project_id")),
        None)
    if pending is not None:
        # 上次 OAuth 成功但 onboarding 没走完：token 还在，先试免浏览器续跑
        print(f"[Antigravity] 账号 {pending.email or pending.id} 缺 project（上次 onboarding 未完成），")
        print("             先尝试直接续跑 onboarding（不需要再点浏览器授权）…")
        try:
            cred = resume_onboarding(pending.id)
        except LoginError as exc:
            print(f"[Antigravity] 续跑失败: {exc}")
            print("             如已修好账号问题仍失败，可删除该账号的凭据文件后重新走完整登录：")
            print(f"             rm ~/.buddy-proxy/antigravity/{pending.id}.json && buddy login antigravity")
            return 1
        _print_antigravity_ready(cred)
        return 0

    if accounts:
        print("[OK] antigravity 已有登录态；继续登录：相同邮箱=更新凭据，"
              "新邮箱=追加为备用账号（failover 自动切换）。")

    try:
        cred = login_interactive(open_browser=open_browser)
    except KeyboardInterrupt:
        print("\n[Antigravity] 已取消。")
        return 1
    except LoginError as exc:
        print(f"\n[X] antigravity 登录失败: {exc}")
        return 1
    _print_antigravity_ready(cred)
    return 0


def _print_antigravity_ready(cred: dict) -> None:
    print(f"[OK] antigravity 登录成功: {cred.get('email') or '(未知邮箱)'}")
    print(f"     tier: {cred.get('tier')}  project: {cred.get('project_id')}")
    print("     启动代理时加 --antigravity（或 ANTIGRAVITY_ENABLED=1）即可启用该通道。")
    print("     若网关正在运行，需 `buddy restart` 才会加载新凭据。")


def _login_kimi(open_browser: bool = True, **_kwargs) -> int:
    """Kimi Code 登录：官方 device flow（浏览器打开授权链接，CLI 轮询换 token）。

    流程：device_authorization 拿 user_code + 完整验证链接 → 自动开浏览器
    （打不开就手动复制）→ 轮询换 token → 尽力补 /v1/me 的昵称 → 多账号
    落盘 ``~/.buddy-proxy/kimi/``。已有 kimi cli 导出的 token JSON 也可以
    在管理面板直接导入，不必重走授权。
    """
    from buddy_proxy.kimi.login import LoginError, login_interactive

    try:
        login_interactive(open_browser=open_browser)
    except LoginError as exc:
        print(f"\n[X] Kimi 登录失败: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n[Kimi] 已取消。")
        return 1
    return 0


_DISPATCH = {
    "codebuddy": _login_codebuddy,
    "trae": _login_trae,
    "zcode": _login_zcode,
    "glm": _login_glm,
    "doubao": _login_doubao,
    "dumate": _login_dumate,
    "mimo": _login_mimo,
    "qoder": _login_qoder,
    "gemini": _login_gemini,
    "antigravity": _login_antigravity,
    "kimi": _login_kimi,
}


def _resolve_provider_region(raw: str, cli_region: str | None) -> tuple[str, str | None]:
    """provider 名归一 + intl 登录别名的隐含区域。

    ``traeintl``/``qoderintl``/``codebuddyintl`` 是通道 id（服务端海外版
    provider），登录侧没有独立入口——别名落到主 provider 并隐含
    ``region=global``（账号落盘打 region 标，重启后 ``intl_enabled()`` 检测到
    海外账号自动注册通道）。显式给了别的 ``--region`` 时抛 ValueError，别让
    两个信号打架。
    """
    name = raw.strip().lower()
    provider = PROVIDER_ALIASES.get(name, name)
    region = cli_region
    if name in ("traeintl", "qoderintl", "codebuddyintl"):
        if region and region != "global":
            raise ValueError(
                f"{name} 隐含 --region global（海外版），与 --region {region} 冲突")
        region = "global"
    return provider, region


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="buddy_proxy.auth.login",
        description="各上游 provider 的统一登录入口（provider 支持 workbuddy=codebuddy 别名）",
    )
    parser.add_argument("provider", nargs="?", default="codebuddy",
                        help="codebuddy(=workbuddy, =codebuddyintl 海外) / trae(=traeintl 海外) / zcode / glm / doubao / mimo / qoder(=qoderintl 海外) / gemini / antigravity / kimi，默认 codebuddy")
    parser.add_argument("--no-browser", action="store_true",
                        help="codebuddy/trae/mimo/qoder/gemini/antigravity/kimi 登录不自动打开浏览器，只打印链接")
    parser.add_argument("--region", default=None,
                        help="trae/qoder/codebuddy 登录区域：cn（国内版）/ global（海外版）。"
                             "不给且是交互终端时会问一句；两区账号不通用。"
                             "traeintl/qoderintl/codebuddyintl 隐含 global")
    args = parser.parse_args()

    try:
        provider, region = _resolve_provider_region(args.provider, args.region)
    except ValueError as exc:
        parser.error(str(exc))
    handler = _DISPATCH.get(provider)
    if handler is None:
        parser.error(f"未知 provider: {args.provider}（支持: {', '.join(KNOWN_PROVIDERS)}，"
                     f"workbuddy 是 codebuddy 的别名；traeintl/qoderintl 是 "
                     f"trae/qoder 的海外登录别名）")
    return handler(open_browser=not args.no_browser, region=region)


if __name__ == "__main__":
    raise SystemExit(main())
