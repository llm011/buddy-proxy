"""真实重启模拟：用子进程模拟「旧进程写入 → 进程退出 → 新进程启动」。

前面的测试是在同一进程里 new 两个 tracker。这里用真正的子进程，验证的不只是
逻辑，还有：模块 import 时 ``_tracker = _new_tracker()`` 这行在**新进程启动
早期**（凭据目录已就绪）确实把冷却恢复进去了——这是线上 01:50 那次事故的
真实时序。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap


def test_cooldown_survives_real_process_restart(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    script_dir = tmp_path / "acct"
    script_dir.mkdir()

    # 新进程的「账号列表」要能看到这个账号，否则对账会把它当已删账号剔掉
    index = {"version": 1, "accounts": [{"id": "pu-live", "priority": 1}]}
    (state / "index.json").write_text(json.dumps(index), encoding="utf-8")

    old = textwrap.dedent("""
        from buddy_proxy.raccoon import failover
        failover.mark_cooldown("pu-live", retry_after="1800", reason="账号风控")
        print("marked", "pu-live" in failover._cooldowns)
    """)
    new = textwrap.dedent("""
        from buddy_proxy.raccoon import failover
        left, kind = failover.cooldown_left("pu-live")
        print("restored", int(left > 1700), kind)
    """)
    # 子进程必须显式带 PYTHONPATH：env 里没有它就会 import 到别处（比如同一台
    # 机器上主仓库的旧代码）——测试照样「跑绿了」，但测的根本不是这份代码。
    src_root = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
    env = {"RACCOON_STATE_DIR": str(state), "PYTHONPATH": src_root}

    out_old = subprocess.run([sys.executable, "-c", old], env=env, capture_output=True,
                             text=True, check=True)
    assert "marked True" in out_old.stdout, out_old.stderr

    out_new = subprocess.run([sys.executable, "-c", new], env=env, capture_output=True,
                              text=True, check=True)
    # int(True) 打印成 1：锁的是「恢复出 >1700s 且类别是 account」
    assert "restored 1 account" in out_new.stdout, (
        f"新进程启动时没恢复冷却：stdout={out_new.stdout!r} stderr={out_new.stderr[-400:]!r}")
