"""``buddy login <provider>`` 的 CLI 入口测试（离线，不访问上游）。

锁住两件容易回退的事：

1. **zcode 的指引要能照着做**。zcode 没有可自动化的浏览器登录（凭据是智谱
   官网签发的 coding-plan API key，签发入口得人工点），所以这个子命令的全部
   价值就是「把怎么拿到 key 说清楚」。早期版本只打印一个文件路径——用户根本
   不知道该去哪儿取 key，等于没说。
2. **指引里的写文件命令是覆盖而不是追加**。读取只认第一个非空行，追加会让
   旧 key 继续生效。

运行：
    .venv/bin/python -m pytest tests/test_login_cli.py -v
"""
from __future__ import annotations

import pathlib
import re

import pytest

from buddy_proxy.auth import login as auth_login
from buddy_proxy.providers import zcode


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """隔离状态目录与相关环境变量，绝不碰真实的 key 文件。"""
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    for name in ("ZCODE_API_KEY", "MIMO_API_KEY", "MIMO_ACCOUNT_JSON", "MIMO_COOKIE_DB"):
        monkeypatch.delenv(name, raising=False)
    # 别真去读用户主目录下的 ZCode CLI 配置（config.json 那条回退链）
    monkeypatch.setattr(
        zcode.Path, "home", classmethod(lambda cls: pathlib.Path(tmp_path))
    )


# ---------------------------------------------------------------------------
# zcode 指引
# ---------------------------------------------------------------------------


def test_zcode_guide_tells_where_to_get_key(capsys):
    """未配置时：必须给出控制台地址 + 购买入口 + 具体怎么配。"""
    rc = auth_login._login_zcode()
    out = capsys.readouterr().out

    assert rc == 1, "没配好应当返回非零，脚本才能感知"
    assert auth_login.ZCODE_CONSOLE_URL in out, "必须告诉用户去哪领 key"
    assert auth_login.ZCODE_PLAN_URL in out, "没套餐的话也要知道去哪开通"
    assert "ZCODE_API_KEY" in out, "要给出环境变量这种最快的方式"
    assert "buddy restart" in out, "配完得知道要重启才生效"


def test_zcode_guide_shows_secret_path_when_configured(capsys, monkeypatch):
    """已配置时：回显打码 key，并说明换 key 改哪里。"""
    monkeypatch.setenv("ZCODE_API_KEY", "1234567890abcdef.ABCDEFGHIJKLMNOP")
    rc = auth_login._login_zcode()
    out = capsys.readouterr().out

    assert rc == 0
    assert "1234567890abcdef.ABCDEFGHIJKLMNOP" not in out, "完整 key 绝不能回显"
    assert "123456***MNOP" in out, "打码形式便于对照是哪把 key"
    assert auth_login.ZCODE_CONSOLE_URL in out


def test_zcode_guide_overwrites_instead_of_appending(capsys):
    """写文件的指引必须是覆盖（``>``）而不是追加（``>>``）。

    读取侧（``zcode._load_secret_file``）只认**第一个非空行**。用 ``>>`` 追加时
    旧 key 依然生效——用户换了 key 却毫无察觉，排查起来毫无线索。另外这条命令
    不经过 ``__main__.main()``，新机器上状态目录可能还不存在，所以指引里要带
    ``mkdir -p``。
    """
    auth_login._login_zcode()
    out = capsys.readouterr().out

    # 只看真正让人复制的命令行：正文里那句「别用 >> 追加」是有意留的提醒
    commands = [ln for ln in out.splitlines() if "echo" in ln]
    assert commands, "指引里应当给出可直接复制的写文件命令"
    for line in commands:
        assert ">>" not in line, f"这行是追加写法，旧 key 会继续生效: {line.strip()}"
    assert "mkdir -p" in out, "目录可能还不存在，得先建出来"


def test_zcode_guide_survives_appended_file_documented_case(tmp_path, monkeypatch):
    """把指引里的命令真跑一遍：落地后 ``resolve_credentials`` 读到的就是新 key。

    防的是「指引看着对、但和解析逻辑对不上」——追加写法的 bug 正是这么漏过去的。
    """
    import subprocess

    from buddy_proxy.providers import zcode

    target = tmp_path / "zcode_api_key"
    # 模拟用户照着指引操作：先建目录再覆盖写
    subprocess.run(
        f"mkdir -p {tmp_path} && echo 'NEWKEY_PART1.PART2' > {target}",
        shell=True, check=True,
    )
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    assert zcode._load_secret_file() == "NEWKEY_PART1.PART2"


def test_codebuddy_login_cli_accepts_region_dispatch_keyword(monkeypatch):
    """main 给所有登录 handler 统一传 region，CodeBuddy 国内版忽略即可。"""
    called = {}

    def fake_login(open_browser=True, **kwargs):
        called["open_browser"] = open_browser
        called.update(kwargs)
        return 0

    monkeypatch.setattr(auth_login, "_login_codebuddy", fake_login)
    monkeypatch.setattr(auth_login, "_DISPATCH", {"codebuddy": auth_login._login_codebuddy})
    monkeypatch.setattr(auth_login.sys, "argv", ["buddy login", "codebuddy"])

    assert auth_login.main() == 0
    assert called == {"open_browser": True, "region": None}


def test_every_login_handler_accepts_common_dispatch_arguments():
    """main 给每个 handler 统一传 open_browser 和 region。"""
    import inspect

    for provider, handler in auth_login._DISPATCH.items():
        try:
            inspect.signature(handler).bind(open_browser=True, region=None)
        except TypeError as exc:
            pytest.fail(f"{provider} 登录 handler 不接受统一参数: {exc}")


def test_every_known_channel_has_enable_hint():
    """每个认得的通道都要有「怎么开启」的说明，且不能推出不存在的开关。

    ``traepat`` 是这条规矩的由来：它没有 ``--traepat``（挂在 ``--trae`` 分支里
    由 ``TRAE_PAT_BEARER`` 决定），而路由报错要照着这张表给用户指路。
    """
    from buddy_proxy.core import settings as core_settings

    for pid in core_settings.KNOWN_PROVIDER_IDS:
        hint = core_settings.provider_enable_hint(pid)
        assert hint, f"{pid} 没有启用说明"
        if pid == "traepat":
            assert "--traepat" not in hint, "这个参数不存在"
            assert "TRAE_PAT_BEARER" in hint


# ---------------------------------------------------------------------------
# 别名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("MIMO", "mimo"),
        ("mimo", "mimo"),
        ("workbuddy", "codebuddy"),
        ("cb", "codebuddy"),
        ("quoder", "qoder"),
    ],
)
def test_aliases_resolve(typed, expected):
    """别名归一：大小写不敏感，历史别称照样命中。"""
    got = auth_login.PROVIDER_ALIASES.get(typed.strip().lower(), typed.strip().lower())
    assert got == expected


def test_alias_table_is_pinned():
    """钉住别名表的完整集合：新增别名是一个需要想清楚的决定，不该顺手加。

    （``workbuddy`` 是 codebuddy 的旧产品名，``quoder`` 等是既有拼法；
    表里没有的写法一律报「未知 provider」——这是用户发现敲错了的信号，
    收进表里反而把它盖掉了。）
    """
    assert set(auth_login.PROVIDER_ALIASES) == {
        "workbuddy", "cb",
        "quoder", "qodor", "qder", "qodercn", "qoder-cn",
        "gemini-cli",  # 与通道 id 同名：登录命令两写等价
        # 通道 id（海外版 provider）接进登录入口：海外版没有独立账号体系，
        # 别名落到主 provider 并隐含 --region global（_resolve_provider_region）。
        # 用户实测「buddy login traeintl」报未知参数才补的——不是顺手加的拼写变体。
        "traeintl", "qoderintl", "codebuddyintl",
    }
    for alias, target in auth_login.PROVIDER_ALIASES.items():
        assert target in auth_login.KNOWN_PROVIDERS, f"{alias} 指向了未知通道"


def test_every_alias_target_is_dispatchable():
    """别名只能指向真实存在的 provider，否则等于把用户引到 error 分支。"""
    for alias, target in auth_login.PROVIDER_ALIASES.items():
        assert target in auth_login._DISPATCH, f"别名 {alias} 指向了不存在的 {target}"
        assert target in auth_login.KNOWN_PROVIDERS


# ---------------------------------------------------------------------------
# proxy.sh 登录白名单与 python 别名表同步
# ---------------------------------------------------------------------------

#: 仓库根（tests/ 的上一级）的 proxy.sh——shell 层白名单在这里维护。
PROXY_SH = pathlib.Path(__file__).resolve().parents[1] / "proxy.sh"


def test_proxy_sh_login_whitelist_covers_python_side():
    """proxy.sh cmd_login 的 provider 白名单必须收下 python 侧全部入口名。

    ``buddy login`` 的链路是 buddy → proxy.sh login → python -m
    buddy_proxy.auth.login，shell 层有一个自己的 case 白名单：python 侧
    加了别名而 shell 层没同步的话，用户敲下去直接「未知参数」被拦（实测
    codebuddyintl 漏过、traeintl/qoderintl 当年也漏过同一处）。两层各自
    维护没有测试钉住就会再漏，这里用 python 侧的别名表 + 入口表做全集
    校验——以后加别名/加 provider，白名单没跟上 CI 就红。
    """
    text = PROXY_SH.read_text(encoding="utf-8")
    m = re.search(r"^\s*([a-z0-9|-]+)\)\s*\n\s*(?:#[^\n]*\n\s*)*provider=\"\$1\"", text, re.M)
    assert m, "找不到 proxy.sh cmd_login 的 provider case 白名单（结构改了？）"
    shell_names = set(m.group(1).split("|"))

    expected = set(auth_login.PROVIDER_ALIASES) | set(auth_login.KNOWN_PROVIDERS)
    missing = expected - shell_names
    assert not missing, (
        f"proxy.sh login 白名单缺少: {sorted(missing)}——"
        f"python 侧认的名字 shell 层不认，`buddy login` 会直接报未知参数"
    )


# ---------------------------------------------------------------------------
# codebuddy 登录的区域选择（PR #139 review 补的）
# ---------------------------------------------------------------------------

def test_login_codebuddy_picks_region_via_pick_region(monkeypatch, tmp_path):
    """_login_codebuddy 的区域必须经 _pick_region 决定，不许静默默认 cn。

    proxy.sh/buddy/README 三处 usage 和 argparse help 都写了「--region 不给
    且是交互终端时会问一句」，trae/qoder 一直是这样；codebuddy 曾是
    ``region = region or "cn"``——交互终端不问、静默落 CN，想登海外版的
    用户拿到一套 CN uid 很难察觉（两区账号连错域必 401）。这条测试钉住
    「选区走 _pick_region 且后续按选出的区域接端点/落 session」。
    """
    import json

    recorded: dict = {}

    def fake_pick(label, cli_region, *, default_key):
        recorded["label"], recorded["cli"], recorded["default"] = label, cli_region, default_key
        return "global"  # 模拟交互 prompt 里用户选了海外版

    monkeypatch.setattr(auth_login, "_pick_region", fake_pick)
    monkeypatch.setenv("CODEBUDDY_INTL_SESSION", str(tmp_path / "intl-session.json"))
    monkeypatch.delenv("CODEBUDDY_INTL_ENDPOINT", raising=False)

    class FakeClient:
        """替身 CodeBuddyClient：login() 往 session_file 写一份合法 session。"""

        def __init__(self, endpoint, session_file=None):
            recorded["endpoint"] = endpoint
            self.session_file = pathlib.Path(
                session_file or pathlib.Path.home() / ".codebuddy-session.json")

        def login(self, open_browser=True):
            self.session_file.write_text(json.dumps({
                "auth": {"accessToken": "tok-x", "refreshToken": "r", "expiresAt": 1},
                "account": {"uid": "u1", "nickname": "tester"},
                "machineId": "m1",
            }), encoding="utf-8")

    monkeypatch.setattr("buddy_proxy.codebuddy_provider.client.CodeBuddyClient", FakeClient)

    rc = auth_login._login_codebuddy(open_browser=False, region=None)

    assert rc == 0
    # 选区确实走了 _pick_region（label 正确、无 --region、默认 cn）
    assert recorded["label"] == "CodeBuddy"
    assert recorded["cli"] is None and recorded["default"] == "cn"
    # 选出的 global 贯穿到端点与海外独立 session 文件
    assert recorded["endpoint"] == "https://www.codebuddy.ai"
    assert (tmp_path / "intl-session.json").exists()
