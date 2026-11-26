"""buddy start/restart 打开浏览器时不能被系统 opener 卡住。"""

from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import time


REPO = pathlib.Path(__file__).resolve().parents[1]


def test_start_and_restart_return_when_system_opener_hangs(tmp_path: pathlib.Path) -> None:
    """真实场景：macOS open 已把页面交给浏览器，但进程几十分钟不退出。

    opener 故意 sleep 30 秒。两条 buddy 命令都必须在 5 秒内返回；opener 的
    三路 fd 也必须脱离 capture pipe，否则即使 shell 退出，调用方仍会等 EOF。
    """
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy2(REPO / "buddy", work / "buddy")
    (work / "buddy").chmod(0o755)
    proxy_log = tmp_path / "proxy.log"
    (work / "proxy.sh").write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(proxy_log))}\n",
        encoding="utf-8",
    )
    (work / "proxy.sh").chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("bash", "sleep"):
        executable = shutil.which(command)
        assert executable is not None
        (bin_dir / command).symlink_to(executable)
    for command in ("curl", "xdg-open"):
        # 有 open 时 xdg-open 不该走；curl 则立即报告服务健康。
        (bin_dir / command).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / command).chmod(0o755)
    open_log = tmp_path / "open.log"
    open_pids = tmp_path / "open.pids"
    (bin_dir / "open").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(open_log))}\n"
        f"printf '%s\\n' \"$$\" >> {shlex.quote(str(open_pids))}\n"
        "exec sleep 30\n",
        encoding="utf-8",
    )
    (bin_dir / "open").chmod(0o755)

    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "BUDDY_HOME": str(work),
        "BUDDY_TEST": "1",
    }
    try:
        for command in ("start", "restart"):
            proc = subprocess.run(
                ["./buddy", command], cwd=work, env=env,
                capture_output=True, text=True, timeout=5,
            )
            assert proc.returncode == 0, proc.stderr
        assert proxy_log.read_text(encoding="utf-8").splitlines() == ["start", "restart"]
        # buddy 不等待 opener，所以测试要异步等它真正 exec；立即读日志会有启动竞态。
        for _ in range(50):
            if open_pids.exists() and len(open_pids.read_text().splitlines()) == 2:
                break
            time.sleep(0.1)
        assert open_log.read_text(encoding="utf-8").splitlines() == [
            "http://127.0.0.1:8787/ui", "http://127.0.0.1:8787/ui",
        ]
    finally:
        # 异步 opener 是测试故意留下的短寿命进程，只按它自己写出的 pid 清理。
        if open_pids.exists():
            for raw_pid in open_pids.read_text(encoding="utf-8").splitlines():
                try:
                    os.kill(int(raw_pid), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
