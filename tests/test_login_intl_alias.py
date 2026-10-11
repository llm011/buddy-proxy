"""登录入口的 traeintl/qoderintl/codebuddyintl 别名：隐含 region=global、区域冲突报错。

海外版没有独立账号体系（共用 work 账号池按 region 落盘），「buddy login
traeintl」是最直觉的敲法——别名落到 trae 并隐含 global，重启后 traeintl
通道自动注册（intl_enabled()）。codebuddyintl 同理（海外 session 落独立
文件 ~/.codebuddy-session-global.json）。
"""
from __future__ import annotations

import pytest

from buddy_proxy.auth.login import _resolve_provider_region


def test_traeintl_alias_implies_global():
    assert _resolve_provider_region("traeintl", None) == ("trae", "global")


def test_qoderintl_alias_implies_global():
    assert _resolve_provider_region("qoderintl", None) == ("qoder", "global")


def test_codebuddyintl_alias_implies_global():
    assert _resolve_provider_region("codebuddyintl", None) == ("codebuddy", "global")


def test_intl_alias_with_explicit_global_ok():
    assert _resolve_provider_region("traeintl", "global") == ("trae", "global")


def test_intl_alias_conflicting_region_raises():
    with pytest.raises(ValueError, match="冲突"):
        _resolve_provider_region("traeintl", "cn")


def test_plain_provider_region_passthrough():
    assert _resolve_provider_region("trae", "global") == ("trae", "global")
    assert _resolve_provider_region("trae", None) == ("trae", None)
    assert _resolve_provider_region("qoder", "cn") == ("qoder", "cn")


def test_existing_aliases_untouched():
    assert _resolve_provider_region("workbuddy", None) == ("codebuddy", None)
    assert _resolve_provider_region("quoder", "global") == ("qoder", "global")


def test_case_and_whitespace_normalized():
    assert _resolve_provider_region("  TRAEINTL ", None) == ("trae", "global")
