"""Tests for the get_activity_log tool: arg normalization, query construction,
formatting, and execute_tool wiring.

NOTE: imports from app.services.agent_tools are deferred into test functions to
avoid the pre-existing circular import between agent_tools and llm.caller at
collection time. Importing app.services.activity_logger at module top-level is
safe (it only depends on app.database and app.models).
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.activity_logger import _normalize_activity_query


def test_normalize_defaults():
    out = _normalize_activity_query({})
    assert out == {"limit": 30, "hours": None, "action_types": None, "keyword": None}


def test_normalize_limit_clamped_high():
    assert _normalize_activity_query({"limit": 9999})["limit"] == 100


def test_normalize_limit_clamped_low_and_garbage():
    assert _normalize_activity_query({"limit": 0})["limit"] == 1
    assert _normalize_activity_query({"limit": -5})["limit"] == 1
    assert _normalize_activity_query({"limit": "abc"})["limit"] == 30  # falls back to default


def test_normalize_hours():
    assert _normalize_activity_query({"hours": 24})["hours"] == 24
    assert _normalize_activity_query({"hours": 0})["hours"] is None      # 0 = no window
    assert _normalize_activity_query({"hours": "bad"})["hours"] is None


def test_normalize_action_types_filters_invalid():
    out = _normalize_activity_query({"action_types": ["tool_call", "nonsense", "chat_reply"]})
    assert out["action_types"] == ["tool_call", "chat_reply"]
    # all-invalid collapses to None (no filter rather than match-nothing surprise)
    assert _normalize_activity_query({"action_types": ["nope"]})["action_types"] is None
    # a bare string is accepted and wrapped
    assert _normalize_activity_query({"action_types": "tool_call"})["action_types"] == ["tool_call"]


def test_normalize_keyword():
    assert _normalize_activity_query({"keyword": "  hello  "})["keyword"] == "hello"
    assert _normalize_activity_query({"keyword": "   "})["keyword"] is None
    assert _normalize_activity_query({"keyword": 123})["keyword"] is None
