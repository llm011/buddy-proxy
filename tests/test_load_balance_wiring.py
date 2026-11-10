"""带负载均衡开关的通道，转发路径必须真的接了 ``reorder``。

额度页会给"提供 ``_quota_account_ids``"的多账号通道摆负载均衡开关。开关只是
UI 与配置；真正让它生效的是转发路径上的 ``lb_mod.reorder(...)``。两处各改各的，
就会出现"面板上能点、点了没用"的通道（kiro 就差点这样）。

只读源码文本，不 import 通道包：通道模块 import 链很重，且这里只关心接线是否存在。
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "buddy_proxy"


def test_channels_with_lb_toggle_call_reorder():
    providers = sorted(_SRC.glob("*/provider.py"))
    with_ids = [p for p in providers
                if "def _quota_account_ids" in p.read_text(encoding="utf-8")]
    assert with_ids, "找不到任何带 _quota_account_ids 的通道，这条护栏需要跟着改"
    missing = sorted(p.parent.name for p in with_ids
                     if "lb_mod.reorder(" not in p.read_text(encoding="utf-8"))
    assert not missing, (
        f"这些通道有负载均衡开关的账号映射，但转发路径没调 lb_mod.reorder: {missing}；"
        "开关会显示却不生效"
    )
