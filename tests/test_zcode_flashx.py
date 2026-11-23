"""zcode 通道 glm-5.3-flashx 接入回归测试（离线，不访问上游）。

背景（2026-09-19 实测）：``glm-5.3-flashx`` 出现在智谱 coding 端点
``/models`` 列表里，但本机订阅调用被拒为 ``429 code 1311
「当前订阅套餐暂未开放GLM-5.3-FlashX权限」``——同一把 key 打 ``glm-5.3`` /
``glm-5.3-flash`` / ``glm-5-turbo`` 均 200，属套餐授权缺口而非 key 失效。
故按「预备接入」登记模型表：订阅开通后无需改代码即可用。

同时锁死两条实测结论：
1. 该端点 model 名**大小写不敏感**（四种写法都解析到 GLM-5.3-FlashX，乱名才
   报 1211 模型不存在）——故大小写不能作为校验手段，只能靠断言锁住字面量。
2. ``traepat`` 通道**没有**这个模型（不在 PAT 目录，目录由 models_config.json
   的 provider=="traepat" 条目生成）。

关于 ``codebuddy`` 通道：**2026-09-27 复查改判**。当日实测 ``codebuddy`` 通道对该
模型返回 11102 ``service info not found``，故只登记在 zcode；现已开放——实测
``codebuddy/glm-5.3-flashx`` 连续多次 200 且返回真实 ``usage``，而 zcode 通道
反被拒为 1311「套餐暂未开放」。所以现在**应该**把它写进 models_config.json
（无 provider 字段 → 归 CodeBuddy 静态表）。
"""
from __future__ import annotations

import json

from buddy_proxy.providers.zcode import (
    DEFAULT_MODELS,
    MODEL_NAME_CANONICAL,
    ZcodeProvider,
)
from buddy_proxy.web.model_list import normalize_model_format


def test_flashx_registered_in_provider_tables():
    """模型表与上游名映射都要有 flashx，且字面量与上游一致。"""
    assert "glm-5.3-flashx" in DEFAULT_MODELS
    assert MODEL_NAME_CANONICAL["glm-5.3-flashx"] == "GLM-5.3-FlashX"


def test_flashx_listed_by_provider_models():
    """ZcodeProvider.models() 要包含 flashx（/v1/models 与自动匹配都靠它）。"""
    ids = [m["id"] for m in ZcodeProvider().models()]
    assert "glm-5.3-flashx" in ids
    # 既有模型不受影响
    assert {"glm-5.3", "glm-5.3-flash", "glm-5-turbo"} <= set(ids)


def test_flashx_in_health_report():
    """health() 的 models 列表是排障入口，必须能反映 flashx。"""
    assert "glm-5.3-flashx" in ZcodeProvider().health()["models"]


def test_bare_name_auto_matches_zcode():
    """裸名 glm-5.3-flashx 必须能被 zcode 命中。

    forward_chat 的第 2 优先级路由是「模型 id 命中某个非默认 provider 的
    models()」。zcode 是唯一声明 glm-* 的通道（codebuddy 走静态表兜底），
    若这里漏登记，裸名请求会落回 codebuddy 兜底并报 11102 而非 zcode 的
    1311，排障时极具误导性。
    """
    provider = ZcodeProvider()
    assert any(m["id"] == "glm-5.3-flashx" for m in provider.models())


def test_canonical_rewrite_applied_on_anthropic_passthrough():
    """anthropic 直通路径要把小写 id 改写为上游正式名。

    断言的是 ``forward`` 里 ``MODEL_NAME_CANONICAL.get(body["model"], ...)``
    的取值口径，避免有人误删映射后静默透传小写名。
    """
    assert MODEL_NAME_CANONICAL.get("glm-5.3-flashx", "glm-5.3-flashx") == "GLM-5.3-FlashX"


def test_codebuddy_channel_now_claims_flashx():
    """``glm-5.3-flashx`` 已进入 CodeBuddy 静态表（2026-09-27 复查改判）。

    原判定（2026-09-19）：codebuddy 通道对该模型返回 11102
    ``service info not found``，故只登记在 zcode、不写进 models_config.json。
    **该结论已过期**——CodeBuddy 侧现已开放，实测 ``codebuddy/glm-5.3-flashx``
    连续多次 200 并返回真实 ``usage``；反倒是 zcode 通道被拒为 1311
    「当前订阅套餐暂未开放GLM-5.3-FlashX权限」。

    因此本条改为锁「静态表里确实登记了它」：漏登记会让客户端在目录里看不到
    这个**确实可用**的模型。
    """
    from buddy_proxy.web import model_list

    config_file = model_list.pathlib.Path(model_list.__file__).parent / "models_config.json"
    data = json.loads(config_file.read_text("utf-8"))
    entries = [m for m in data.get("models", []) if m.get("id") == "glm-5.3-flashx"]
    assert entries, "flashx 应登记进 models_config.json（CodeBuddy 通道实测可用）"
    # 无 ``provider`` 字段 → 归 CodeBuddy 静态表
    assert any(m.get("provider") is None for m in entries)


def test_normalize_model_format_keeps_flashx_fields():
    """配置解析口径不因新模型变形（防御性：字段名拼错会静默丢能力）。"""
    entry = normalize_model_format({
        "id": "glm-5.3-flashx",
        "name": "GLM-5.3-FlashX",
        "vendor": "zcode",
        "max_input": 200000,
        "max_output": 48000,
        "tool_call": True,
        "images": False,
        "reasoning": True,
    })
    assert entry["id"] == "glm-5.3-flashx"
    assert entry["tool_call"] is True
    assert entry["reasoning"] is True


# ---------------------------------------------------------------------------
# 凭据文件位置：只认本项目自己的状态目录
# ---------------------------------------------------------------------------


def test_secret_file_lives_in_project_state_dir(tmp_path, monkeypatch):
    """zcode 的 key 文件放在本项目状态目录下，可被 BUDDY_PROXY_STATE_DIR 挪走。

    以前指向别人的 secrets 目录（``~/.ethan/.secrets``），那是别的工具的私有
    目录、不归本项目管：混读会让「这个文件到底谁负责写」说不清，也容易和那边
    的同名文件互相踩。现在与其它状态文件同源（``core.paths.state_file``），
    环境变量一改就整体搬家。
    """
    from buddy_proxy.providers import zcode

    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    path = zcode.secret_file_path()
    assert path.parent == tmp_path, f"应落在状态目录下，实际 {path}"
    assert path.name == "zcode_api_key"
    assert ".ethan" not in str(path), "不得再引用外部 agent 的 secrets 目录"


def test_secret_file_is_read_from_state_dir(tmp_path, monkeypatch):
    """写进状态目录的 key 要能被真正读出来（裸 key 与 name=value 两种形态）。"""
    from buddy_proxy.providers import zcode

    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ZCODE_API_KEY", raising=False)

    zcode.secret_file_path().write_text("abc123.def456\n", encoding="utf-8")
    assert zcode._load_secret_file() == "abc123.def456"

    zcode.secret_file_path().write_text("ZCODE_API_KEY=xyz.789\n", encoding="utf-8")
    assert zcode._load_secret_file() == "xyz.789"


def test_no_ethan_secrets_reference_in_source():
    """源码里不得再出现那个外部 secrets 目录的字面量（防止回潮）。"""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "buddy_proxy"
    hits = [
        f"{p.relative_to(src)}:{i}"
        for p in src.rglob("*.py")
        for i, line in enumerate(p.read_text("utf-8").splitlines(), 1)
        if ".ethan/.secrets" in line
    ]
    assert hits == [], f"仍在引用外部 secrets 目录: {hits}"


# ---------------------------------------------------------------------------
# GLM-5.4（2026-11-23 接入）
# ---------------------------------------------------------------------------

def test_glm_5_4_is_in_the_catalog():
    """5.4 要能被 forward_chat 的「自动匹配 provider.models()」命中。

    漏了它，``glm-5.4`` 请求会落到 codebuddy 兜底通道，报一个跟模型无关的错。
    """
    from buddy_proxy.providers.zcode import DEFAULT_MODELS

    assert "glm-5.4" in DEFAULT_MODELS
    # 描述里要标出 1M 上下文与多模态（实测两者都成立，见下一条）
    desc = DEFAULT_MODELS["glm-5.4"]
    assert "1M ctx" in desc
    assert "multimodal" in desc


def test_glm_5_4_has_no_variants():
    """5.4 只有裸名——别照 5.3 的形状顺手补 -flash/-flashx/-turbo。

    2026-11-23 实测：``glm-5.4-flash`` / ``glm-5.4-flashx`` / ``glm-5.4-turbo``
    上游一律回 ``400 {"code":1211,"message":"模型不存在，请检查模型代码。"}``，
    而上游 ``/models`` 列表里 5.4 系列也只有 ``glm-5.4`` 一项。

    这条测试的作用是**拦住「补全变体」的冲动**：目录里一旦出现 5.4 变体，
    用户在 /ui 里选中它就是个必然失败的入口。
    """
    from buddy_proxy.providers.zcode import DEFAULT_MODELS

    for suffix in ("-flash", "-flashx", "-turbo", "-air"):
        assert f"glm-5.4{suffix}" not in DEFAULT_MODELS, (
            f"glm-5.4{suffix} 上游不存在（1211），不该进目录")


def test_glm_5_4_catalog_tables_stay_in_sync():
    """三张表必须同键：目录 / 大小写归一 / 抵扣系数。

    只加其中一张的后果各不相同且都很难查：
    - 缺 CANONICAL → anthropic 直通时上游拿到未归一的 name（日志难读）；
    - 缺 COEFFS → 积分列永远空，用户以为免费；
    - 多出目录外的键 → 该模型选不到，白算。
    """
    from buddy_proxy.providers.zcode import (
        CREDIT_COEFFS, DEFAULT_MODELS, MODEL_NAME_CANONICAL,
    )

    assert set(DEFAULT_MODELS) == set(MODEL_NAME_CANONICAL)
    assert set(CREDIT_COEFFS) <= set(DEFAULT_MODELS)
    assert MODEL_NAME_CANONICAL["glm-5.4"] == "GLM-5.4"


def test_glm_5_4_declares_vision_in_catalog():
    """``models()`` 要把实测能力带给下游——5.4 只由 zcode/glm 提供，没人替它补。

    ``models()`` 原本只吐 id + 描述，而 ``aggregate_models`` 对能力字段是「组里
    有几条合并几条」：一条都没报就是**没有这个字段**，``/v1/models`` 于是把 5.4
    报成 ``supports_images: false``。对多通道模型无所谓（别的通道会报并集），但
    5.4 目前只有 zcode 和同源的 glm 两个通道——codebuddy 回 11102、traepat 不在
    目录。客户端据此在本地把图剥掉再发，而它明明能看图（anthropic 端点带 base64
    图实测 200 且答对了颜色）。

    断言用「字段存在 + images 为真」而不是逐个比数值：数值由
    ``test_glm_5_4_limits_are_measured`` 单独锁，改标定值时只动一处。
    """
    from buddy_proxy.providers.zcode import ZcodeProvider

    entry = next(m for m in ZcodeProvider().models() if m["id"] == "glm-5.4")
    assert {"images", "tool_call", "reasoning", "max_input", "max_output"} <= set(entry)
    assert entry["images"] is True


def test_glm_5_4_limits_are_measured():
    """窗口与输出上限是标定值，改动前要重测。

    1M 上下文：1,000,000 token 通过、1,100,000 报 ``1261 Prompt exceeds max
    length``；max_tokens 131072 合法、131073 起报 ``1210 限制数值范围``。
    这些数只是**目录元数据**，本仓库没有拿 max_output 去夹请求的代码——别指望
    改这里能防住上游那条 1210。
    """
    from buddy_proxy.providers.zcode import GLM_54_MAX_INPUT, GLM_54_MAX_OUTPUT

    assert GLM_54_MAX_INPUT == 1_048_576
    assert GLM_54_MAX_OUTPUT == 131_072


def test_glm_5_4_credit_estimate_uses_measured_coeffs():
    """5.4 的抵扣系数是实测标定的（官方文档还没更新到 5.4）。

    标定值 (4.6, 1.1, 16.0) 与 5.3 的 (6.9, 1.7, 24.0) 同构（约 2/3），
    但**不能拿 2/3 顶替**：2/3 会算出 cached=1.13，实测是 1.10。

    这里锁一个手算过的数：10000 输入（其中 8000 命中缓存）+ 1000 输出，高峰：
        2000×4.6 + 8000×1.1 + 1000×16.0 = 9200 + 8800 + 16000 = 34000
        34000 / 10000 = 3.4
    """
    from buddy_proxy.core.credit_estimate import estimate_credit
    from buddy_proxy.providers.zcode import CREDIT_COEFFS

    assert CREDIT_COEFFS["glm-5.4"] == (4.6, 1.1, 16.0)

    est = estimate_credit("zcode", "glm-5.4", 10000, 1000, cached_tokens=8000)
    assert est is not None, "5.4 必须能出估算值（否则积分列空白）"
    # 非高峰 5 折，所以只断言量级落在两档之间，避免绑死时段
    assert 1.5 <= est <= 3.5


def test_zcode_start_stays_on_flash_only():
    """Start Plan 套餐只有 glm-5.3-flash——别跟着 coding-plan 一起加 5.4。

    2026-11-23 接 5.4 时有人问「``zcode_start.py`` 是不是漏改了」。核实结论
    是**确认过的边界**：那是另一个套餐（``zcode-plan/anthropic`` 端点），本机
    ZCode CLI 配置里 ``bigmodel-start-plan`` 的默认模型也还是 ``glm-5.3-flash``
    ——官方客户端都没把 5.4 挂上去。

    这条测试的作用：等哪天真要加 5.4 到 Start Plan，它会红一次，提醒你把
    ``DEFAULT_MODELS`` / ``MODEL_NAME_CANONICAL`` 两张表一起补，而不是只改一张。
    """
    from buddy_proxy.providers.zcode_start import (
        DEFAULT_MODELS as START_MODELS,
        MODEL_NAME_CANONICAL as START_CANONICAL,
    )

    assert set(START_MODELS) == {"glm-5.3-flash"}, (
        "Start Plan 目录变了？确认套餐真放开了，并同步 MODEL_NAME_CANONICAL")
    assert set(START_MODELS) == set(START_CANONICAL)
    assert "glm-5.4" not in START_MODELS
