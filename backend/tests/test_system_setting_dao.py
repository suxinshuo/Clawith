"""Unit tests for SystemSettingDAO.set_value (task 8.1).

These tests use a session double (RecordingSession/SessionFactory), mirroring
the style already used in tests/test_base_dao.py. No real database connection
is required; the real upsert conflict behaviour is re-verified against a
live database in task 11.5.
"""

from types import SimpleNamespace

import pytest

from app.dao.system_setting_dao import SystemSettingDAO
from app.models.system_settings import SystemSetting


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """Minimal async-session double that records add()/flush()/execute() calls."""

    def __init__(self, existing_setting: SystemSetting | None = None):
        self._existing_setting = existing_setting
        self.added: list[object] = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _Result(self._existing_setting)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class SessionFactory:
    """Matches the `async_session()` factory shape expected by BaseDAO.session()."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_set_value_creates_a_new_row_when_key_is_absent(monkeypatch):
    """key doesn't exist yet -> a new SystemSetting row is created with the given value."""
    session = RecordingSession(existing_setting=None)
    monkeypatch.setattr("app.dao.base.async_session", SessionFactory(session))

    dao = SystemSettingDAO()
    result = await dao.set_value("some_new_key", {"mode": "enforce"})

    # A brand new SystemSetting must have been added to the session (not an update
    # of an existing row), and it must carry the requested key/value.
    assert len(session.added) == 1
    created = session.added[0]
    assert isinstance(created, SystemSetting)
    assert created.key == "some_new_key"
    assert created.value == {"mode": "enforce"}
    assert result is created
    assert session.flushed is True
    assert session.committed is True


@pytest.mark.asyncio
async def test_set_value_updates_the_existing_row_instead_of_creating_a_second_one(monkeypatch):
    """key already exists -> its value field is updated, no second row is created."""
    existing = SimpleNamespace(key="token_budget_enforcement_mode", value={"mode": "warn_only"})
    session = RecordingSession(existing_setting=existing)
    monkeypatch.setattr("app.dao.base.async_session", SessionFactory(session))

    dao = SystemSettingDAO()
    result = await dao.set_value("token_budget_enforcement_mode", {"mode": "enforce"})

    # No new row should have been added via db.add() -- the existing object was
    # mutated in place.
    assert session.added == []
    assert existing.value == {"mode": "enforce"}
    assert result is existing
    assert session.flushed is True
    assert session.committed is True
