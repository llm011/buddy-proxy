

def test_qwen4_max_is_listed_locally():
    """``qwen4-max`` 上游目录里没有，但实测可用，要主动列出来。

    2026-11-12 实测：上游 ``/v1/models`` 只报 9 个模型，不含 qwen4-max；
    但直接点名调用能通（返回正常文本，``billable: false`` 的内测档）。
    不加进 ``LOCAL_EXTRA_MODELS`` 的话，用户只能靠「裸名硬发」调用，
    在管理页和客户端选择器里都看不见。
    """
    from buddy_proxy.qoder.catalog import LOCAL_EXTRA_MODELS

    keys = [str(m.get("key") or "") for m in LOCAL_EXTRA_MODELS]
    assert "qwen4-max" in keys


def test_local_extra_models_do_not_invent_metadata():
    """补充条目不编造上游没给的能力字段。

    上游没声明 max_input / 是否支持图片，就不该填一个数字——客户端会当真，
    长上下文请求可能被误判。
    """
    from buddy_proxy.qoder.catalog import LOCAL_EXTRA_MODELS

    for m in LOCAL_EXTRA_MODELS:
        assert not m.get("max_input_tokens"), f"{m.get('key')} 不该编造上下文窗口"
        assert not m.get("is_vl"), f"{m.get('key')} 不该断言支持图片"
