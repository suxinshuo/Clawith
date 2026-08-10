"""Tests for task 8.2: the token budget enforcement mode product entry point.

Covers the two new dedicated endpoints (`GET`/`PUT /enterprise/token-budget-enforcement`)
and the new guard added to the generic `PUT /system-settings/{key}` endpoint for the
`token_budget_enforcement_mode` key.

These tests monkeypatch `system_setting_dao.get_value`/`set_value` with a small
in-memory store rather than a real database connection. Because `enterprise.py` and
`budget.py` both import the *same* `system_setting_dao` singleton instance, patching
its methods here also affects what `budget.current_enforcement_state()` observes —
which is exactly what lets the "same-process immediate effect" tests below exercise
the real cache-invalidation behaviour instead of a mocked one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import enterprise
from app.services.token_accounting import budget
from app.services.token_accounting.budget import (
    MODE_ENFORCE,
    MODE_WARN_ONLY,
    SETTING_ENFORCEMENT_MODE,
    reset_enforcement_mode_cache,
)


@pytest.fixture(autouse=True)
def _reset_enforcement_mode_cache_between_tests():
    """Prevent the 30s in-process cache from leaking state across test cases."""
    reset_enforcement_mode_cache()
    yield
    reset_enforcement_mode_cache()


class _FakeSettingStore:
    """Minimal stand-in for `system_setting_dao` backed by a plain dict.

    Only implements the two methods these endpoints/`budget.py` actually call:
    `get_value` (read) and `set_value` (the task 8.1 upsert).
    """

    def __init__(self, initial: dict | None = None) -> None:
        self._rows: dict[str, dict] = dict(initial or {})

    async def get_value(self, key: str, default=None):
        return self._rows.get(key, default if default is not None else {})

    async def set_value(self, key: str, value: dict):
        self._rows[key] = value
        return SimpleNamespace(key=key, value=value)


def _patch_store(monkeypatch, store: "_FakeSettingStore") -> None:
    """Patch both call sites that read `token_budget_enforcement_mode`.

    `enterprise.py` calls `system_setting_dao.set_value`/`get_value` directly
    for the write path and the `set_by`/raw-value lookups; `budget.py`'s
    `current_enforcement_state()` (invoked by both endpoints to build the
    response, and by the cache-invalidation test to prove same-process
    effect) reads through its own module-level reference to the same
    singleton. Both must be patched or `current_enforcement_state()` will hit
    a real (unavailable in this test environment) database connection and
    fail-open to `warn_only`, masking the behaviour under test.
    """
    monkeypatch.setattr(enterprise, "system_setting_dao", store)
    monkeypatch.setattr(budget, "system_setting_dao", store)


def _org_admin() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email="org-admin@example.com", role="org_admin", tenant_id=uuid.uuid4())


def _platform_admin() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email="platform-admin@example.com", role="platform_admin", tenant_id=None)


# ---------------------------------------------------------------------------
# GET /enterprise/token-budget-enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_endpoint_is_readable_by_org_admin_and_has_the_expected_shape(monkeypatch) -> None:
    """org_admin can read the mode (self-diagnosis: why is my agent being blocked)."""
    store = _FakeSettingStore({SETTING_ENFORCEMENT_MODE: {"mode": "enforce", "set_by": "migration"}})
    _patch_store(monkeypatch, store)

    result = await enterprise.get_token_budget_enforcement(current_user=_org_admin())

    assert result == {
        "configured_mode": MODE_ENFORCE,
        "effective_mode": MODE_ENFORCE,
        "grace_until": None,
        "grace_active": False,
        "set_by": "migration",
        "propagation_seconds": 30,
    }


@pytest.mark.asyncio
async def test_get_endpoint_reports_grace_active_when_grace_until_is_in_the_future(monkeypatch) -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    store = _FakeSettingStore(
        {SETTING_ENFORCEMENT_MODE: {"mode": "enforce", "grace_until": future, "set_by": "admin@example.com"}}
    )
    _patch_store(monkeypatch, store)

    result = await enterprise.get_token_budget_enforcement(current_user=_org_admin())

    assert result["configured_mode"] == MODE_ENFORCE
    assert result["effective_mode"] == MODE_WARN_ONLY, "grace active -> effective_mode is forced to warn_only"
    assert result["grace_active"] is True
    assert result["grace_until"] == datetime.fromisoformat(future).isoformat()
    assert result["set_by"] == "admin@example.com"


@pytest.mark.asyncio
async def test_get_endpoint_defaults_set_by_to_none_when_absent(monkeypatch) -> None:
    store = _FakeSettingStore({SETTING_ENFORCEMENT_MODE: {"mode": "warn_only"}})
    _patch_store(monkeypatch, store)

    result = await enterprise.get_token_budget_enforcement(current_user=_org_admin())

    assert result["set_by"] is None


# ---------------------------------------------------------------------------
# PUT /enterprise/token-budget-enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_endpoint_rejects_org_admin(monkeypatch) -> None:
    """A tenant's org_admin must not be able to flip this platform-wide switch."""
    store = _FakeSettingStore({SETTING_ENFORCEMENT_MODE: {"mode": "warn_only"}})
    _patch_store(monkeypatch, store)

    with pytest.raises(HTTPException) as exc_info:
        await enterprise.update_token_budget_enforcement(
            enterprise.TokenBudgetEnforcementUpdate(mode="enforce"),
            current_user=_org_admin(),
        )

    assert exc_info.value.status_code == 403
    # The store must be untouched -- the guard must fire before any write.
    assert store._rows[SETTING_ENFORCEMENT_MODE] == {"mode": "warn_only"}


@pytest.mark.asyncio
async def test_put_endpoint_platform_admin_succeeds_and_next_read_in_process_sees_the_new_value(
    monkeypatch,
) -> None:
    """Platform admin can flip the mode, and the very next judgement in this
    process must observe the new value immediately (cache invalidated), not
    wait out the 30s TTL.
    """
    store = _FakeSettingStore({SETTING_ENFORCEMENT_MODE: {"mode": "warn_only"}})
    _patch_store(monkeypatch, store)

    # Prime the cache with the old value, mirroring a live process that has
    # already judged at least one request before the admin changes the mode.
    assert await budget.current_enforcement_mode() == MODE_WARN_ONLY

    result = await enterprise.update_token_budget_enforcement(
        enterprise.TokenBudgetEnforcementUpdate(mode="enforce"),
        current_user=_platform_admin(),
    )

    assert result["configured_mode"] == MODE_ENFORCE
    assert result["effective_mode"] == MODE_ENFORCE
    assert store._rows[SETTING_ENFORCEMENT_MODE]["mode"] == "enforce"
    assert store._rows[SETTING_ENFORCEMENT_MODE]["set_by"] == "platform-admin@example.com"
    # This is the assertion that actually proves the cache was invalidated: if
    # it weren't, this call would still return the stale warn_only value that
    # was cached above, for up to _MODE_TTL_SECONDS.
    assert await budget.current_enforcement_mode() == MODE_ENFORCE


@pytest.mark.asyncio
async def test_put_endpoint_bare_mode_change_preserves_existing_grace_until(monkeypatch) -> None:
    """Neither `clear_grace` nor a new `grace_until` supplied -> an in-progress
    grace window must survive a bare mode change untouched.
    """
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    store = _FakeSettingStore(
        {SETTING_ENFORCEMENT_MODE: {"mode": "enforce", "grace_until": future, "set_by": "migration"}}
    )
    _patch_store(monkeypatch, store)

    result = await enterprise.update_token_budget_enforcement(
        enterprise.TokenBudgetEnforcementUpdate(mode="enforce"),
        current_user=_platform_admin(),
    )

    assert result["grace_until"] == datetime.fromisoformat(future).isoformat()
    assert result["grace_active"] is True
    assert store._rows[SETTING_ENFORCEMENT_MODE]["grace_until"] == future


@pytest.mark.asyncio
async def test_put_endpoint_explicit_grace_until_overrides_the_stored_one(monkeypatch) -> None:
    old_future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    new_future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    store = _FakeSettingStore({SETTING_ENFORCEMENT_MODE: {"mode": "enforce", "grace_until": old_future}})
    _patch_store(monkeypatch, store)

    result = await enterprise.update_token_budget_enforcement(
        enterprise.TokenBudgetEnforcementUpdate(mode="enforce", grace_until=new_future),
        current_user=_platform_admin(),
    )

    assert result["grace_until"] == datetime.fromisoformat(new_future).isoformat()
    assert store._rows[SETTING_ENFORCEMENT_MODE]["grace_until"] == new_future


@pytest.mark.asyncio
async def test_put_endpoint_clear_grace_true_immediately_deactivates_grace(monkeypatch) -> None:
    """`clear_grace: true` must win outright, even if `grace_until` was also supplied."""
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    store = _FakeSettingStore(
        {SETTING_ENFORCEMENT_MODE: {"mode": "enforce", "grace_until": future, "set_by": "migration"}}
    )
    _patch_store(monkeypatch, store)

    result = await enterprise.update_token_budget_enforcement(
        enterprise.TokenBudgetEnforcementUpdate(mode="enforce", clear_grace=True, grace_until=future),
        current_user=_platform_admin(),
    )

    assert result["grace_until"] is None
    assert result["grace_active"] is False
    assert result["effective_mode"] == MODE_ENFORCE
    assert "grace_until" not in store._rows[SETTING_ENFORCEMENT_MODE]


@pytest.mark.asyncio
async def test_put_endpoint_rejects_an_unknown_mode() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        enterprise.TokenBudgetEnforcementUpdate(mode="disabled")


# ---------------------------------------------------------------------------
# PUT /system-settings/{key} guard for token_budget_enforcement_mode
# (3.9 Preservation: other keys' behaviour must stay untouched)
# ---------------------------------------------------------------------------


class _SettingRow:
    def __init__(self, key: str, value: dict) -> None:
        self.key = key
        self.value = value
        self.updated_at = None


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RecordingDB:
    def __init__(self, existing: _SettingRow | None = None) -> None:
        self._existing = existing
        self.added: list[_SettingRow] = []
        self.committed = False
        self.refreshed = False

    async def execute(self, _statement):
        return _Result(self._existing)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj) -> None:
        self.refreshed = True


@pytest.mark.asyncio
async def test_generic_endpoint_rejects_org_admin_for_the_enforcement_mode_key() -> None:
    db = _RecordingDB(existing=_SettingRow(SETTING_ENFORCEMENT_MODE, {"mode": "warn_only"}))

    with pytest.raises(HTTPException) as exc_info:
        await enterprise.update_system_setting(
            SETTING_ENFORCEMENT_MODE,
            enterprise.SettingUpdate(value={"mode": "enforce"}),
            current_user=_org_admin(),
            db=db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 403
    assert "token-budget-enforcement" in exc_info.value.detail
    assert db.committed is False


@pytest.mark.asyncio
async def test_generic_endpoint_still_allows_platform_admin_for_the_enforcement_mode_key() -> None:
    """The new guard raises the bar to platform_admin; it must not break the
    generic endpoint outright for that key when the caller *is* one.
    """
    existing = _SettingRow(SETTING_ENFORCEMENT_MODE, {"mode": "warn_only"})
    db = _RecordingDB(existing=existing)

    result = await enterprise.update_system_setting(
        SETTING_ENFORCEMENT_MODE,
        enterprise.SettingUpdate(value={"mode": "enforce"}),
        current_user=_platform_admin(),
        db=db,  # type: ignore[arg-type]
    )

    assert result["value"] == {"mode": "enforce"}
    assert db.committed is True


@pytest.mark.asyncio
async def test_generic_endpoint_is_unaffected_for_other_keys(monkeypatch) -> None:
    """3.9 Preservation: the new guard must not affect any other key's
    existing permission/behaviour -- org_admin can still PATCH arbitrary
    settings via the generic endpoint.
    """
    existing = _SettingRow("invitation_code_enabled", {"enabled": False})
    db = _RecordingDB(existing=existing)

    result = await enterprise.update_system_setting(
        "invitation_code_enabled",
        enterprise.SettingUpdate(value={"enabled": True}),
        current_user=_org_admin(),
        db=db,  # type: ignore[arg-type]
    )

    assert result["value"] == {"enabled": True}
    assert db.committed is True


@pytest.mark.asyncio
async def test_generic_endpoint_platform_only_guard_for_platform_key_is_unchanged() -> None:
    """3.9 Preservation: the pre-existing `key == "platform"` guard is untouched."""
    db = _RecordingDB(existing=_SettingRow("platform", {}))

    with pytest.raises(HTTPException) as exc_info:
        await enterprise.update_system_setting(
            "platform",
            enterprise.SettingUpdate(value={"public_base_url": "https://example.com"}),
            current_user=_org_admin(),
            db=db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 403
    assert db.committed is False
