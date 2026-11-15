"""``buddy usage`` / ``buddy doctor`` 的离线测试。

锁住的都是**曾经踩过的坑**，不是在背实现细节：

1. 额度标签的前缀可能是通道名、缩短名或 uuid 指纹——都当账号名显示的话，
   用户看到的是「Qoder #1 Qoder」这种自己认不出的名字。
2. 服务没起时要退化成「本地账号清单」，而不是一句「额度接口不可用」。
3. 告警区：Trae 每天一个签到包，逐条列会刷屏；Kimi 的 5 小时窗口被后端
   误判成低额，要标注而不是打红。
4. doctor 的指标目录必须与 ``buddy logs`` 同口径，不能自己推。
"""
from __future__ import annotations

import argparse
import json

from buddy_proxy.cli import doctor as cli_doctor
from buddy_proxy.cli import usage as cli_usage
from buddy_proxy.cli.render import Palette

NO_COLOR = Palette(False)


# ---------------------------------------------------------------------------
# 标签 → 账号前缀
# ---------------------------------------------------------------------------


def test_account_prefix_drops_provider_name():
    assert cli_usage.account_prefix("Trae #1 · 签到奖励", "Trae") == ""
    # 缩短名：前缀是通道名的一部分，也不是账号名
    assert cli_usage.account_prefix("Qoder #1 · 资源包", "Qoder 海外版") == ""
    # 机翻名：前缀和通道 id 毫无字面关系，只能靠通道名表认出来
    assert cli_usage.account_prefix("CodeBuddy #3 · 资源包", "codebuddy") == ""
    assert cli_usage.account_prefix(
        "baidu-dumate #4 · 日用量", "baidu-dumate",) == ""
    table = {"dumate": "DuMate"}
    assert cli_usage.account_prefix("DuMate #4 · 日用量", "dumate",
                                    names=table) == ""
    # 前缀是通道 id 的 uuid 前 8 位
    assert cli_usage.account_prefix(
        "46D8527F #2 · 资源包", "46d8527f-c817-4113-b2fc-347148a4e4e7") == ""


def test_account_prefix_keeps_real_name():
    assert cli_usage.account_prefix("alice@example.com #2 · 周额", "ZenMux") \
        == "alice@example.com"
    assert cli_usage.account_prefix("资源包", "Kimi") == ""


def test_prefer_local_name_falls_back():
    assert cli_usage._prefer_local_name("56624506-3", "56624506") == "56624506-3"
    assert cli_usage._prefer_local_name(None, "abc") == "abc"
    assert cli_usage._prefer_local_name("", "") == ""


# ---------------------------------------------------------------------------
# 快照 → 视图
# ---------------------------------------------------------------------------


def _snapshot():
    item = lambda label, remaining, total, unit="credit": {  # noqa: E731
        "label": label, "remaining": remaining, "total": total,
        "percent": 50, "unit": unit, "reset_ts": 0, "expire_ts": 0,
    }
    return {
        "providers": [
            {"id": "trae", "name": "Trae", "quota": {"supported": True, "items": [
                item("Trae #1 · 签到奖励", 100, 200),
                item("Trae #2 · 签到奖励", 100, 200),
                item("未分账号额度", 5, 10),
            ]}},
            {"id": "kimi", "name": "Kimi", "quota": {"supported": True,
                                                    "items": [item("Kimi #1 · 周额", 3, 100)]}},
            {"id": "off", "name": "Offline", "disabled": True, "quota": {}},
        ],
        "expiring": [
            {"provider": "trae", "label": "签到奖励", "remaining": 100, "unit": "credit",
             "days_left": 2, "expire_ts": 1_760_000_000},
        ],
        "low_quota": [
            {"provider": "kimi", "label": "5 小时窗口", "remaining": 99,
             "percent_left": 99},
            {"provider": "kimi", "label": "周额", "remaining": 0, "percent_left": 0},
        ],
        "expiry_warn_days": 7,
        "expiry_urgent_days": 4,
    }


def test_build_usage_groups_by_account():
    view = cli_usage.build_usage(_snapshot())
    assert view["source"] == "service"
    by = {g["provider"]: g for g in view["providers"]}
    assert [a["account"] for a in by["trae"]["accounts"]] == ["#1", "#2"]
    # 不带 ``#N`` 的条目进「未分账号」，不能硬塞给某个号
    assert len(by["trae"]["unassigned"]) == 1
    assert by["off"]["disabled"] is True


def test_build_usage_provider_filter_also_filters_warnings():
    view = cli_usage.build_usage(_snapshot(), providers=["kimi"])
    assert [g["provider"] for g in view["providers"]] == ["kimi"]
    assert {e["provider"] for e in view["expiring"]} == set()
    assert {e["provider"] for e in view["low_quota"]} == {"kimi"}


def test_build_usage_accounts_filter():
    view = cli_usage.build_usage(_snapshot(), accounts=["#2"])
    by = {g["provider"]: g for g in view["providers"]}
    assert [a["account"] for a in by["trae"]["accounts"]] == ["#2"]
    assert "kimi" not in by  # 没有 #2 的通道整组消失


def test_build_usage_merges_local_cooling():
    local = [{"provider": "trae", "index": 1, "name": "main",
              "token": "ok", "hours_left": 5,
              "cooling": [{"kind": "429", "minutes_left": 10}]}]
    view = cli_usage.build_usage(_snapshot(), local_accounts=local)
    acc = next(g for g in view["providers"] if g["provider"] == "trae")["accounts"][0]
    assert acc["name"] == "main"  # 本地名字优先于标签前缀
    assert acc["cooling"][0]["minutes_left"] == 10
    assert acc["hours_left"] == 5


def test_build_usage_offline_degrades_to_local():
    local = [{"provider": "codebuddy", "index": 3, "name": "56624506-3",
              "token": "ok", "hours_left": 48, "cooling": []}]
    view = cli_usage.build_usage(None, local_accounts=local)
    assert view["source"] == "local"
    assert view["local_providers"] == ["codebuddy"]
    g = view["providers"][0]
    assert g["local_only"] is True
    assert g["accounts"][0]["name"] == "56624506-3"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_offline_hint_and_hidden_channels():
    view = cli_usage.build_usage(None, local_accounts=[])
    text = "\n".join(cli_usage.render_text(view, NO_COLOR))
    assert "服务未运行" in text
    # 离线且一个本地号都没有：别假装有通道，直接说清楚下一步
    assert "没有匹配的通道" in text

    # 有本地号时通道照常列，且标明「只有本地账号状态」
    local = [{"provider": "codebuddy", "index": 1, "name": "mine",
              "token": "ok", "hours_left": 48, "cooling": []}]
    view = cli_usage.build_usage(None, local_accounts=local)
    text = "\n".join(cli_usage.render_text(view, NO_COLOR))
    assert "只列出本地账号" in text
    assert "mine" in text


def test_render_hides_channels_without_anything():
    view = cli_usage.build_usage({"providers": [
        {"id": "empty", "name": "Empty", "quota": {}},
        {"id": "kimi", "name": "Kimi",
         "quota": {"supported": True, "items": [{
             "label": "Kimi #1 · 周额", "remaining": 3, "total": 100,
             "percent": 97, "unit": "credit"}]}},
    ]})
    text = "\n".join(cli_usage.render_text(view, NO_COLOR))
    assert "Empty" not in text
    assert "另有 1 个通道" in text
    assert "Empty" in "\n".join(cli_usage.render_text(view, NO_COLOR, show_empty=True))


def test_condense_merges_identical_items():
    items = [{"item_label": "签到奖励", "unit": "credit", "remaining": 100,
              "total": 200, "percent": 50, "expire_ts": ts}
             for ts in (1_700_000_000, 1_700_100_000, 1_700_200_000)]
    out = cli_usage._condense(items)
    assert len(out) == 1
    assert out[0]["_count"] == 3
    assert out[0]["expire_ts"] == 1_700_200_000
    assert out[0]["expire_first_ts"] == 1_700_000_000


def test_condense_folds_overflow():
    items = [{"item_label": f"包{i}", "unit": "credit", "remaining": 1,
              "total": 100 - i, "percent": 1} for i in range(20)]
    out = cli_usage._condense(items)
    assert len(out) == cli_usage._MAX_ITEM_ROWS
    assert out[-1]["_summary"] is True
    assert out[-1]["item_label"] == f"还有 {20 - cli_usage._MAX_ITEM_ROWS + 1} 项"


def test_message_items_stay_out_of_tables():
    view = cli_usage.build_usage({"providers": [{
        "id": "copilot", "name": "Copilot",
        "quota": {"supported": True, "items": [
            {"label": "Copilot #1 · 额度", "remaining": "未登录，请先 buddy login"}
        ]}}]})
    rendered = "\n".join(cli_usage._items_table(
        view["providers"][0]["accounts"][0]["items"], NO_COLOR))
    assert rendered == ""


def test_warnings_merge_and_annotate():
    expiring = [
        {"provider": "trae", "provider_name": "Trae", "label": "签到奖励",
         "remaining": 100, "unit": "credit", "days_left": 1,
         "expire_ts": 1_763_300_000},
    ] * 4
    expiring[0] = {**expiring[0], "expire_ts": 1_763_300_000}
    expiring[-1] = {**expiring[-1], "expire_ts": 1_763_500_000}
    view = {"expiring": expiring, "expiry_warn_days": 7, "expiry_urgent_days": 4,
            "low_quota": [{"provider": "kimi", "provider_name": "Kimi",
                           "label": "5 小时窗口", "remaining": 99,
                           "percent_left": 99}]}
    text = "\n".join(cli_usage._render_warnings(view, NO_COLOR))
    assert "签到奖励 ×4" in text
    assert "剩 100credit ×4" not in text  # 数量挂在名字上，金额只写一次
    assert "~" in text  # 合并后写时间区间
    assert "该窗口周期短" in text
    assert "将在 7 天内到期" in text


def test_warnings_overflow_summary():
    view = {"expiring": [
        {"provider": f"p{i}", "provider_name": f"P{i}", "label": "包",
         "remaining": 1, "unit": "credit", "days_left": 1,
         "expire_ts": 1_763_300_000} for i in range(cli_usage._WARN_LINES + 3)],
        "expiry_warn_days": 7, "expiry_urgent_days": 4, "low_quota": []}
    text = "\n".join(cli_usage._render_warnings(view, NO_COLOR))
    assert "另有 3 条" in text


def test_exit_code():
    assert cli_usage.compute_exit_code({"expiring": [], "low_quota": []}) == 0
    assert cli_usage.compute_exit_code({"expiring": [], "low_quota": [{"provider": "x"}]}) == 2
    assert cli_usage.compute_exit_code({
        "expiring": [{"days_left": 1}], "expiry_urgent_days": 4}) == 2
    # 提醒期（超过加急天数）还不算紧急
    assert cli_usage.compute_exit_code({
        "expiring": [{"days_left": 6}], "expiry_urgent_days": 4}) == 0


def _usage_args(**kw):
    ns = argparse.Namespace(provider=None, account=None, refresh=None, all=False,
                            list_providers=False, base_url="http://127.0.0.1:9",
                            json=False, no_color=True)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_run_rejects_unknown_provider(capsys):
    assert cli_usage.run(_usage_args(provider=["nope"])) == 1
    assert "未知通道" in capsys.readouterr().err


def test_run_json_offline(monkeypatch, capsys):
    # 端口 9 上没有服务：fetch 必须快速失败并退化成 local，而不是报错退出
    monkeypatch.setattr(cli_usage, "_collect_local_accounts", lambda: [])
    assert cli_usage.run(_usage_args(json=True)) in (0, 2)
    assert json.loads(capsys.readouterr().out)["source"] == "local"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_settings_report_flags_broken_json(tmp_path):
    (tmp_path / "settings.json").write_text("{oops", encoding="utf-8")
    (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    rows = {r[0]: r for r in cli_doctor._settings_report(tmp_path)}
    assert rows["settings.json"][1] == "语法错误"
    assert "第 1 行" in rows["settings.json"][2]
    assert rows["ok.json"][1] == "正常"
    assert rows["empty.json"][1] == "正常"


def test_metrics_report_uses_logs_dir(tmp_path):
    """指标目录由调用方按服务口径给出，不能从 state_dir 自己推。"""
    state, logs = tmp_path / "state", tmp_path / "repo" / "logs"
    state.mkdir()
    logs.mkdir(parents=True)
    (logs / "metrics.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    (logs / "metrics.jsonl.2026-01-01").write_text("{}\n", encoding="utf-8")
    (logs / "unrelated.log").write_text("x", encoding="utf-8")

    m = cli_doctor._metrics_report(state, logs)
    assert m["exists"] is True
    assert m["lines"] == 2
    assert m["archives"] == 1
    # state 目录里的文件与指标目录的文件都要列出来（两边合并）
    names = {r[0] for r in cli_doctor._log_files(state, logs)[0]}
    assert {"metrics.jsonl", "unrelated.log"} <= names
    assert cli_doctor._metrics_report(state)["exists"] is False


def test_doctor_run_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_doctor, "local_channels", lambda: [])
    monkeypatch.setattr(cli_doctor, "_port_listening", lambda *a: False)
    from buddy_proxy.cli import logs as cli_logs
    monkeypatch.setattr(cli_logs, "metrics_dir", lambda: tmp_path)
    # 端口关闭 → 1（即使配置正常）
    assert cli_doctor.run(argparse.Namespace(port=1, json=False, no_color=True)) == 1
    capsys.readouterr()  # 丢掉第一段的文本输出，下面只看 JSON

    monkeypatch.setattr(cli_doctor, "_port_listening", lambda *a: True)
    monkeypatch.setattr(cli_doctor, "_http_json", lambda *a, **k: {"providers": {}})
    # run() 不接 state_dir 参数，坏配置要放进真实 state 目录才测得到；
    # 用一个指向 tmp 的假 state 目录，避免动到本机 ~/.buddy-proxy
    import buddy_proxy.core.paths as paths
    state = tmp_path / "state"
    state.mkdir()
    (state / "settings.json").write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(paths, "state_dir", lambda: state)
    assert cli_doctor.run(argparse.Namespace(port=1, json=True, no_color=True)) == 1
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["metrics"]["exists"] is False
    assert report["settings"][0][1] == "语法错误"


def test_doctor_renders_dict_providers(tmp_path):
    report = {
        "port": 8787, "listening": True, "state_dir": str(tmp_path),
        "health": {"providers": {"codebuddy": {"id": "codebuddy", "configured": True},
                                 "copilot": {"id": "copilot", "configured": False}}},
        "settings": [], "channels": [], "metrics": {"exists": False},
        "logs": [], "logs_total_bytes": 0,
    }
    text = "\n".join(cli_doctor.render(report, NO_COLOR))
    assert "已配置通道 1 个" in text
    assert "codebuddy" in text
    assert "号" not in text.split("已配置通道")[1].splitlines()[0]  # 不再编造账号数
