"""``buddy update`` 子命令的行为测试。

这条命令会在别人的仓库里跑 `git pull`，所以「不该动的时候一动不动」比
「能更新成功」更重要。这里用一个一次性的裸仓库+克隆来跑真实 git（不 mock），
因为它要防的恰恰是真实 git 的行为差异：

- **工作树脏 → 必须拒绝，且 HEAD 不变**（不 stash、不 merge、不碰在制品）
- **分叉 → 必须失败并退出非零**（`--ff-only`，不造意外 merge commit）
- **脚本里不能出现 ``"$var中文"`` 写法**：bash 会把多字节字符的首字节并进
  变量名，`set -u` 下直接报 `before\\xef: unbound variable`（真踩过）。
  这条纯静态检查也要留，因为它在 dry-run 里不会暴露。
"""

from __future__ import annotations

import inspect
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BUDDY = REPO / "buddy"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="需要 git 与 bash",
)


def _git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _make_repo(root: pathlib.Path, restart_marker: pathlib.Path | None = None
               ) -> pathlib.Path:
    """建一个「上游裸仓库 + 工作克隆」，克隆里放好 buddy 脚本。

    ``init.defaultBranch`` 必须**显式钉成 main**，不能靠环境默认值：CI 上
    默认是 ``master``，于是裸仓库的 HEAD 指向 ``refs/heads/master``，而这里
    第一个提交推的是 ``main``——裸仓库就有了「HEAD 指向一个不存在的分支」
    这种状态。之后的 ``side`` 克隆 checkout 不出任何东西（工作区全空、
    HEAD 悬空），在那里提交会造出一个**无关的根提交**，再推给 ``main``
    自然被拒（non-fast-forward）。本机默认恰好是 main 所以一直没暴露——
    CI 上才炸（``test_fast_forwards_when_clean`` / ``_diverged_branch``）。

    用 ``-c init.defaultBranch=main`` 传给 ``git init`` 而不是设全局配置：
    只影响这条命令，不污染跑测试的机器。
    """
    upstream = root / "upstream.git"
    work = root / "work"
    _git("-c", "init.defaultBranch=main", "init", "--quiet", "--bare", str(upstream),
         cwd=root)
    _git("clone", "--quiet", str(upstream), str(work), cwd=root)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "pyproject.toml").write_text('[project]\nname = "b"\nversion = "0.0.0"\n')
    # 真仓库里 uv.lock/.venv 是被忽略的；这里照抄，否则 uv sync 一跑工作树就脏了
    (work / ".gitignore").write_text("uv.lock\n.venv/\nlogs/\n")
    shutil.copy2(BUDDY, work / "buddy")
    (work / "buddy").chmod(0o755)
    # 所有成功路径都要隔离重启；否则缺 proxy.sh 的失败会掩盖后续副作用。
    restart_marker = restart_marker or root / "restart.log"
    proxy = work / "proxy.sh"
    proxy.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(restart_marker))}\n'
    )
    proxy.chmod(0o755)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "init", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    _git("branch", "-u", "origin/main", cwd=work)
    return work


def _run_update(work: pathlib.Path, extra_path: pathlib.Path | None = None
                ) -> subprocess.CompletedProcess[str]:
    """只跑真实本地 git；浏览器、健康检查和依赖工具不能泄漏到宿主。

    系统 PATH 并不保证没有 uv，还会找到真实 open/curl。只允许脚本需要的
    无副作用工具；uv 仅由 extra_path 显式提供，保留「没有 uv」的真实分支。
    """
    with tempfile.TemporaryDirectory(prefix="buddy-update-bin-") as temp:
        bin_dir = pathlib.Path(temp)
        for command in ("bash", "git", "sed", "grep"):
            executable = shutil.which(command)
            assert executable is not None, f"需要 {command}"
            (bin_dir / command).symlink_to(executable)
        # 健康检查立即成功，不探测真实 8787，也不等待真实秒数。
        for command in ("curl", "open", "xdg-open"):
            fake = bin_dir / command
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
        path = str(bin_dir)
        if extra_path is not None:
            path = f"{extra_path}:{path}"
        return subprocess.run(
            ["./buddy", "update"], cwd=work, capture_output=True, text=True,
            env={"PATH": path, "HOME": str(work), "BUDDY_HOME": str(work),
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"},
            timeout=10,
        )


def _fake_uv(dir_: pathlib.Path, *, exit_code: int = 0) -> pathlib.Path:
    """造一个假 uv 放进 PATH，用来触到 ``uv sync`` 分支。

    真跑 uv 没必要（这里测的是 buddy 怎么对待 uv 的成败，不是 uv 本身），
    但它得**像 uv 一样是 PATH 上的可执行文件**，否则 ``command -v uv`` 那关
    就过不去——而这正是原测试漏掉这个分支的原因。
    """
    dir_.mkdir(parents=True, exist_ok=True)
    uv = dir_ / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$PWD" "$*" >> '
        f'{shlex.quote(str(dir_ / "uv.log"))}\nexit {exit_code}\n'
    )
    uv.chmod(0o755)
    return dir_


def _head(work: pathlib.Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True).stdout.strip()


def _advance_upstream(work: pathlib.Path, text: str) -> None:
    """在上游追加一个提交，让工作克隆变成「落后一版」。

    必须**经由另一个克隆**去提交：直接在这个工作克隆里 commit + push 的话，
    本地 HEAD 就跟着变了，克隆永远不落后——那样测到的是「已是最新」分支，
    而不是「拉到了新代码」（我第一版就是这么写错的）。
    """
    side = work.parent / "side"
    if not side.exists():
        _git("clone", "--quiet", str(work.parent / "upstream.git"), str(side),
             cwd=work.parent)
        _git("config", "user.email", "t@example.com", cwd=side)
        _git("config", "user.name", "t", cwd=side)
    (side / "marker.txt").write_text(text)
    _git("add", "-A", cwd=side)
    _git("commit", "-qm", text, cwd=side)
    _git("push", "-q", "origin", "HEAD:main", cwd=side)
    _git("fetch", "-q", "origin", cwd=work)


def test_refuses_to_touch_a_dirty_tree(tmp_path: pathlib.Path) -> None:
    """工作树脏 → 拒绝执行，且 HEAD 一动不动。

    「更新」这类命令最坏的失败不是报错，而是**悄悄把你的在制品覆盖掉**。
    所以这里断言的不只是退出码，还有 HEAD 完全没变、脏文件内容还在。
    """
    work = _make_repo(tmp_path)
    dirty = work / "marker.txt"
    dirty.write_text("work in progress")
    before = _head(work)

    proc = _run_update(work)

    assert proc.returncode != 0, f"脏工作树竟然成功了:\n{proc.stdout}"
    assert "未提交改动" in proc.stdout, proc.stdout
    assert _head(work) == before, "HEAD 不该被改动"
    assert dirty.read_text() == "work in progress", "在制品被覆盖了"


def test_fast_forwards_when_clean(tmp_path: pathlib.Path) -> None:
    """干净且落后一版 → 真的快进到上游 HEAD，新文件落地。"""
    work = _make_repo(tmp_path)
    _advance_upstream(work, "new upstream commit")
    upstream_head = subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=work,
        capture_output=True, text=True).stdout.strip()
    # 落后了才测得出「更新」，否则测的是「已是最新」那条分支
    assert _head(work) != upstream_head, "前置条件：本地应落后于上游"

    proc = _run_update(work)

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "未找到 uv" in proc.stdout, proc.stdout
    assert (tmp_path / "restart.log").read_text().splitlines() == ["restart"]
    assert _head(work) == upstream_head, f"没快进到上游:\n{proc.stdout}\n{proc.stderr}"
    assert (work / "marker.txt").read_text() == "new upstream commit"


def _is_dirty(work: pathlib.Path) -> bool:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=work,
                         capture_output=True, text=True).stdout
    return bool(out.strip())


@pytest.mark.parametrize("also_tracked", [False, True])
def test_dirty_hint_actually_unblocks_the_user(tmp_path: pathlib.Path,
                                               also_tracked: bool) -> None:
    """拦下之后给的提示，照着做必须**真的能解封**。

    这里不满足于断言提示文案里有没有 ``-u``——而是**把建议的命令真跑一遍**，
    再确认工作树干净了、``buddy update`` 放行了。因为原提示（``git stash``）
    的问题恰恰是「文案看着合理、照做却没用」：

    脏判定用 ``status --porcelain``，会把未跟踪文件（``??``）算进去；而
    ``git stash`` 默认不收未跟踪的，对纯 ``??`` 的树直接说
    "No local changes to save"，用户再跑一次又被同一理由拦住。

    ``also_tracked`` 覆盖「未跟踪 + 已跟踪混合」：这种树下旧的普通 stash 会
    把已跟踪的收走、``??`` 仍留着，**同样解不开**——所以提示要按「有没有
    ``??``」判，而不是按「是不是只有 ``??``」判。两种都参数化跑一遍。
    """
    work = _make_repo(tmp_path)
    (work / "scratch.txt").write_text("untracked wip")
    if also_tracked:
        # 必须改**已在 HEAD 里**的文件（pyproject.toml 由 _make_repo 提交过）。
        # 别现造一个新文件再 add+commit 再去改：那样测得的是「未跟踪」，
        # 混合场景根本没造出来（第一版就这么写错了）。
        tracked = work / "pyproject.toml"
        tracked.write_text(tracked.read_text() + "\n# dirty\n")

    blocked = _run_update(work)
    assert blocked.returncode != 0, blocked.stdout
    assert "stash -u" in blocked.stdout, (
        f"含未跟踪文件时应建议 stash -u，实际:\n{blocked.stdout}"
    )

    # 照提示做，然后确认真的解封了
    _git("stash", "-u", cwd=work)
    assert not _is_dirty(work), "照提示做完工作树还是脏的，提示等于没用"
    again = _run_update(work)
    assert "未提交改动" not in again.stdout, (
        f"照提示做完仍被同一理由拦住（用户会以为卡死）:\n{again.stdout}"
    )


def test_tracked_only_dirty_tree_gets_plain_stash_hint(tmp_path: pathlib.Path) -> None:
    """只有已跟踪改动时，不该劝人用 ``-u``（会顺带收走本来不在讨论范围的未跟踪文件）。"""
    work = _make_repo(tmp_path)
    tracked = work / "pyproject.toml"          # 由 _make_repo 提交过，改它才是「已跟踪」
    tracked.write_text(tracked.read_text() + "\n# dirty\n")
    proc = _run_update(work)
    assert proc.returncode != 0, proc.stdout
    assert "stash -u" not in proc.stdout, (
        f"没有未跟踪文件却建议 -u:\n{proc.stdout}"
    )


def test_pull_failure_hint_quotes_git_verbatim(tmp_path: pathlib.Path) -> None:
    """pull 失败的两条提示要能被用户**对上号**，所以得引 git 的原话。

    原先一律说「本地可能有分叉提交」，但远端不可达（断网/代理挂）也走这条
    分支，人被支去翻本地历史就白费劲。两条 git 原文实测为：
    ``fatal: Not possible to fast-forward, aborting.`` 与
    ``fatal: Could not read from remote repository.``
    """
    work = _make_repo(tmp_path)
    _advance_upstream(work, "upstream side")
    (work / "local.txt").write_text("local side")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "local side", cwd=work)   # 造分叉

    proc = _run_update(work)

    assert proc.returncode != 0, proc.stdout
    assert "Not possible to fast-forward" in proc.stdout, (
        f"没引用 git 的分叉原文，用户对不上屏幕上的报错:\n{proc.stdout}"
    )
    assert "Could not read from remote" in proc.stdout, (
        f"没提「连不上远端」这种可能:\n{proc.stdout}"
    )


def test_syncs_dependencies_when_uv_is_available(tmp_path: pathlib.Path) -> None:
    """PATH 上有 uv 时必须真的调它，然后再重启。

    这条分支原先**一次都没被执行过**：``_run_update`` 的 PATH 里没有 uv，
    全走「未找到 uv，跳过依赖同步」。变异测试（把 ``uv sync`` 改成必然失败）
    重跑仍 7 passed 才发现的——也就是说「有 uv 时会怎样」完全没人验。
    """
    marker = tmp_path / "restart.log"
    work = _make_repo(tmp_path, restart_marker=marker)
    _advance_upstream(work, "new upstream commit")
    uvdir = _fake_uv(tmp_path / "fakebin", exit_code=0)

    proc = _run_update(work, extra_path=uvdir)

    # 失败时**连 stderr 一起打**：git 的报错全在 stderr，只打 stdout 的话
    # 在 CI 上只能看到「assert 3 == 0」这种毫无线索的输出（第一版就是）。
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "同步依赖" in proc.stdout, proc.stdout
    assert (uvdir / "uv.log").read_text().splitlines() == [str(work), "sync"]
    assert marker.read_text().splitlines() == ["restart"], "成功路径应该重启且只重启一次"


def test_update_succeeds_even_if_opening_the_browser_fails(
        tmp_path: pathlib.Path) -> None:
    """更新成功之后「顺手打开管理页」失败，**不能**让整条命令报失败。

    CI 上真实踩到的（无头机器没有浏览器）：

        [buddy] 已更新 10911dc -> 601f226
        [buddy] 同步依赖（uv sync）…
        [buddy] 重启服务…
        /usr/bin/open: w3m: not found      <- 到这儿才失败
        exit 3

    代码和依赖都已更新、服务也重启了，却以非零退出——用户会以为没更成。
    根因是 ``open_ui`` 里 ``open`` 那支没写 ``|| true``，``set -e`` 把它
    传了出去（``xdg-open`` 那支本来就有）。

    这里往 PATH 塞一个必然失败的假 ``open`` 来复现：headless 环境等价物。
    """
    marker = tmp_path / "restart.log"
    work = _make_repo(tmp_path, restart_marker=marker)
    _advance_upstream(work, "new upstream commit")
    badbin = tmp_path / "badbin"
    badbin.mkdir()
    fake_open = badbin / "open"
    fake_open.write_text("#!/bin/sh\necho 'open: no browser' >&2\nexit 3\n")
    fake_open.chmod(0o755)

    proc = _run_update(work, extra_path=badbin)

    assert marker.exists(), "前置条件：服务应该已经重启过了（更新本身是成功的）"
    assert proc.returncode == 0, (
        f"打开浏览器失败把整条 update 拖成了失败:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_update_does_not_use_host_side_effect_commands(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主恰好装了 uv/浏览器也不能改变分支或触发真实网络请求。"""
    work = _make_repo(tmp_path)
    hostbin = tmp_path / "hostbin"
    hostbin.mkdir()
    marker = tmp_path / "host-command.log"
    for command in ("uv", "pip", "pip3", "python", "python3",
                    "curl", "open", "xdg-open"):
        fake = hostbin / command
        fake.write_text(
            f'#!/bin/sh\necho {command} >> {shlex.quote(str(marker))}\nexit 99\n'
        )
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{hostbin}{os.pathsep}{os.environ['PATH']}")

    proc = _run_update(work)

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "未找到 uv" in proc.stdout, proc.stdout
    assert not marker.exists(), "不该调用宿主的安装/浏览器/网络命令"
    assert (tmp_path / "restart.log").read_text().splitlines() == ["restart"]


def test_aborts_before_restart_when_uv_sync_fails(tmp_path: pathlib.Path) -> None:
    """``uv sync`` 失败 → 中止，**不能**把服务重启起来。

    这是整条命令里最容易伤到用户的一处：代码已经拉到新版、依赖却没装上，
    这时若照常重启，服务会带着旧依赖跑新代码（缺包就直接起不来），
    用户看到的是「更新完服务挂了」。所以必须断在重启之前。
    """
    marker = tmp_path / "restart.log"
    work = _make_repo(tmp_path, restart_marker=marker)
    _advance_upstream(work, "new upstream commit")
    uvdir = _fake_uv(tmp_path / "fakebin", exit_code=1)

    proc = _run_update(work, extra_path=uvdir)

    assert proc.returncode != 0, f"uv sync 失败竟然返回 0:\n{proc.stdout}"
    assert "uv sync 失败" in proc.stdout, proc.stdout
    assert (uvdir / "uv.log").read_text().splitlines() == [str(work), "sync"]
    assert not marker.exists(), "uv sync 失败后不该重启服务"


def test_diverged_branch_fails_loudly(tmp_path: pathlib.Path) -> None:
    """本地有分叉提交 → 明确失败，不偷偷造一个 merge commit。

    用 ``--ff-only`` 就是这个目的；换成默认的 ``git pull`` 会凭空多出一个
    merge 提交，把线性历史搞乱且不易察觉。
    """
    work = _make_repo(tmp_path)
    _advance_upstream(work, "upstream side")
    (work / "local.txt").write_text("local side")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "local side", cwd=work)   # 造成分叉
    before = _head(work)

    proc = _run_update(work)

    assert proc.returncode != 0, f"分叉竟然成功了:\n{proc.stdout}"
    assert _head(work) == before, "分叉时不该改动本地历史"
    parents = subprocess.run(["git", "rev-list", "--parents", "-n", "1", "HEAD"],
                             cwd=work, capture_output=True, text=True).stdout.split()
    assert len(parents) == 2, "不该产生 merge commit"


def test_no_bare_variable_before_a_multibyte_char() -> None:
    """脚本里不能写 ``$var中文`` —— bash 会把中文字节并进变量名。

    实测 ``log "当前 $before，拉取中…"`` 直接报
    ``before\\xef: unbound variable``（``set -u`` 下必炸）。JSON/终端里都看
    不出问题，只有真跑那行才暴露，所以用静态检查把它钉住。

    注意不能只查 **引号紧跟变量** 的写法（``"$var中文``）：踩到的那次是
    ``"当前 $branch @ $before，拉取中"``，变量在字符串中间。第一版正则要求
    前面是 ``"``，于是漏掉了它——变异测试（把 ``${before}`` 改回裸 ``$before``）
    照样全绿才发现。
    """
    text = BUDDY.read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):        # 注释里提到这个坑是合理的
            continue
        # 只要 $name 后面紧跟非 ASCII 且没写花括号就中招，与它在字符串里
        # 什么位置无关
        for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)([^\s\x00-\x7F])", line):
            offenders.append(f"{i}: ${m.group(1)}{m.group(2)}  <- {line.strip()}")
    assert not offenders, "变量名后紧跟多字节字符，需改用 ${var}：\n" + "\n".join(offenders)


def test_repo_builder_pins_the_branch_name() -> None:
    """建测试仓库时必须显式钉 ``init.defaultBranch=main``。

    这些测试靠**真实 git** 跑，所以不能被「跑测试的机器怎么配的」左右。
    漏掉这个钉法的后果（CI 上实测）：默认分支为 ``master`` 时，裸仓库 HEAD
    指向不存在的 ``refs/heads/master``，于是 ``side`` 克隆 checkout 不出东西、
    在那里提交会造出**无关的根提交**，推给 ``main`` 被拒（non-fast-forward）
    —— ``test_fast_forwards_when_clean`` 与 ``test_diverged_branch_fails_loudly``
    在 CI 上红，而本机（默认 main）全绿。这种「只在别人机器上失败」最难查。
    """
    # 必须扫 *_make_repo 自己的源码*，不能读整个测试文件：断言里的字面量会
    # 自己命中自己，变异成别的名字也照样通过（第一版就是这么写的，靠变异
    # 测试才发现恒真）。inspect 拿到的是真函数体，不含这条断言。
    src = inspect.getsource(_make_repo)
    assert "init.defaultBranch=main" in src, (
        "建仓库时没钉 init.defaultBranch；CI（默认 master）上会挂"
    )
    assert re.search(r'"-c",\s*"init\.defaultBranch=main"', src), (
        "应该用 git -c init.defaultBranch=main init 的形式传给 init 本身"
    )


def test_no_duplicate_function_definitions() -> None:
    """顶层函数不能重复定义。

    bash 对重复定义不报错，**后面的静默覆盖前面的**——改动时不小心把已有
    函数连带复制一份，功能看起来正常（跑的是后一个），但前一份成了死代码，
    下次改「那个函数」很可能改错地方。我加 cmd_update 时就这么复制出过一份
    重复的 open_ui，靠肉眼读 diff 才发现。
    """
    text = BUDDY.read_text(encoding="utf-8")
    names = re.findall(r"(?m)^([a-z_][a-z0-9_]*)\(\)\s*\{", text)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"这些函数被重复定义（后者会静默覆盖前者）: {dupes}"


def test_update_is_registered_as_a_subcommand() -> None:
    """``buddy update`` 得真的挂在分派表里、也写进 help。"""
    text = BUDDY.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*update\)\s+cmd_update", text), "分派表里没有 update"
    assert re.search(r"(?m)^\s*cmd_update\(\)\s*\{", text), "没有 cmd_update 定义"
    assert "buddy update" in text.split("set -euo pipefail")[0], "help 头部没写 update"
