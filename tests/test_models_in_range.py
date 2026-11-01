"""``modelsInRange`` 只收「真实有请求」的桶。

后端为补上 dumate 的积分图，会往 ``STATS.model_daily`` 里塞
``{provider:'dumate', model:'', count:0}`` 的**账号级消耗**合成桶（见
``queries._inject_dumate_credits``）。这些桶不是任何模型的请求，进 RANGE_MODELS
会在耗时图 / 型号占比 / Top10 / 筛选区里凭空多出一个空模型名——2026-11-01 回归。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

APP_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/buddy_proxy/web/static/app_state.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

MODELS_RE = re.compile(r"function modelsInRange\(\) \{[\s\S]*?\n\}\n", re.S)


def _models(model_daily: list[dict]) -> list[dict]:
    text = APP_JS.read_text(encoding="utf-8")
    m = MODELS_RE.search(text)
    assert m, "app_state.js 里找不到 modelsInRange"
    script = f"""
const STATS = {{model_daily: {json.dumps(model_daily)}}};
function inRange(date) {{ return true; }}
{m.group(0)}
console.log(JSON.stringify(modelsInRange()));
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, f"node 执行失败:\n{r.stderr[:2000]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_zero_count_synthetic_bucket_is_excluded():
    """dumate 的账号级合成桶（model 空、count=0）不该进流量视图。"""
    out = _models([
        {"date": "2026-11-01", "provider": "dumate", "model": "", "count": 0,
         "credit_sum": 27471.2, "credit_est_sum": 0, "errors": 0,
         "duration_ms_sum": 0, "duration_ms_max": 0, "prompt_tokens": 0,
         "completion_tokens": 0, "last_ts": 0},
        {"date": "2026-11-01", "provider": "dumate", "model": "kimi-k3", "count": 5,
         "credit_sum": 0, "credit_est_sum": 0, "errors": 0,
         "duration_ms_sum": 10, "duration_ms_max": 10, "prompt_tokens": 1,
         "completion_tokens": 1, "last_ts": 1},
    ])
    assert [m["model"] for m in out] == ["kimi-k3"], "空模型的合成桶不该出现"


def test_real_requests_still_pass_through():
    out = _models([
        {"date": "2026-11-01", "provider": "codebuddy", "model": "a", "count": 3,
         "credit_sum": 0, "credit_est_sum": 0, "errors": 0, "duration_ms_sum": 30,
         "duration_ms_max": 20, "prompt_tokens": 2, "completion_tokens": 2, "last_ts": 2},
    ])
    assert len(out) == 1 and out[0]["model"] == "a" and out[0]["count"] == 3
    assert out[0]["avg_ms"] == 10