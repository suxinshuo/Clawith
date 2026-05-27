"""Tests for Feishu card builders, send_card_with_fallback, and card.action.trigger handling.

NOTE: All app.* imports are deferred into test functions to avoid triggering the
pre-existing circular import between app.services.agent_tools and app.services.llm.caller
on test collection.
"""

import json
import uuid

import pytest


def _extract_buttons(card: dict) -> list[dict]:
    """Walk the card body and return all `tag: button` elements. Schema 2.0
    lays them out via column_set/column wrappers, so a flat scan is needed."""
    out: list[dict] = []

    def visit(el):
        if not isinstance(el, dict):
            return
        if el.get("tag") == "button":
            out.append(el)
            return
        for child in el.get("columns") or []:
            for sub in child.get("elements") or []:
                visit(sub)
        for sub in el.get("elements") or []:
            visit(sub)

    for e in card["body"]["elements"]:
        visit(e)
    return out


# ─── Card builder snapshots ────────────────────────────────────────


def test_build_kv_card_shape():
    from app.services.feishu_service import build_kv_card
    card = build_kv_card(
        title="Daily Status",
        fields=[
            {"key": "Owner", "value": "Alice", "color": "green"},
            {"key": "Due", "value": "2026-06-01"},
        ],
        summary="OKR sync",
    )
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "Daily Status"
    assert card["config"]["summary"]["content"] == "OKR sync"
    body_md = card["body"]["elements"][0]["content"]
    assert "**Owner**: <font color='green'>Alice</font>" in body_md
    assert "**Due**: 2026-06-01" in body_md


def test_build_kv_card_empty_fields():
    from app.services.feishu_service import build_kv_card
    card = build_kv_card("T", [])
    assert "（无字段）" in card["body"]["elements"][0]["content"]


def test_build_actions_card_button_callback_protocol():
    from app.services.feishu_service import build_actions_card
    aid = str(uuid.uuid4())
    card = build_actions_card(
        title="Decide",
        body="Pick one",
        actions=[
            {"label": "继续", "action_id": "go", "style": "primary", "context": {"k": "v"}},
            {"label": "取消", "action_id": "cancel", "style": "danger",
             "confirm": {"title": "确认", "text": "放弃此操作？"}},
        ],
        agent_id=aid,
    )
    assert card["schema"] == "2.0"
    elements = card["body"]["elements"]
    # First element: markdown body
    assert elements[0]["tag"] == "markdown"
    assert "Pick one" in elements[0]["content"]
    # Second element: column_set row holding the buttons
    assert elements[1]["tag"] == "column_set"
    buttons = _extract_buttons(card)
    assert len(buttons) == 2

    # Button protocol
    btn0_value = buttons[0]["behaviors"][0]["value"]
    assert btn0_value["v"] == 1
    assert btn0_value["kind"] == "card_action"
    assert btn0_value["agent_id"] == aid
    assert btn0_value["action_id"] == "go"
    assert btn0_value["label"] == "继续"
    assert btn0_value["context"] == {"k": "v"}
    assert buttons[0]["type"] == "primary"

    # Confirm dialog
    assert buttons[1]["confirm"]["title"]["content"] == "确认"
    assert buttons[1]["confirm"]["text"]["content"] == "放弃此操作？"
    assert buttons[1]["type"] == "danger"


def test_build_table_card_markdown_table():
    from app.services.feishu_service import build_table_card
    card = build_table_card(
        title="Plans",
        columns=["Plan", "Cost"],
        rows=[["A", "100"], ["B", "200"]],
    )
    md = card["body"]["elements"][0]["content"]
    assert "| Plan | Cost |" in md
    assert "| --- | --- |" in md
    assert "| A | 100 |" in md
    assert "| B | 200 |" in md


def test_build_table_card_handles_empty():
    from app.services.feishu_service import build_table_card
    card = build_table_card("T", [], [])
    assert "（空表）" in card["body"]["elements"][0]["content"]


def test_build_approval_card_decisions():
    from app.services.feishu_service import build_approval_card
    aid = "agent-uuid"
    card = build_approval_card(
        title="需审批",
        summary_text="请确认是否继续",
        approval_id="apv-42",
        agent_id=aid,
    )
    assert card["body"]["elements"][1]["tag"] == "column_set"
    buttons = _extract_buttons(card)
    assert len(buttons) == 2
    approve_v = buttons[0]["behaviors"][0]["value"]
    reject_v = buttons[1]["behaviors"][0]["value"]
    assert approve_v["kind"] == "approval"
    assert approve_v["agent_id"] == aid
    assert approve_v["action_id"] == "approve:apv-42"
    assert approve_v["context"]["decision"] == "approve"
    assert approve_v["context"]["approval_id"] == "apv-42"
    assert reject_v["context"]["decision"] == "reject"
    assert buttons[0]["type"] == "primary"
    assert buttons[1]["type"] == "danger"


# ─── send_card_with_fallback ───────────────────────────────────────


@pytest.mark.asyncio
async def test_send_card_with_fallback_happy_path(monkeypatch):
    from app.services.feishu_service import feishu_service
    calls = {}

    async def _fake_create_card_entity(self, app_id, app_secret, card_dict):
        calls["create"] = card_dict
        return "card_xyz"

    async def _fake_send_card_by_card_id(self, app_id, app_secret, receive_id, card_id, receive_id_type="open_id"):
        calls["send"] = (receive_id, card_id, receive_id_type)

    async def _fake_send_md(*a, **kw):
        calls["fallback"] = True
        return {"code": 0, "msg": "md"}

    monkeypatch.setattr(type(feishu_service), "create_card_entity", _fake_create_card_entity)
    monkeypatch.setattr(type(feishu_service), "send_card_by_card_id", _fake_send_card_by_card_id)
    monkeypatch.setattr(type(feishu_service), "send_markdown_message", _fake_send_md)

    result = await feishu_service.send_card_with_fallback(
        "aid", "asec", "ou_user", "open_id",
        {"schema": "2.0"}, "fallback text", stage="unit_test",
    )
    assert result == {"code": 0, "msg": "ok", "card_id": "card_xyz"}
    assert calls["create"] == {"schema": "2.0"}
    assert calls["send"] == ("ou_user", "card_xyz", "open_id")
    assert "fallback" not in calls


@pytest.mark.asyncio
async def test_send_card_with_fallback_falls_back_on_cardkit_failure(monkeypatch):
    from app.services.feishu_service import feishu_service
    calls = {}

    async def _fake_create_card_entity(self, *a, **kw):
        raise RuntimeError("CardKit boom")

    async def _fake_send_md(self, *, app_id, app_secret, receive_id, text, receive_id_type, stage):
        calls["fallback"] = {"to": receive_id, "text": text, "id_type": receive_id_type, "stage": stage}
        return {"code": 0, "msg": "ok-md"}

    monkeypatch.setattr(type(feishu_service), "create_card_entity", _fake_create_card_entity)
    monkeypatch.setattr(type(feishu_service), "send_markdown_message", _fake_send_md)

    result = await feishu_service.send_card_with_fallback(
        "aid", "asec", "ou_user", "open_id",
        {"schema": "2.0"}, "fallback text", stage="unit_test",
    )
    assert result == {"code": 0, "msg": "ok-md"}
    assert calls["fallback"]["to"] == "ou_user"
    assert calls["fallback"]["text"] == "fallback text"
    assert calls["fallback"]["stage"].endswith("_md_fallback")


# ─── Card action event handler (parse + synthesize) ───────────────


def test_parse_card_action_value_dict_and_string_and_garbage():
    from app.api.feishu import _parse_card_action_value
    payload = {"v": 1, "kind": "card_action", "label": "Go"}
    assert _parse_card_action_value(payload) == payload
    assert _parse_card_action_value(json.dumps(payload)) == payload
    assert _parse_card_action_value(None) == {}
    assert _parse_card_action_value("not-json") == {}
    assert _parse_card_action_value(["nope"]) == {}


def test_build_synthetic_card_user_text_card_action_with_context():
    from app.api.feishu import _build_synthetic_card_user_text
    text = _build_synthetic_card_user_text({
        "v": 1, "kind": "card_action", "label": "查看详情",
        "action_id": "view_42", "context": {"item_id": 42},
    })
    assert "[卡片操作] 查看详情" in text
    assert "上下文" in text and "item_id" in text
    assert "action_id=view_42" in text


def test_build_synthetic_card_user_text_approval_emits_decision():
    from app.api.feishu import _build_synthetic_card_user_text
    text = _build_synthetic_card_user_text({
        "v": 1, "kind": "approval", "label": "通过",
        "action_id": "approve:apv-1",
        "context": {"approval_id": "apv-1", "decision": "approve"},
    })
    assert "[卡片操作] 通过" in text
    assert "approval_id=apv-1" in text
    assert "decision=approve" in text


@pytest.mark.asyncio
async def test_handle_feishu_card_action_rejects_agent_id_mismatch():
    from app.api.feishu import _handle_feishu_card_action
    aid = uuid.uuid4()
    body = {
        "header": {"event_type": "card.action.trigger", "event_id": "ev1"},
        "event": {
            "operator": {"open_id": "ou_xxx"},
            "action": {"value": {
                "v": 1, "kind": "card_action", "agent_id": "DIFFERENT_AGENT",
                "label": "Go", "action_id": "go",
            }},
            "context": {},
        },
    }
    resp = _handle_feishu_card_action(aid, body, "ev1")
    assert resp["toast"]["type"] == "error"
    assert "操作来源" in resp["toast"]["content"]


@pytest.mark.asyncio
async def test_handle_feishu_card_action_rejects_unknown_kind():
    from app.api.feishu import _handle_feishu_card_action
    aid = uuid.uuid4()
    body = {
        "header": {"event_type": "card.action.trigger", "event_id": "ev1"},
        "event": {
            "operator": {"open_id": "ou_xxx"},
            "action": {"value": {"v": 1, "kind": "weird", "agent_id": str(aid), "label": "?"}},
        },
    }
    resp = _handle_feishu_card_action(aid, body, "ev1")
    assert resp["toast"]["type"] == "warning"


@pytest.mark.asyncio
async def test_handle_feishu_card_action_rejects_missing_v():
    from app.api.feishu import _handle_feishu_card_action
    aid = uuid.uuid4()
    body = {
        "header": {"event_type": "card.action.trigger", "event_id": "ev1"},
        "event": {
            "operator": {"open_id": "ou_xxx"},
            "action": {"value": {"kind": "card_action", "agent_id": str(aid), "label": "X"}},
        },
    }
    resp = _handle_feishu_card_action(aid, body, "ev1")
    assert resp["toast"]["type"] == "warning"


@pytest.mark.asyncio
async def test_handle_feishu_card_action_happy_path_schedules_synthetic(monkeypatch):
    """Valid click → returns success toast and schedules synthetic message processing."""
    from app.api import feishu as feishu_api

    aid = uuid.uuid4()
    captured = {}

    async def _fake_process(agent_id, body, db):
        captured["agent_id"] = agent_id
        captured["body"] = body

    class _FakeSession:
        async def __aenter__(self):
            return "fake_db"
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(feishu_api, "process_feishu_event", _fake_process)
    monkeypatch.setattr("app.database.async_session", lambda: _FakeSession())

    body = {
        "header": {"event_type": "card.action.trigger", "event_id": "ev_orig"},
        "event": {
            "operator": {"open_id": "ou_user", "user_id": "u_42"},
            "action": {"value": {
                "v": 1, "kind": "approval", "agent_id": str(aid),
                "label": "通过", "action_id": "approve:apv-1",
                "context": {"approval_id": "apv-1", "decision": "approve"},
            }},
            "context": {"open_chat_id": "oc_group_xxx", "open_message_id": "om_card"},
        },
    }
    resp = feishu_api._handle_feishu_card_action(aid, body, "ev_orig")
    assert resp["toast"]["type"] == "success"
    assert "通过" in resp["toast"]["content"]

    # Allow the scheduled task to run
    import asyncio
    for _ in range(5):
        if captured:
            break
        await asyncio.sleep(0.01)

    assert captured.get("agent_id") == aid
    inner = captured["body"]
    assert inner["header"]["event_type"] == "im.message.receive_v1"
    assert inner["header"]["event_id"].startswith("card_action_")
    msg = inner["event"]["message"]
    assert msg["chat_type"] == "group"      # because open_chat_id starts with oc_
    assert msg["chat_id"] == "oc_group_xxx"
    text = json.loads(msg["content"])["text"]
    assert "[卡片操作] 通过" in text
    assert "decision=approve" in text
    assert inner["event"]["sender"]["sender_id"]["open_id"] == "ou_user"


# NOTE: _resolve_feishu_receive lives in app.services.agent_tools, which has a
# pre-existing circular import with app.services.llm.caller (also affects
# test_a2a_msg_type.py when imported alongside other tests). The helper is
# exercised end-to-end via the manual card-send verification path; we skip a
# direct unit test here rather than wrestle with the import ordering.
