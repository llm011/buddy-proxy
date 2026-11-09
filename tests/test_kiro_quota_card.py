"""kiro 额度卡必须出现在管理页（通用额度卡，不依赖专属面板）。

## 来由

用户报「界面上看不到 kiro 的额度」。后端数据完全正常（`benefits` 接口里
`quota.supported=true`、`KIRO PRO`、`Credit 998.93/1000`）——问题在前端：

`renderBenefits` 里有一个「排除表」，把**有专属面板**的通道从通用额度卡里
剔掉（它们的额度在各自面板里展示）。kiro 既没有专属面板、又没被排除表之外
的逻辑覆盖，于是**两个地方都不显示**。

这个测试钉住两件事：
1. kiro 不在排除表里（走通用额度卡）；
2. 排除表只包含**确实有专属面板**的通道——新增通道时别乱加。
"""

from __future__ import annotations

import pathlib
import re

BENEFITS_JS = (pathlib.Path(__file__).resolve().parents[1]
               / "src/buddy_proxy/web/static/benefits.js")
INDEX = (pathlib.Path(__file__).resolve().parents[1]
         / "src/buddy_proxy/web/static/index.html")


def _panel_providers() -> set[str]:
    """从 JS 里解析出排除表（有专属面板的通道集合）。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    m = re.search(r"const _PANEL_PROVIDERS = new Set\(\[(.*?)\]\);", text, re.S)
    assert m, "找不到 _PANEL_PROVIDERS 定义"
    return set(re.findall(r"'([a-z0-9-]+)'", m.group(1)))


def test_kiro_has_dedicated_panel():
    """kiro **有**专属面板（2026-11-10 补的），所以要被排除出通用额度卡。

    历史：一开始没面板也没排除 → 额度卡两边都不显示（用户报「看不到 kiro 额度」）。
    中间方案让它走通用卡；后来补了面板（账号管理 + 导入 + 授权），就回归
    「有面板的通道从通用卡排除」这条统一规则。
    """
    assert "kiro" in _panel_providers(), "kiro 有 kiro-panel，应被排除避免重复渲染"


def test_every_excluded_provider_has_a_real_panel_container():
    """排除表里的每个通道都必须真的在 index.html 里有面板容器。

    没有面板的通道被加进排除表 = 它的额度永远看不到（kiro 就这样丢过一次）。
    """
    html = INDEX.read_text(encoding="utf-8")
    missing = [pid for pid in _panel_providers()
               if f'id="{pid}-panel"' not in html]
    assert not missing, f"这些通道被排除但没有面板容器: {missing}"


def test_quota_filter_uses_the_set():
    """过滤逻辑必须真的用这个集合，别退回到长串 `p.id !== '...'`。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "_PANEL_PROVIDERS.has(p.id)" in text
    # 旧写法（长串 !==）不该再出现
    assert "p.id !== 'traepat' && p.id !== 'antigravity'" not in text
