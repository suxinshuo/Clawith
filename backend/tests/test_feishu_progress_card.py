"""Feishu progress card rendering and Runtime event reduction tests."""

import json
from types import SimpleNamespace

import pytest

from app.services import feishu_progress_card as progress
from app.services.feishu_progress_card import (
    FeishuProgressState,
    build_progress_card,
    run_progress_card_updater,
)


def _event(event_type: str, payload: dict):
    return SimpleNamespace(event_type=event_type, payload=payload)


def _activity(activity_type: str, **payload):
    return _event("status_changed", {"activity_type": activity_type, **payload})


# ─── card rendering ─────────────────────────────────────────────────────


def test_progress_card_declares_update_multi_so_feishu_accepts_a_patch() -> None:
    """Feishu rejects a PATCH unless update_multi is set on old *and* new card."""
    card = build_progress_card(agent_name="Ada")

    assert card["config"]["update_multi"] is True
    assert card["schema"] == "2.0"


def test_progress_card_renders_the_answer_as_markdown() -> None:
    card = build_progress_card(agent_name="Ada", answer_text="# Title\n\n- a\n- b", done=True)

    elements = card["body"]["elements"]
    markdown = [element for element in elements if element["tag"] == "markdown"]
    assert any("# Title" in element["content"] for element in markdown)


def test_progress_card_shows_the_agent_name_in_the_header() -> None:
    card = build_progress_card(agent_name="Ada")

    assert card["header"]["title"]["content"] == "Ada"


def test_progress_card_keeps_only_the_most_recent_tool_lines() -> None:
    lines = [f"tool-{index}" for index in range(50)]
    card = build_progress_card(agent_name="Ada", tool_status_lines=lines)

    rendered = json.dumps(card, ensure_ascii=False)
    assert "tool-49" in rendered
    assert "tool-0\\n" not in rendered


def test_progress_card_truncates_an_oversized_answer() -> None:
    """A single Feishu card is capped at 30 KB; an agent reply can exceed it."""
    card = build_progress_card(agent_name="Ada", answer_text="x" * 60_000, done=True)

    payload = json.dumps(card, ensure_ascii=False)
    assert len(payload.encode("utf-8")) < 30 * 1024
    # json.dumps escapes the note's newlines, so match its visible text only.
    assert progress.TRUNCATION_NOTE.strip() in payload


def test_progress_card_truncation_keeps_multibyte_answers_under_the_cap() -> None:
    card = build_progress_card(agent_name="Ada", answer_text="中" * 30_000, done=True)

    payload = json.dumps(card, ensure_ascii=False)
    assert len(payload.encode("utf-8")) < 30 * 1024


def test_progress_card_falls_back_to_a_placeholder_when_empty() -> None:
    """Feishu rejects a card whose markdown element has empty content."""
    card = build_progress_card(agent_name="Ada")

    markdown = [
        element for element in card["body"]["elements"] if element["tag"] == "markdown"
    ]
    assert markdown
    assert all(element["content"].strip() for element in markdown)


# ─── Runtime event reduction ────────────────────────────────────────────


def test_state_records_a_running_tool() -> None:
    state = FeishuProgressState(agent_name="Ada")

    changed = state.apply(
        _activity("tool_call", name="read_file", call_id="c1", status="running")
    )

    assert changed is True
    assert any("read_file" in line for line in state.tool_status_lines)


def test_state_replaces_a_running_tool_with_its_completion() -> None:
    state = FeishuProgressState(agent_name="Ada")
    state.apply(_activity("tool_call", name="read_file", call_id="c1", status="running"))
    state.apply(_activity("tool_call", name="read_file", call_id="c1", status="done"))

    matching = [line for line in state.tool_status_lines if "read_file" in line]
    assert len(matching) == 1


def test_state_keeps_the_latest_assistant_progress() -> None:
    state = FeishuProgressState(agent_name="Ada")
    state.apply(_activity("assistant_progress", content="first round"))
    state.apply(_activity("assistant_progress", content="second round"))

    assert state.answer_text == "second round"


def test_state_marks_waiting_for_user_input() -> None:
    state = FeishuProgressState(agent_name="Ada")

    changed = state.apply(_event("waiting_started", {"waiting_type": "user"}))

    assert changed is True
    assert state.waiting_for_user is True


def test_state_ignores_events_that_do_not_change_the_card() -> None:
    state = FeishuProgressState(agent_name="Ada")

    assert state.apply(_event("evidence_added", {})) is False


def test_state_treats_terminal_events_as_done() -> None:
    state = FeishuProgressState(agent_name="Ada")

    state.apply(_event("run_completed", {}))

    assert state.done is True


# ─── updater loop ──────────────────────────────────────────────────────


class _Source:
    def __init__(self, events) -> None:
        self.events = events

    async def stream_run(self, _handle, *, after=None):
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_updater_patches_the_card_as_the_run_advances() -> None:
    patches: list[dict] = []

    async def patcher(card: dict) -> None:
        patches.append(card)

    await run_progress_card_updater(
        agent_name="Ada",
        handle=object(),
        event_source=_Source(
            [
                _activity("tool_call", name="read_file", call_id="c1", status="running"),
                _activity("assistant_progress", content="working on it"),
                _event("run_completed", {}),
            ]
        ),
        patcher=patcher,
        min_interval_seconds=0.0,
    )

    assert patches
    assert "read_file" in json.dumps(patches[0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_updater_throttles_patches_to_respect_the_feishu_rate_limit() -> None:
    """Feishu allows 5 PATCH/s on one message; bursts of events must coalesce."""
    patches: list[dict] = []
    now = {"value": 0.0}

    async def patcher(card: dict) -> None:
        patches.append(card)

    await run_progress_card_updater(
        agent_name="Ada",
        handle=object(),
        event_source=_Source(
            [_activity("assistant_progress", content=f"round {index}") for index in range(10)]
        ),
        patcher=patcher,
        min_interval_seconds=1.0,
        clock=lambda: now["value"],
    )

    # Every event lands in the same throttle window, so only the first patch and
    # the final flush reach Feishu.
    assert len(patches) <= 2
    assert "round 9" in json.dumps(patches[-1], ensure_ascii=False)


@pytest.mark.asyncio
async def test_updater_stops_patching_once_the_run_terminates() -> None:
    """The Runtime delivery renders the final card as the last word.

    If the updater kept patching after the terminal event it could overwrite
    the delivered answer with a stale in-progress render.
    """
    patches: list[dict] = []

    async def patcher(card: dict) -> None:
        patches.append(card)

    await run_progress_card_updater(
        agent_name="Ada",
        handle=object(),
        event_source=_Source(
            [
                _activity("assistant_progress", content="mid"),
                _event("run_completed", {}),
                _activity("assistant_progress", content="must not be rendered"),
            ]
        ),
        patcher=patcher,
        min_interval_seconds=0.0,
    )

    rendered = json.dumps(patches, ensure_ascii=False)
    assert "mid" in rendered
    assert "must not be rendered" not in rendered


@pytest.mark.asyncio
async def test_updater_hands_over_when_the_run_waits_for_the_user() -> None:
    """The Runtime stages a `waiting` delivery too, which renders the question."""
    patches: list[dict] = []

    async def patcher(card: dict) -> None:
        patches.append(card)

    await run_progress_card_updater(
        agent_name="Ada",
        handle=object(),
        event_source=_Source(
            [
                _activity("assistant_progress", content="before the question"),
                _event("waiting_started", {"waiting_type": "user"}),
                _activity("assistant_progress", content="must not be rendered"),
            ]
        ),
        patcher=patcher,
        min_interval_seconds=0.0,
    )

    rendered = json.dumps(patches, ensure_ascii=False)
    assert "before the question" in rendered
    assert "must not be rendered" not in rendered


@pytest.mark.asyncio
async def test_updater_never_raises_when_feishu_rejects_a_patch() -> None:
    """A cosmetic progress patch must never take down the run or the reply."""
    attempts: list[int] = []

    async def patcher(_card: dict) -> None:
        attempts.append(1)
        raise RuntimeError("Feishu rate limited the card")

    await run_progress_card_updater(
        agent_name="Ada",
        handle=object(),
        event_source=_Source(
            [
                _activity("assistant_progress", content="one"),
                _event("run_completed", {}),
            ]
        ),
        patcher=patcher,
        min_interval_seconds=0.0,
    )

    assert attempts


@pytest.mark.asyncio
async def test_updater_never_raises_when_the_event_stream_fails() -> None:
    class _FailingSource:
        async def stream_run(self, _handle, *, after=None):
            raise RuntimeError("run disappeared")
            yield  # pragma: no cover

    await run_progress_card_updater(
        agent_name="Ada",
        handle=object(),
        event_source=_FailingSource(),
        patcher=None,
        min_interval_seconds=0.0,
    )
