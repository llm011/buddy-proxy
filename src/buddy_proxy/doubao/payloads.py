"""豆包工作 ``/chat/completion`` 请求体构造（classic / agent 两种形态）。

2026-10 自 ``cdp_client.py`` 拆出：两个构造函数都是纯函数（不读
``self``），落成模块级函数；旧路径 ``buddy_proxy.doubao.cdp_client``
保持 re-export 兼容。
"""

from __future__ import annotations

import json
import uuid
from typing import Any


def _build_classic_payload(
    text: str,
    use_deep_think: int,
    need_create: bool,
    bot_id: str,
    conversation_id: str,
    now_ms: int,
    now_sec: int,
) -> dict[str, Any]:
    """经典管线 payload（原样保留：服务端按默认豆包模型路由）。"""
    return {
        "client_meta": {
            "local_conversation_id": f"local_{uuid.uuid4().int % 10**16}" if need_create else "",
            "conversation_id": conversation_id,
            "bot_id": bot_id,
            "last_section_id": "",
            "last_message_index": None,
        },
        "messages": [{
            "local_message_id": str(uuid.uuid4()),
            "content_block": [{
                "block_type": 10000,
                "content": {
                    "text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""},
                    "pc_event_block": "",
                },
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
            }],
            "message_status": 0,
        }],
        "option": {
            "send_message_scene": "",
            "create_time_ms": now_ms,
            "collect_id": "",
            "is_audio": False,
            "answer_with_suggest": False,
            "tts_switch": False,
            "need_deep_think": use_deep_think,
            "click_clear_context": False,
            "from_suggest": False,
            "is_regen": False,
            "is_replace": False,
            "disable_sse_cache": False,
            "select_text_action": "",
            "resend_for_regen": False,
            "scene_type": 0,
            "unique_key": str(uuid.uuid4()),
            "start_seq": 0,
            "need_create_conversation": need_create,
            "regen_query_id": [],
            "edit_query_id": [],
            "regen_instruction": "",
            "no_replace_for_regen": False,
            "message_from": 0,
            "shared_app_name": "",
            "shared_app_id": "",
            "sse_recv_event_options": {"support_chunk_delta": True},
            "is_ai_playground": False,
            "recovery_option": {
                "is_recovery": False,
                "req_create_time_sec": now_sec,
                "append_sse_event_scene": 0,
            },
            "message_storage_type": 0,
        },
        "ext": {
            "use_deep_think": str(use_deep_think),
            "fp": "",
            "collection_id": "",
            "commerce_credit_config_enable": "0",
            "sub_conv_firstmet_type": "1" if need_create else "0",
        },
    }


#: ``person-*`` 形态的模型（2026-11 新菜单）在 ``need_deep_think`` 上的固定值。
#:
#: 实测（2026-11-13 抓包 person-deepseek-v4-1-flash-261017）App 传的就是
#: 10001。它与经典管线的 0/1/3 思考枚举不是一个语义域——新菜单的推理强度由
#: ``model_config.reasoning_effort`` 表达，``need_deep_think`` 退化成占位。
_PERSON_MODEL_DEEP_THINK = 10001


def _build_agent_payload(
    text: str,
    model_spec: dict[str, Any],
    need_create: bool,
    bot_id: str,
    conversation_id: str,
    now_ms: int,
    now_sec: int,
) -> dict[str, Any]:
    """agent 管线 payload —— App 选定具体模型时的同款协议（2026-09 抓包）。

    与经典管线的差异：``option.agent_mode=1``、``option.model_config``、
    ``option.aggregate_params``、``option.general_task_param`` 与
    ``ext.agent_mode`` / ``ext.general_task_param``。实测经典管线会忽略
    模型字段，只有 agent 管线按 ``model_item_key`` 路由。
    """
    item_key = str(model_spec["item_key"])
    extra = model_spec.get("extra") or {}
    provider = model_spec.get("provider") or ""
    effort = int(model_spec.get("reasoning_effort", 5))
    # agent 管线里 need_deep_think/use_deep_think 就是 model_item_key
    # （App 抓包：自动=9、Orange=6），不是经典管线的 0/1/3 思考枚举。
    #
    # ⚠ **key 有两种形态**（2026-11-13 抓包确认）：
    #
    #   旧：纯数字，如 ``"4"``（Turbo）/``"6"``（Orange）/``"9"``（Auto）
    #   新：``person-<model-id>``，如 ``"person-deepseek-v4-1-flash-261017"``
    #
    # 新形态直接 ``int()`` 会 ValueError。App 在这种模型上根本**不传**
    # ``need_deep_think`` 的数字语义——它仍带着这个字段，但值不再是 key。
    # 实测新模型请求里 ``need_deep_think: 10001``（固定值），与经典管线的
    # 0/1/3 完全不同。所以这里按「能不能转 int」分派：
    # 数字 key 沿用旧语义；``person-*`` 走 10001。
    try:
        deep_think: Any = int(item_key)
    except ValueError:
        deep_think = _PERSON_MODEL_DEEP_THINK

    local_message_id = str(uuid.uuid4())
    # 新会话 runtime_type=2 + 不修改现有会话；续聊 runtime_type=1 +
    # need_modify_conversation=True（App 抓包实测：反过来会导致服务端把
    # "新会话"消息并进当前最近会话，多轮串话）
    general_task_param = {
        "action": 0,
        "thread_local_message_id": [local_message_id],
        "client_option": {"enable_sandbox": False, "os": "Mac", "shared_folder_path": []},
        "runtime_type": 2 if need_create else 1,
        "agent_task_param": {"runtime_type": 2 if need_create else 1},
        "agent_task_param_change": {
            "runtime_changed": True, "device_changed": False,
            "sandbox_auth_type_changed": False,
        },
        "need_modify_conversation": not need_create,
        "task_input_json": "{\"project_context\":{}}",
    }
    ext: dict[str, Any] = {
        "general_task_param": json.dumps(general_task_param, ensure_ascii=False),
        "use_deep_think": str(deep_think),
        "agent_mode": "1",
        "collection_id": "",
        "is_finish": "1",
        "commerce_credit_config_enable": "0",
    }
    if need_create:
        # App 新建会话时携带（实测缺失不会报错，但带上与 App 行为一致）
        ext["conversation_init_option"] = "{\"need_ack_conversation\":true}"
        ext["sub_conv_firstmet_type"] = "1"
    return {
        "client_meta": {
            "local_conversation_id": f"local_{uuid.uuid4().int % 10**16}" if need_create else "",
            "conversation_id": conversation_id,
            "bot_id": bot_id,
            "last_section_id": "",
            "last_message_index": None,
        },
        "messages": [{
            "local_message_id": local_message_id,
            "content_block": [{
                "block_type": 10000,
                "content": {
                    "text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""},
                    "pc_event_block": "",
                },
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
            }],
            "message_status": 0,
        }],
        "option": {
            "send_message_scene": "",
            "create_time_ms": now_ms,
            "collect_id": "",
            "is_audio": False,
            "answer_with_suggest": False,
            "agent_mode": 1,
            "tts_switch": False,
            "need_deep_think": deep_think,
            "click_clear_context": False,
            "from_suggest": False,
            "is_regen": False,
            "is_replace": False,
            "is_from_click_option": False,
            "is_from_click_softlink": False,
            "disable_sse_cache": False,
            "select_text_action": "",
            "is_select_text": False,
            "resend_for_regen": False,
            "scene_type": 0,
            "unique_key": str(uuid.uuid4()),
            "start_seq": 0,
            "need_create_conversation": need_create,
            "regen_query_id": [],
            "edit_query_id": [],
            "regen_instruction": "",
            "no_replace_for_regen": False,
            "message_from": 0,
            "shared_app_name": "",
            "shared_app_id": "",
            "sse_recv_event_options": {"support_chunk_delta": True},
            "is_ai_playground": False,
            "is_old_user": False,
            "user_ug_source_info": {
                "channel": "1001", "landing_page_url": "", "source": "1001",
                "channel_v2": "1001", "landing_page_url_v2": "", "source_v2": "1001",
            },
            "general_task_param": general_task_param,
            "recovery_option": {
                "is_recovery": False,
                "req_create_time_sec": now_sec,
                "append_sse_event_scene": 0,
            },
            "message_storage_type": 0,
            "related_deleted_message_ids": {},
            "connector_info_list": [],
            "model_config": {
                "model_item_key": item_key,
                "model_extra_params": extra,
                "reasoning_effort": effort,
            },
            "aggregate_params": {
                "conversation_mode": "",
                "mode_id": "3",
                "model_item_key": item_key,
                "agent_mode": "1",
                "reasoning_effort": str(effort),
                "provider_id": provider,
            },
        },
        "user_context": [],
        "ext": ext,
    }


