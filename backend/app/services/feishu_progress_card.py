"""Feishu progress card — one interactive card patched as a Runtime run advances.

Upstream's Runtime never streams tokens: the finest progress it durably records
is one `assistant_progress` / `tool_call` activity per model round in
`agent_run_events`, and the user-facing answer only exists once the model calls
`finish(content=...)`. This module renders those per-round events into a single
`interactive` card that is PATCHed in place, so a Feishu user sees the agent
working instead of staring at silence until the one-shot reply lands.

Ownership is split to avoid two writers racing on one card:

* while the run is in flight, `run_progress_card_updater` owns the card;
* the moment the run reaches a terminal event the updater stops for good and
  the Runtime channel delivery renders the final card as the last word.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from loguru import logger

# Feishu allows 5 PATCH/s on a single message. 1 s matches the cadence the fork
# proved in production before the v1.11.2 merge and leaves ample margin.
PROGRESS_PATCH_INTERVAL_SECONDS = 1.0

# Feishu caps one card at 30 KB. Budget the two variable-length regions well
# under that so JSON escaping cannot push the envelope over the limit.
ANSWER_BYTE_BUDGET = 16 * 1024
TOOL_STATUS_BYTE_BUDGET = 2 * 1024

TRUNCATION_NOTE = "\n\n…（内容过长，已截断）"

# Keep the card readable on a phone: only the tail of a long tool run matters.
TOOL_STATUS_KEEP_LINES = 20

_WORKING_PLACEHOLDER = "⏳ 正在处理…"
_WAITING_PLACEHOLDER = "⏳ 等待你的回复…"
_TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed", "run_cancelled"})


def _truncate_to_bytes(text: str, budget: int) -> str:
    """Trim `text` so its UTF-8 form fits `budget`, noting that it was cut."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    keep = budget - len(TRUNCATION_NOTE.encode("utf-8"))
    if keep <= 0:
        return TRUNCATION_NOTE
    return encoded[:keep].decode("utf-8", errors="ignore") + TRUNCATION_NOTE


def build_progress_card(
    *,
    agent_name: str,
    tool_status_lines: list[str] | None = None,
    answer_text: str = "",
    waiting_for_user: bool = False,
    done: bool = False,
) -> dict:
    """Render one state of the progress card as Feishu card JSON (schema 2.0).

    `update_multi` must be declared here: Feishu rejects a PATCH unless both the
    old and the new card carry it.
    """
    elements: list[dict] = []

    if tool_status_lines:
        recent = tool_status_lines[-TOOL_STATUS_KEEP_LINES:]
        elements.append(
            {
                "tag": "markdown",
                "content": _truncate_to_bytes("\n".join(recent), TOOL_STATUS_BYTE_BUDGET),
                "text_size": "notation",
            }
        )
        elements.append({"tag": "hr"})

    body = answer_text.strip()
    if body:
        body = _truncate_to_bytes(body, ANSWER_BYTE_BUDGET)
        if not done:
            body = f"{body}\n\n{_WORKING_PLACEHOLDER}"
    elif waiting_for_user:
        body = _WAITING_PLACEHOLDER
    else:
        # Feishu rejects a markdown element with empty content.
        body = _WORKING_PLACEHOLDER

    elements.append({"tag": "markdown", "content": body, "text_align": "left"})

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "streaming_mode": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": (agent_name or "AI")[:100]},
        },
        "body": {"elements": elements},
    }


class FeishuProgressState:
    """Fold Runtime events into the state one Feishu card should display."""

    def __init__(self, *, agent_name: str) -> None:
        self.agent_name = agent_name
        self.answer_text = ""
        self.waiting_for_user = False
        self.done = False
        self._tool_lines: dict[str, str] = {}
        self._tool_order: list[str] = []

    @property
    def tool_status_lines(self) -> list[str]:
        return [self._tool_lines[call_id] for call_id in self._tool_order]

    @property
    def handover(self) -> bool:
        """True once a channel delivery is due to render this card itself.

        The Runtime stages a delivery for both `waiting` and `terminal`
        boundaries, so the updater must stop at either one rather than race the
        delivered text with a stale in-progress render.
        """
        return self.done or self.waiting_for_user

    def card(self) -> dict:
        return build_progress_card(
            agent_name=self.agent_name,
            tool_status_lines=self.tool_status_lines,
            answer_text=self.answer_text,
            waiting_for_user=self.waiting_for_user,
            done=self.done,
        )

    def apply(self, event) -> bool:
        """Fold one event in. Returns True when the visible card changed."""
        event_type = getattr(event, "event_type", "") or ""
        payload = getattr(event, "payload", None) or {}

        if event_type in _TERMINAL_EVENT_TYPES:
            self.done = True
            return True

        if event_type == "waiting_started":
            if payload.get("waiting_type") == "user" and not self.waiting_for_user:
                self.waiting_for_user = True
                return True
            return False

        if event_type == "resumed":
            if self.waiting_for_user:
                self.waiting_for_user = False
                return True
            return False

        if event_type != "status_changed":
            return False

        activity_type = payload.get("activity_type")
        if activity_type == "assistant_progress":
            return self._apply_answer(payload.get("content"))
        if activity_type == "tool_call":
            return self._apply_tool_call(payload)
        return False

    def _apply_answer(self, content: object) -> bool:
        if not isinstance(content, str) or not content.strip():
            return False
        if content == self.answer_text:
            return False
        self.answer_text = content
        return True

    def _apply_tool_call(self, payload: dict) -> bool:
        status = payload.get("status")
        if status not in {"running", "done"}:
            return False
        name = payload.get("name")
        call_id = payload.get("call_id")
        if not isinstance(name, str) or not isinstance(call_id, str) or not call_id:
            return False

        line = f"⚙️ {name} 执行中…" if status == "running" else f"✅ {name}"
        if self._tool_lines.get(call_id) == line:
            return False
        # Key by call_id so a completion replaces its own "running" line rather
        # than stacking a second entry for the same tool call.
        if call_id not in self._tool_lines:
            self._tool_order.append(call_id)
        self._tool_lines[call_id] = line
        return True


async def run_progress_card_updater(
    *,
    agent_name: str,
    handle,
    event_source,
    patcher: Callable[[dict], Awaitable[None]] | None,
    min_interval_seconds: float = PROGRESS_PATCH_INTERVAL_SECONDS,
    after=None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Patch one Feishu card while a Runtime run advances.

    Never raises: a progress card is cosmetic and must not disturb the run or
    the reply. Returns as soon as the run reaches a delivery boundary, leaving
    that render to the Runtime channel delivery.
    """
    state = FeishuProgressState(agent_name=agent_name)
    now = clock or time.monotonic
    last_patch_at: float | None = None
    pending = False

    async def flush() -> None:
        nonlocal last_patch_at, pending
        pending = False
        last_patch_at = now()
        if patcher is None:
            return
        try:
            await patcher(state.card())
        except Exception as e:
            logger.warning(f"[Feishu Progress] Card patch failed: {e}")

    try:
        async for event in event_source.stream_run(handle, after=after):
            if not state.apply(event):
                continue
            if state.handover:
                # Hand the card over to the delivery; patching now would race it.
                return
            if last_patch_at is None or now() - last_patch_at >= min_interval_seconds:
                await flush()
            else:
                pending = True
    except Exception as e:
        logger.warning(f"[Feishu Progress] Event stream ended early: {e}")

    if pending:
        await flush()
