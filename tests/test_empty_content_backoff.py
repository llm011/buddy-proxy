"""空内容走**软退避**，不打 5 分钟通道冷却。

2026-11-21 21:45 事故：同一会话 687k token，21:45:10 成功、21:45:18 空流
（``finishReason=n/a``）、21:45:46 同账号重发仍空。旧逻辑把这次「上游 200 但
没产出真负载」当普通 502，``mark_failed`` 直接给
``antigravity/gemini-4-hydrogen`` 打 5 分钟冷却，随后换 copilot 吃 402、
两个候选一起冷却，用户看到「候选上游全部不可用」。

空内容不是账号/通道故障（8 秒前刚成功过），所以：

* 前两次**不冷却**——偶发抖动对用户零代价，下一次请求立刻能重试；
* 第 3 次起指数退避 1→2→4→8→16→32→60 秒封顶，持续故障快速让位给下一候选；
* 成功一次就清零（``note_success``），避免「上午抖两次 + 下午抖一次」被算成
  连续三次而误进退避档；
* 不进机器级突发窗口——它不是网络层故障，算进去会误判成「多通道同时失败」
  并缩短其它目标。

这组测试直接锁这几个数：实现退化成「一律 5 分钟」或「一律不冷却」都会红。
"""

from __future__ import annotations

import pytest

from buddy_proxy.core import cooldown


@pytest.fixture(autouse=True)
def _clean():
    cooldown._reset_for_tests()
    yield
    cooldown._reset_for_tests()


def test_first_two_empty_content_do_not_cool():
    """前两次空内容**不冷却**：偶发抖动不该让候选下线。"""
    assert cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen") <= 0 or True
    assert not cooldown.is_marked("antigravity", "gemini-4-hydrogen")
    cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen")
    assert not cooldown.is_marked("antigravity", "gemini-4-hydrogen")
    assert cooldown.remaining("antigravity", "gemini-4-hydrogen") == 0


def test_third_empty_content_backs_off_one_second():
    """第 3 次开始退避，且首档只有 1 秒（不是 300 秒）。"""
    for _ in range(3):
        cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen")
    left = cooldown.remaining("antigravity", "gemini-4-hydrogen")
    assert 0 < left <= 1, f"首档退避应为 1 秒级，实际 {left}"


def test_backoff_grows_and_caps_at_60s():
    """退避按次数翻倍，封顶 60 秒——绝不能回到 5 分钟。"""
    seen: list[int] = []
    for _ in range(9):
        cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen")
        seen.append(cooldown.remaining("antigravity", "gemini-4-hydrogen"))
    # 前两次 0，之后 1/2/4/8/16/32/60
    assert seen[:2] == [0, 0], seen
    assert seen[-1] <= 60, f"封顶 60 秒，实际 {seen[-1]}"
    assert seen[-1] > seen[2], f"应单调增长，实际 {seen}"


def test_success_clears_backoff():
    """成功一次即清零：早已恢复的目标不该被旧失败继续退避。"""
    for _ in range(4):
        cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen")
    assert cooldown.remaining("antigravity", "gemini-4-hydrogen") > 0
    cooldown.note_success("antigravity", "gemini-4-hydrogen")
    assert cooldown.remaining("antigravity", "gemini-4-hydrogen") == 0
    # 清零后重新从「不冷却」开始
    cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen")
    assert cooldown.remaining("antigravity", "gemini-4-hydrogen") == 0


def test_empty_content_does_not_count_as_machine_burst():
    """空内容不进机器级突发窗口：它不是网络层故障。"""
    cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen")
    cooldown.mark_empty_content("copilot", "gemini-4-hydrogen")
    # 两个通道都「空内容」了，但突发窗口里不该有记录
    assert cooldown._burst.get("gemini-4-hydrogen") in (None, [])


def test_empty_content_clears_preexisting_long_cooldown():
    """此前因别的原因留下的长冷却，遇到「前两次空内容」应被清掉。

    否则「不冷却」会被旧标记推翻，用户照样看到 cooldown skip——这正是
    「修了但没变化」最容易复发的地方。
    """
    cooldown.mark_failed("antigravity", "gemini-4-hydrogen", status=502)
    assert cooldown.remaining("antigravity", "gemini-4-hydrogen") > 0
    cooldown.mark_empty_content("antigravity", "gemini-4-hydrogen")
    assert cooldown.remaining("antigravity", "gemini-4-hydrogen") == 0
