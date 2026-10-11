"""trae 通道 glm-5.3-flashx 接入回归测试（离线，不访问上游）。

背景（2026-09-30 实测）：``glm-5.3-flashx`` 在 trae 通道**可用**，但 trae 的
``MODEL_IDS`` 目录里原先没有它——于是出现「能通却查不到」的隐状态：
用户不可能从 ``/v1/models`` 发现这个模型，只能靠口口相传。

**最难的一步不是发现有响应，而是排除「别名模糊匹配」这个解释。**
``glm-5.3-flashx`` 恰是 ``glm-5.3-flash`` 加个后缀，很容易被当成前缀匹配或
大小写不敏感解析的产物——若真是那样，登记进目录就是错的（等于对外承诺一个
上游并不存在的模型）。三条证据一起看才排除了它：

1. **源码里没有任何 flashx 映射**：``MODEL_MAP`` 无此键，trae 把模型名
   **原样透传**给上游，是上游自己认这个名字（本文件第一条断言即锁这点）。
2. **上游精确匹配**：``glm-5.3-flashxx`` 与大小写变体 ``glm-5.3-flashX``
   都被 4001 ``param is invalid`` 拒绝。若上游做前缀或大小写不敏感解析，
   这两个不会死——它们死掉说明上游是精确认字面量。
3. **真实出流**：连续多次 200，带真实 ``usage``，自称 Z.ai GLM 系。

另外两个通道的对照结论（**不要**因为名字相近就以为行为一致——三个通道是
各自独立的授权，互不推导）：

- ``zcode``：同一模型被拒为 1311「当前订阅套餐暂未开放GLM-5.3-FlashX权限」。
- ``traepat``：被拒，「不在 traepat 目录（PAT 通道仅含扩展模型与注册过的
  个人目录模型）」。二者已由 ``test_zcode_flashx.py`` 覆盖。
"""
from __future__ import annotations

from buddy_proxy.core.credit_estimate import estimate_credit
from buddy_proxy.trae.config import (
    MODEL_CREDITS,
    MODEL_IDS,
    MODEL_MAP,
    _map_model,
    _WORK_FUNCTION_OVERRIDE,
)
from buddy_proxy.trae.provider import TraeProvider


def test_flashx_listed_by_trae_catalog():
    """目录里要有 flashx（/v1/models 与前端模型选择器都读它）。

    漏登记的后果不是报错而是**静默**：请求照样能通，但客户端列不出来，
    等于这个模型对用户不存在。
    """
    ids = [m["id"] for m in TraeProvider().models()]
    assert "glm-5.3-flashx" in ids
    # 既有 glm 模型不受影响
    assert {"glm-5.3", "glm-5.3-flash"} <= set(ids)


def test_flashx_sits_with_glm_family_in_catalog():
    """目录顺序沿用上游展示序：flashx 紧跟 glm-5.3-flash 之后（原 T1 档内）。"""
    assert MODEL_IDS.index("glm-5.3-flashx") == MODEL_IDS.index("glm-5.3-flash") + 1


def test_flashx_is_passed_through_verbatim():
    """**关键断言**：模型名原样透传，不做任何映射。

    这条锁的是上面「证据 1」。若有人日后给 flashx 加了 ``MODEL_MAP`` 映射
    （比如图省事改成映射到 glm-5.3-flash），请求就会静默变成另一个模型，
    而 ``usage.model`` 还回显着 flashx——用户拿不到任何提示。宁可红。
    """
    assert "glm-5.3-flashx" not in MODEL_MAP
    assert _map_model("glm-5.3-flashx") == "glm-5.3-flashx"


def test_flashx_needs_no_function_override():
    """不要给 flashx 加 ``_WORK_FUNCTION_OVERRIDE``。

    ``_WORK_FUNCTION_OVERRIDE`` 里那些模型（glm-5.1 / Doubao-Seed-Code /
    glm-5.3-flash）是在 Work 通道（solo_work_lite）下 4001 才改走 chat_v3
    的。flashx 的实测全部走的是**默认主路径 native chat_v3**（200 出流），
    solo_work_lite 从未被触发、也未实测——所以这里没有任何依据支持加覆盖
    项。别顺手抄一个进去：那会掩盖问题，真在 solo_work_lite 下失败时应该
    让 4001 诚实地冒出来，而不是被覆盖项悄悄绕过。
    """
    assert "glm-5.3-flashx" not in _WORK_FUNCTION_OVERRIDE


def test_flashx_credit_multiplier():
    """目录展示倍率 x0.31，积分估算走实测计价（两者口径不同，见下）。

    x0.31 来自 2026-09-30 WorkBuddy 客户端倍率面板截图（口径已经过 4 个旧
    模型交叉验证）。但**积分估算不能用它线性折算**：官方真实计费对输入/输出
    分开计价（out≈4×in），「100×倍率/1M 总 tokens」会差约 9 倍——估算走
    ``MEASURED_CREDIT_RATES`` 实测单价（官方账单对账解出，9 条记录回代 8 中；
    2026-10-12 长会话缓存流量二次对账后单价整体下修 ~0.28x，见该表注释）。
    (30000,100) 实测口径：30000×2.351e-5 + 100×7.242e-5 ≈ 0.71。
    """
    assert MODEL_CREDITS["glm-5.3-flashx"] == "x0.31"
    entry = next(m for m in TraeProvider().models() if m["id"] == "glm-5.3-flashx")
    assert entry["credits"] == "x0.31"
    assert estimate_credit("trae", "glm-5.3-flashx", 30000, 100) == 0.71


def test_flashx_auto_matches_trae_channel():
    """裸名 ``glm-5.3-flashx`` 要能被 trae 通道认领（forward_chat 自动路由用）。

    只断言**裸名**：路由侧在调 ``accepts_model`` 之前就已经剥过 ``trae/``
    前缀（见 ``forward.py``「``model`` 必须是已剥前缀的」），所以带前缀的
    写法不在本方法的契约里——写成 ``accepts_model("trae/glm-5.3-flashx")
    会失败，那不是 bug。基类里那段剥前缀逻辑是给 qoder 这类「目录 id 自带
    前缀」的通道用的，trae 的目录 id 是裸名。
    """
    assert TraeProvider().accepts_model("glm-5.3-flashx")
