"""Trae 常量与配置：区域、URL、版本头、模型映射、环境开关、调试/心跳工具。"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# ───────────────────────── Trae 常量 ─────────────────────────

BASE_URL_CN = "https://trae-api-cn.mchost.guru"
BASE_URL_SG = "https://a0ai-api-sg.byteintlapi.com"
IDE_VERSION = "3.3.67"
IDE_VERSION_CODE = "20260401"
X_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"


# ───────────────────────── 区域（国内版 / 海外版） ─────────────────────────
#
# Trae 有两套**账号互不通用**的区域，各有一套域名（照 qoder 的 Region 模式）。
# 下面每个端点都是 2026-10-05 无凭据探测的实测结论（能区分「端点存在但没鉴权」
# 与「路由根本不存在」，判据见各行注释）：
#
# - **chat 网关**：两侧同一组路径（``/api/agent/v3/llm_utils_chat`` 等）都回
#   401 + ``code 1001``（= 端点在、只是没带 token），路径与错误结构完全一致，
#   所以 transport 只需换 base URL。
# - **额度**：同一个 ``ide_user_ent_usage`` 接口，但**版本号不同**——CN 是
#   ``/trae/api/v2/``，海外只有 ``/trae/api/v1/``（v2 在海外是 Akamai TLB 层
#   404，不是应用级）。两侧响应都带 ``is_dollar_usage_billing`` flag。
# - **签到**：CN 的 ``/trae/api/v2/ug/checkin_credits/{status,claim}`` 回
#   200 + ``code 1001``（路由在）；海外在 grow-normal / growsg-normal /
#   api.trae.ai 三处**全部回应用级 404 "Page not found"** —— 海外版没有签到
#   系统，故 ``has_checkin=False``，自动签到必须跳过海外账号（否则会天天打
#   一串 404 进 checkin.jsonl 污染日历）。
# - **计费口径**：CN 是**积分制**（``is_credits_billing``，权益包
#   ``currency=1`` + ``credits_limit`` / ``credits_amount``）；海外是**美元
#   Usage 余额制**（官网 pricing 实测：Free $1 一次性不刷新、Pro $20、
#   Pro+ $60、Ultra $200，按 token 折算成 Usage 扣减、订阅期开始时刷新；
#   ``is_pay_freshman`` 时新人一次性赠 $3）。额度展示必须分叉，把美元报成
#   「积分」会误导。
# - **package-type 头**：CN ``stable_cn`` / 海外 ``stable_i18n``。

@dataclass(frozen=True)
class TraeRegion:
    """一个区域的端点集合（对齐 ``qoder.config.Region`` 的口径）。"""

    key: str
    label: str
    #: chat 网关基址（``BASE_URL_CN`` / ``BASE_URL_SG``）。
    chat_base: str
    #: UG（user growth）基址：额度 / 签到都挂它下面。
    ug_base: str
    #: 额度接口的版本号（CN ``v2`` / 海外 ``v1``，见上方实测注释）。
    ug_usage_version: str
    #: OAuth 域（ExchangeToken / GetUserInfo）。**不是** chat 网关：
    #: 聊天走它会 404（2026-09 实测，见 trae_work_login.OUT_PATH 旁注释）。
    oauth_api: str
    #: 浏览器授权页基址。
    auth_host: str
    #: ``package-type`` 头的值。
    package_type: str
    #: 上游有没有每日签到（海外实测无 → False）。
    has_checkin: bool
    #: 计费口径：``credits``（积分）/ ``dollar``（美元 Usage 余额）。
    billing: str

    def usage_url(self) -> str:
        """权益/额度用量接口。"""
        return f"{self.ug_base}/trae/api/{self.ug_usage_version}/pay/ide_user_ent_usage"

    def checkin_url(self, action: str) -> str:
        """签到接口（``action`` = ``status`` / ``claim``）；无签到的区域不应调用。"""
        return f"{self.ug_base}/trae/api/v2/ug/checkin_credits/{action}"

    def exchange_token_url(self) -> str:
        """refreshToken → access_token。"""
        return f"{self.oauth_api}/cloudide/api/v3/trae/oauth/ExchangeToken"

    def user_info_url(self) -> str:
        return f"{self.oauth_api}/cloudide/api/v3/trae/GetUserInfo"

    def authorization_url(self) -> str:
        return f"https://{self.auth_host}/authorization"


#: 已知区域。默认区域可由 ``TRAE_REGION`` 环境变量覆盖。
TRAE_REGIONS: dict[str, TraeRegion] = {
    "cn": TraeRegion(
        key="cn",
        label="Trae 国内版",
        chat_base=BASE_URL_CN,
        ug_base="https://api.trae.cn",
        ug_usage_version="v2",
        oauth_api="https://api.trae.com.cn",
        auth_host="www.trae.cn",
        package_type="stable_cn",
        has_checkin=True,
        billing="credits",
    ),
    "global": TraeRegion(
        key="global",
        label="Trae 海外版",
        chat_base=BASE_URL_SG,
        ug_base="https://growsg-normal.trae.ai",
        ug_usage_version="v1",
        oauth_api="https://api.trae.ai",
        auth_host="www.trae.ai",
        package_type="stable_i18n",
        has_checkin=False,
        billing="dollar",
    ),
}

#: 默认区域 key。
DEFAULT_TRAE_REGION = "cn"


def default_trae_region_key() -> str:
    """默认区域：``TRAE_REGION`` 显式指定 > ``cn``。

    两区账号不通用（连错域 401），所以区域必须显式可配；缺省按 CN——
    历史账号全是 CN，这样迁移前的凭据行为不变。
    """
    env = (os.environ.get("TRAE_REGION") or "").strip().lower()
    return env if env in TRAE_REGIONS else DEFAULT_TRAE_REGION


def resolve_trae_region(key: str | None = None) -> TraeRegion:
    """按 key 取区域；空/未知 key 回退默认区域（不抛——配置层容错）。"""
    k = (key or default_trae_region_key()).strip().lower()
    return TRAE_REGIONS.get(k) or TRAE_REGIONS[default_trae_region_key()]


def resolve_trae_region_by_chat_base(base_url: str) -> TraeRegion:
    """由 chat 网关 base URL 反查区域（legacy 单账号分支用）。

    legacy 分支（``TRAE_TOKEN`` / 本机登录态）没有账号级 region 字段，只有
    一个 base_url，据此反查即可选出对的模型表。认不出（自定义网关/测试
    mock）就回退默认区域——模型名原样透传，不会比现状更差。
    """
    needle = (base_url or "").rstrip("/")
    for reg in TRAE_REGIONS.values():
        if needle and needle == reg.chat_base.rstrip("/"):
            return reg
    return resolve_trae_region()

# Trae Work (SOLO) 客户端版本。
# 注意：服务端按版本号 gating 新模型——旧版本（0.1.43/20260716）请求 glm-5.3
# 等新模型会被拒为 4001 "param is invalid"；升级后才返回真实路由（实测）。
_TRAE_APP_VERSION = "0.2.0"
_TRAE_APP_VERSION_CODE = "20260901"

# X-Ide-Version-Code 单独可调（默认 20260906）：2026-09 实测上游按这个头逐模型
# 门控能力——20260401 下新一代模型（glm-5.3 / kimi-k2.7-code / qwen3.8-max 等）
# 在 chat_v3 上整表 4001；glm-5.3 的放行阈值实测落在 20260801~20260815 之间，
# kimi-k2.7-code / Doubao-Seed-2.1-Pro 20260725 即放行。上游会持续按模型发布
# 节奏抬阈值，此值要跟随真实 Trae 客户端版本更新（可用环境变量覆盖）。
_TRAE_IDE_VERSION_CODE = os.environ.get("WB_TRAE_IDE_VERSION_CODE", "20260906")

# 原生 function calling 通道（2026-09 实测全模型可用，glm-5-turbo 除外——不在
# chat_v3 通道，自动走文本协议兜底）。WB_TRAE_NATIVE_TOOLS=0 可整体关闭。
_NATIVE_TOOLS_ENABLED = os.environ.get("WB_TRAE_NATIVE_TOOLS", "1").lower() not in ("0", "false", "off")
_NATIVE_FUNCTION = os.environ.get("WB_TRAE_NATIVE_FUNCTION", "chat_v3")

# 3 级端点回退（与 trae-local-api 一致）
ENDPOINTS = [
    "/api/agent/v3/llm_utils_chat",
    "/api/ide/v1/chat",
    "/api/agent/v3/create_agent_task",
]

# Trae Work 通道同端点最大尝试次数（含首次）：覆盖 IncompleteRead 等瞬态断流
_WORK_CHAT_MAX_ATTEMPTS = 3

# 模型名映射：外部别名 -> Trae 内部 config_name（大小写敏感，须与下方实测白名单一致）
MODEL_MAP: dict[str, str] = {
    # "claude-opus-4-7": "glm-5.3",
    # "claude-opus-4-6": "glm-5.3",
    # "claude-opus-4-5": "glm-5.3",
    # "claude-sonnet-4-6": "glm-5.3",
    # "claude-sonnet-4-5": "glm-5.3",
    # "claude-sonnet-4": "glm-5.3",
    # "claude-3.5-sonnet": "glm-5.3",
    # "claude-3.7-sonnet": "glm-5.3",
    # "claude-haiku-4-5": "DeepSeek-V4-Flash",
    # "mimo-v2.5-pro": "glm-5.3",
    # "mimo-v2.5": "glm-5.3",
    # "gpt-4o": "DeepSeek-V4-Pro",
    # "gpt-4o-mini": "DeepSeek-V4-Flash",
    # "gpt-4.1": "DeepSeek-V4-Pro",
    # "auto": "glm-5.3",
    # 大小写容错：Trae config_name 大小写不统一（DeepSeek-/Doubao- 为大写前缀），
    # 而 OpenAI 生态习惯全小写；小写请求若不在此映射，会落回默认 CodeBuddy 通道
    # （报错表现为 CodeBuddy 上游的安全审核/路由错误，而非 Trae 响应）
    # 对外模型 ID 一律小写；这里仅把上游要求大小写敏感的 config_name 转回原样。
    # 反例警示：不是所有上游名都要转大写——step-5-preview 上游只认全小写
    # （2026-10-07 实测，大写 4001），收录新模型前先 probe 原样名能不能通。
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
    "doubao-seed-evolving": "Doubao-Seed-Evolving",
    "doubao-seed-2.1-pro": "Doubao-Seed-2.1-Pro",
    "doubao-seed-2.1-turbo": "Doubao-Seed-2.1-Turbo",
    "doubao-seed-code": "Doubao-Seed-Code",
    # 注意 step-5-preview **不在此表**（原样透传）：上游只认全小写，见目录注释。
}

# 国内 Trae Work 模型目录，按原目录展示顺序平铺。
# 对外 ID 统一小写；大小写敏感的上游 config_name 由 MODEL_MAP 转换。
# 各项均为 2026-09 实测通过值（Work 凭证 + llm_utils_chat 端点）。
# kimi-k3 需会员 Pro+/Ultra/Express（免费账号 1005）；付费账号实测可用
# （2026-09-06 chat_v3 出流正常），故收录。
#
# glm-5.3-flashx（2026-09-30 补录）：上游已放行但本表原先漏收——它不在
# MODEL_MAP 里、模型名原样透传，所以「能通却不出现在 /v1/models」。
# 实测判定依据（三条一起看才排除了「别名模糊匹配」的解释）：
#   1. 源码中无任何 flashx 映射（原样透传，上游自己认这个名字）；
#   2. 上游精确匹配：glm-5.3-flashxx / glm-5.3-flashX 均被 4001
#      "param is invalid" 拒绝——若按前缀或大小写不敏感解析，这两个不会死；
#   3. 连续多次 200 且回真实 usage，自述为 Z.ai GLM 系。
# 注意 zcode 通道对同一模型返回 1311「套餐暂未开放」，别被名字相近误导：
# 两个通道是各自独立的授权，这里能通不代表 zcode 也能通。
#
# deepseek-v4.1-flash（2026-09-30 补录）：与 flashx 同一批漏收——源码无映射、
# 原样透传、上游已放行。它是新一代视觉模型（原生读图 + 1M ctx，上一代
# DeepSeek-V4-Pro 不支持图片输入）。
# **它确实是 V4.1 权重而不是 V4-Flash**（名字被上游接受 ≠ 服务的真是这个
# 模型，还得靠能力指纹区分——self-report 不可靠，V4-Flash 会自称
# "deepseek-chat"）。判定试验（各 5 张纯色 1x1 PNG 问颜色）：
#   deepseek-v4.1-flash  5/5 全对（红/绿/蓝/黄/青），且能读图答题；
#   deepseek-v4-flash    1/5，唯一"对"的那次是把固定幻觉"蓝色"撞上了蓝图；
#                        去掉图片它也答"蓝色"，直接问则承认「无图」。
# 若这个名字背后真是 V4-Flash 权重，视觉表现应与对照组一样烂——它没有。
# 另：上游对该名**精确匹配**（deepseek-v4.1 / v41 / 大写变体 / -flashx
# 全部 4001），说明走的是独立路由而非别名模糊匹配。
# Work 通道（solo_work_lite，即文本协议回落时经 ~/.ethan/trae_work.json
# 转投的那条）未实测——native chat_v3 已通，用不上；**不要**凭猜测往
# _WORK_FUNCTION_OVERRIDE 加条目，真失败时让它诚实地 4001 冒出来。
MODEL_IDS: list[str] = [
    "glm-5.3", "glm-5.3-flash", "glm-5.3-flashx",
    "deepseek-v4.1-flash", "doubao-seed-evolving", "kimi-k3",
    "doubao-seed-2.1-pro", "deepseek-v4-pro", "qwen3.8-max",
    "doubao-seed-2.1-turbo", "minimax-m3", "kimi-k2.6", "glm-5.1",
    "step-5-preview", "doubao-seed-code",
    # 2026-10-12 probe 实测收录（见下注）
    "mimo-v2.6-flash", "mimo-v2.6-pro", "qwen-3.7-plus",
    "kimi-k2.8-preview", "qwen3.8-flash",
]

# 模型积分倍率：整理自知识库《模型及成本整理-workbuddy-trae-含选用建议》
# （2026-09-05 版，WorkBuddy 定价截图）。别名（如 deepseek-v4-flash）由
# models() 按 MODEL_MAP 解析到内部名后取同一倍率。
# glm-5 / glm-5-turbo / glm-5.1 / kimi-k2.6 官方最新价目已下架，未收录。
# glm-5.3-flashx / deepseek-v4.1-flash：2026-09-30 由用户从 WorkBuddy 客户端
# 「模型倍率」面板截图取得（x0.31 / x0.08）。同图里 glm-5.3-flash x0.06 /
# kimi-k3 x1.83 / minimax-m3 x0.26 / qwen3.8-max x1.50 与本表完全吻合，口径一致。
# 注意同图部分旧模型与本表有小差（glm-5.3 / glm-5.2 显示 0.39、DeepSeek-V4-Flash
# 显示 0.10，均带「会员5折」徽章；V4-Pro 带「闲时折扣」徽章）——疑似促销期浮动，
# 本表维持知识库原值未动；若要跟价需定期截图更新，静态表追不动动态折扣。
# 2026-10-06 用户从客户端「模型倍率」面板截图全量跟价：Seed 系四档大动
# （Evolving / 2.1-Pro 0.77→0.08 带「限时 1 折」徽章；2.1-Turbo 0.10→0.20、
# Seed-Code 0.03→0.06 带「专属补贴」徽章）——促销/补贴价，回原价时以最新
# 截图为准再跟。面板里 Seed-2.1-Pro 显示为「Seed-2.1-Pro-0915」（带日期
# 后缀的版本名）；转发 config_name 维持实测通过的 Doubao-Seed-2.1-Pro，
# 若哪天上游只认带后缀的新名（4001）再实测补映射，**勿凭截图改名**。
# Step-5-Preview(x0.48)：用户确认上游新上架，同批收录（倍率同图）。
# 2026-10-07 probe 补实测：config_name 是**全小写 step-5-preview**（原样透传、
# 无 MODEL_MAP 映射）——大写 Step-5-Preview / Step5-Preview / Step-5.5-Preview
# 全部 4001 "param is invalid"（收录当天只进了倍率表、聊天没实测，首次转发即
# 4001 就是这么来的）。与 deepseek-v4.1-flash 同款「上游精确匹配小写字面量」，
# **不得**凭面板显示名加大写映射——那会把能用的名字改死。面板显示的
# "Step-5-Preview" 只是展示名。
# 2026-10-12 面板更新后 probe 补实测收录 5 个：mimo-v2.6-flash / mimo-v2.6-pro /
# qwen-3.7-plus（**带连字符**——qwen3.7-plus 精确匹配 4001，上游只认连字符名）
# 三个 solo_work_lite 直通；kimi-k2.8-preview / qwen3.8-flash 两个 solo_work_lite
# 4001、chat_v3 出流（进覆盖表）。仍拒收：qwen3.7-plus(无连字符) / kimi-k2.8 /
# 大写变体全 4001——名字以实测为准，勿照面板显示名抄。
MODEL_CREDITS: dict[str, str] = {
    "doubao-seed-evolving": "x0.08",
    "doubao-seed-2.1-pro": "x0.08",
    "doubao-seed-2.1-turbo": "x0.20",
    "doubao-seed-code": "x0.06",
    "step-5-preview": "x0.48",
    "glm-5.3-flash": "x0.06",
    "glm-5.3-flashx": "x0.31",
    "glm-5.3": "x0.39",
    "deepseek-v4.1-flash": "x0.08",
    "kimi-k3": "x1.83",
    "deepseek-v4-pro": "x0.72",
    "minimax-m3": "x0.26",
    "qwen3.8-max": "x1.50",
    # 2026-10-12 用户从客户端「模型倍率」面板截图跟价/收录
    "mimo-v2.6-flash": "x0.06",
    "mimo-v2.6-pro": "x0.15",
    "kimi-k2.8-preview": "x0.98",
    "qwen3.8-flash": "x0.08",
    "qwen-3.7-plus": "x0.25",
    # glm-5.3 面板 0.39（带「会员5折」徽章，原 0.40 小差）；deepseek-v4-flash
    # 0.10（「闲时折扣」）不进表——内部目录只有 deepseek-v4.1-flash
}

# 支持图片输入的模型（内部 config_name 口径）。
# 这些模型上游 llm_utils_chat 接受 OpenAI 风格 image_url（data URL）block。
#
# 注意：Trae 目录里的 DeepSeek-V4-Flash / V4-Pro **不支持**图片输入；
# 带图片能力的是新一代 deepseek-v4.1-flash（2026-09-30 实测收录：5 张纯色
# 1x1 PNG 问颜色 5/5 全对，且对照组 deepseek-v4-flash 确认读不了图——
# 判定过程见上方注释）。注意内部名是**全小写**
# ``deepseek-v4.1-flash``，大写 DeepSeek-V4.1-Flash 会被上游 4001。
# 若声明与实际不符，会导致 /v1/models 把纯文本模型报成可读图，客户端盲发
# 图片 → 上游 4001。
#
# 反向的漏报同样有害：未声明时 /v1/models 报 input_modalities=["text"]，
# 外部客户端（agent/IDE）据此自行判断（典型做法是按模型名匹配关键词）会
# 误剥图片，表现为「不支持读图」——即使模型本身支持。
#
# 未在此列表中的模型一律按不支持处理（保守）。
MODEL_SUPPORTS_IMAGES: set[str] = {"deepseek-v4.1-flash"}

# 实测计价（2026-09-30，官方「使用记录」对账法）：模型 -> (输入单价, 输出单价, 校准倍率)。
# 单价单位：积分/token，是**校准时刻的绝对值**（已含当时倍率与折扣）。
#
# 估算式：credits ≈ (in_rate×P + out_rate×C) × 当前倍率 / 校准倍率
# 「当前倍率」取 MODEL_CREDITS（人工跟官方倍率面板的那张表）——面板调价时
# 只改 MODEL_CREDITS，估算自动等比跟进，**不用重跑对账、不用改代码**。
# 元组第三项「校准倍率」是解单价当日 MODEL_CREDITS 的值（现为 x0.31/x0.08），
# 必须与校准时的面板值一致；哪天重跑对账，单价与校准倍率要**成对**更新。
# 注意：单看倍率比值无法区分「上游调倍率」与「上游调基价/折扣起止」——前者
# 估算自动跟对，后者会跟错（按比例缩放），偏差大到对不上账单时重跑对账即可。
#
# 背景：MODEL_CREDITS 的「100×倍率/1M 总 tokens」公式对这两个模型**不成立**——
# 官方真实计费对输入/输出分开计价（out ≈ 4×in），按面板倍率线性折算会差约 9 倍
# （flashx 247 tokens 官方收 0.07，公式只算出 0.008）。上表 x0.31/x0.08 保留作
# 目录展示（官方面板相对倍率），**积分估算走本表**。
#
# 测法：构造「大输入小输出」+「小输入大输出」各一条，与官方账单积分构成二元
# 方程组解出单价；再用其余官方记录回代验证——9 条命中 8 条（唯一偏差 0.0753
# vs 0.07，骑在舍入边界上，上游疑似 floor 而非 round）。
# 附带发现：
# - deepseek-v4.1-flash 在官方账单挂在「DeepSeek-V4-Flash 正式版」名下（计费
#   产品族归属，不代表权重是 V4——能力指纹已证是 V4.1）；
# - 面板 x0.08 带「会员5折」徽章；若按未折价 0.16 归一，两模型隐含基价接近
#   （in ≈2.1~2.6e-4、out ≈8.7~9.1e-4 /token/倍率）——猜测 5 折在账单外补偿，
#   未证实，勿当结论引用。若某天徽章消失、面板翻回 0.16，等比缩放会把估算
#   翻倍：真打折结束则跟对，若只是徽章口径变化则跟错（届时对账见真章）；
# - 断连的请求上游照常计费（实测一次 RemoteDisconnected 仍出账 2.16），对账时
#   注意本侧 metrics 没有它的 token 记录。
# 局限：in 价未细分缓存命中折扣（Claude Code 类大缓存流量会被高估），待有缓存
# 账单样本再拆。其余 trae 模型仍走倍率公式（其校准为 2026-09-05，若有偏差可用
# 同法复测）。
MEASURED_CREDIT_RATES: dict[str, tuple[float, float, float]] = {
    # 2026-10-12 二次对账（Claude Code 长会话流量，官方账单 02:57-02:58 的
    # 16 条 vs 本侧 metrics 同窗口逐条比价）：官方单条 2.04~2.33，对 prompt
    # 87k→95k / comp 38→1663 均不敏感；旧单价（7.98e-5/2.81e-4，2026-09-30
    # 校准）在该形态下估算 ~7.3/条，高出官方 3.4 倍。二元最小二乘重解：
    # in=2.351e-5（0.30x）、out=7.242e-5（0.26x）——两价同比缩 ~0.28，
    # 方向与「旧校准样本无缓存流量、本次全是大缓存会话」一致（cache 折扣
    # 之前就注明「待有缓存账单样本再拆」，这就是那份样本；Trae 的 chat 流
    # 不回缓存 token 数，代理侧仍拆不出 in/out 各自的缓存价，取混合单价）。
    # 注意：本次样本全是缓存命中的稳态请求，会话首请求（无缓存）按本表会
    # 低估（旧表反而准）——两形态没有单价能同时对准，取常见形态（长会话
    # 稳态）优先。官方账单条数比本侧请求少 ~20%（小额请求疑似合并出账），
    # 对账时以金额总量为准、勿逐条硬配。
    "glm-5.3-flashx": (2.351e-5, 7.242e-5, 0.31),
    "deepseek-v4.1-flash": (3.41e-5, 1.39e-4, 0.08),
}

# 部分模型在 solo_work_lite function 下不可用（服务端 4001），需改用 chat_v3。
# 实测（2026-09-03）：glm-5.1 / Doubao-Seed-Code 仅在 chat_v3 下可路由；
# glm-5.3-flash 同理——solo_work_lite 报 4001 "param is invalid"，
# chat_v3 正常出流（会话 s_20260903_2108_4190 即踩此坑）。
_WORK_FUNCTION_OVERRIDE: dict[str, str] = {
    "glm-5.1": "chat_v3",
    "Doubao-Seed-Code": "chat_v3",
    "glm-5.3-flash": "chat_v3",
    # 2026-10-12 probe：solo_work_lite 4001、chat_v3 出流
    "kimi-k2.8-preview": "chat_v3",
    "qwen3.8-flash": "chat_v3",
}

# 海外（global）的同款表。实测（2026-10-06）：GPT-5.6 系 / GLM / Kimi-K2.x /
# MiniMax 在 solo_work_lite 下 4001「param is invalid」、chat_v3 正常出流；
# GPT-6 系（sol/luna）/ GPT-5.x / kimi-k3 走 CN 同款默认 solo_work_lite 即可，
# 不进此表。两区的 function 绑定**互不通用**（CN 的 glm-5.3-flash 在
# solo_work_lite 4001，海外同名不存在；海外 glm-5.2 需要 chat_v3 而 CN 同名
# 不需要），所以按区域分表，取用走 work_function_override()。
#
# 只服务文本兜底通道（transport._send_trae_work_chat，默认 solo_work_lite）；
# **native 主通道不分表**（native_tools 固定 function=chat_v3）——2026-10-06
# 复测 chat_v3 对 gpt-6-sol / kimi-k3 亦 200 出流（gpt-5.6 系本就需要
# chat_v3），海外已收录模型在 chat_v3 下广谱可用，native 直通无回落。
#
# 【2026-10-07 更新】GPT-6 系（sol/luna）的 solo_work_lite 也整体 4001 了
# （文本兜底连非流式都被拒），且 chat_v3 的**流式**请求被上游整体拒绝
# （同 body 非流式 200，非频率限制——100+ 次请求按 stream 标志完美二分）。
# 海外模型现全靠 native chat_v3 非流式兜底：流式被拒时 provider 自动降级
# 非流式重试（见 provider._stream 的 4001 分支），文本协议只作最后防线。
_WORK_FUNCTION_OVERRIDE_INTL: dict[str, str] = {
    "gpt-5.6-sol": "chat_v3",
    "gpt-5.6-terra": "chat_v3",
    "gpt-5.6-luna": "chat_v3",
    "glm-5.2": "chat_v3",
    "minimax-m3": "chat_v3",
    # 2026-10-12 实测：solo_work_lite 4001、chat_v3 出流
    "gpt-5.5": "chat_v3",
    "gpt-6-astra": "chat_v3",
}


def work_function_override(region_key: str) -> dict[str, str]:
    """该区域的 Work function 覆盖表（未收录的模型走 solo_work_lite 默认）。"""
    if (region_key or "cn").strip().lower() == "global":
        return _WORK_FUNCTION_OVERRIDE_INTL
    return _WORK_FUNCTION_OVERRIDE

# ───────────────── 海外版（global）模型目录 ─────────────────
#
# 海外版是**另一套模型池**（Claude / GPT / Gemini 系为主），与上面 CN 表里的
# GLM / Doubao / Qwen 几乎不重叠，所以四张表都必须分开——CN 的 config_name、
# 分级、倍率、图片能力全是 CN 侧实测值，套到海外会两头出错（把海外没有的模型
# 报成可用 → 客户端请求后上游 4001；把海外模型漏报 → /v1/models 里看不见）。
#
# 这三张表只能由「拿海外账号真发请求」的结果填，不能照 CN 表推、也不能照
# 官网价目表抄——`deepseek-v4.1-pro` 就是反例（价目表上有、上游精确匹配拒绝）。
# 收录判据与 CN 侧一致：**上游认这个名字并回真实 usage**，必要时再补能力指纹。
# 表缺某个名字时模型名原样透传，上游不认就诚实地 4001 冒出来，
# 比猜一个名字糊弄过去好。
#
# 【2026-10-06 已填，probe 实录见下】
#
# 海外 config_name 全部为 2026-10-06 实测通过值（Work 凭证 + global 账号 +
# llm_utils_chat 端点，上游精确匹配 + 真实 reasoning/usage 才收录）。
# 两点与 CN 侧不同：
#
# 1. **海外端点要求 messages[].content 是内容块数组**（`[{"type": "text",
#    "text": ...}]`，Go 侧 `[]*idecopilot.LLMRawMessageContent`），纯字符串
#    直接 400 反序列化错——CN 两种都收。见 transport._intl_content_blocks。
# 2. **function 绑定与 CN 不同**：GPT-5.6 系 / GLM / Kimi-K2.x / MiniMax 在
#    CN 默认的 solo_work_lite 下 4001「param is invalid」，必须走 chat_v3
#    （与 CN 的 glm-5.1 同款现象）——见 _WORK_FUNCTION_OVERRIDE_INTL。
#    GPT-6 系（sol/luna）/ GPT-5.x / kimi-k3 则 solo_work_lite 直接可用。
#
# 反例记录（同日实测，三种 function 全 4001，**别再盲试**）：
#   （已推翻）`gpt-6-astra` 2026-10-06 曾记「三种 function 全拒」，实为
#   当日只试了 solo_work_lite 变体；2026-10-12 复测 chat_v3 出流，已收录。
#   `glm-5.3` / `glm-5.3-flash`（海外只有 GLM-5.2）；`deepseek-v4.1-flash` /
#   `DeepSeek-V4-Flash`；`gemini-3.1-pro-preview` / `gemini-3-flash-preview`；
#   `Seed-2.1-Turbo` / `Doubao-Seed-2.1-Turbo`。
# 上游对该端点的 model 名**精确匹配**（大小写/变体全 4001），与 CN 一致。
#
# 实测通但**用户决定不收录**（2026-10-06：模型太老）：`kimi-k2.7-code` /
# `kimi-k2.5` / `minimax-m2.7`（三者 chat_v3 均实测出流）；`gpt-5.2`（2026-10-07
# 用户点名移除：GPT-6/5.6 系在场没啥用）。别当漏收补回来。
MODEL_MAP_INTL: dict[str, str] = {}
MODEL_IDS_INTL: list[str] = [
    "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "kimi-k3", "glm-5.2", "minimax-m3",
]
# 海外是**次数制**（Premium 快速请求 N 次/月 + Basic 美元额度），没有 CN 的
# 积分倍率概念——留空，额度展示走 _quota_items 的海外分支（见 provider.py）。
MODEL_CREDITS_INTL: dict[str, str] = {}
# 图片能力未实测（probe 只发了纯文本）；空集 = 模型目录里不声明读图，
# 请求带图会被上游诚实拒绝，好过瞎声明。
MODEL_SUPPORTS_IMAGES_INTL: set[str] = set()

def model_tables(region_key: str = "cn"):
    """该区域的模型表（映射 / 模型目录 / 倍率 / 图片能力）。

    按 key **现取**模块级变量，不在导入时快照成列表：目录整体重新赋值时，
    调用方也会读取到最新配置。
    """
    if (region_key or "cn").strip().lower() == "global":
        return (MODEL_MAP_INTL, MODEL_IDS_INTL, MODEL_CREDITS_INTL,
                MODEL_SUPPORTS_IMAGES_INTL)
    return (MODEL_MAP, MODEL_IDS, MODEL_CREDITS, MODEL_SUPPORTS_IMAGES)


def map_model_for(region_key: str, requested: str) -> str:
    """按区域解析外部模型名 -> 上游 config_name（未命中则原样透传）。"""
    return model_tables(region_key)[0].get(requested, requested)


def _map_model(requested: str) -> str:
    return MODEL_MAP.get(requested, requested)



_DEBUG_SENSITIVE_KEYS = {"raw", "body", "content", "messages", "token", "uid", "tool_calls", "arguments"}


def _debug_dump(event: str, **kwargs: Any) -> None:
    """WB_DEBUG_DUMP=1 时记录安全诊断摘要，绝不落请求或响应原文。

    惰性 import state 避免模块级循环依赖；失败静默（调试设施不能影响主流程）。
    保留敏感文本的长度和短哈希，供关联同一故障使用。
    """
    if not os.environ.get("WB_DEBUG_DUMP"):
        return
    safe: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in _DEBUG_SENSITIVE_KEYS:
            raw = str(value).encode("utf-8", errors="replace")
            safe[f"{key}_bytes"] = len(raw)
            safe[f"{key}_sha256"] = hashlib.sha256(raw).hexdigest()[:16]
        else:
            safe[key] = value
    try:
        from ..core.state import get_state
        get_state().write_log(event, **safe)
    except Exception:
        pass


# 等待上游期间的心跳间隔（秒）；0 = 关闭心跳。
# 背景：Trae 上游是「整段缓冲」模式——send_trae_chat 同步读完整个 SSE 才返回，
# 生成期间客户端收不到任何数据。长生成（深度 review 大报告等）会触发下游
# 单 chunk 超时（实测 Ethan _CHUNK_TIMEOUT=120s → 回合中止、落库空回复）。
# _stream 在等待时按此间隔发一条 reasoning_content 心跳提示，既喂饱下游
# 超时计时器（续命），又让下游 UI 知道中转还在等。45s 意味着 120s 超时窗口
# 内至少有 2 次心跳，单次 SSE 分包延迟也不会误杀；每条 ~30 字节，开销可忽略。
TRAE_HEARTBEAT_INTERVAL = max(0, int(os.environ.get("WB_TRAE_HEARTBEAT_INTERVAL", "45")))
# 首个或相邻两个真实语义事件之间的最长等待；协议注释/保活不算模型进展。
TRAE_SEMANTIC_TIMEOUT = max(1, int(os.environ.get("WB_TRAE_SEMANTIC_TIMEOUT", "180")))
# 非流式请求整读的总时长上限。urllib 的 timeout 只约束单次 socket 阻塞，
# 上游慢速滴字（keepalive/分块）时 read() 会被无限拖延（实测 PAT 挂过 17min
# 才 502、零输出）。到点主动断开回 504。取值权衡：traepat 非流式健康请求
# p99≈310s、max≈622s（thinking 模型长生成），默认 300s 会牺牲极少数超长
# 成功换取挂死下界；批量场景可调大。
TRAE_NONSTREAM_MAX_S = max(60, int(os.environ.get("WB_TRAE_NONSTREAM_MAX_S", "300")))


def _heartbeat_text(waited: int) -> str:
    return f"⏳ [trae 中转] 上游模型仍在生成，已等待 {waited}s（流保持存活）…"

