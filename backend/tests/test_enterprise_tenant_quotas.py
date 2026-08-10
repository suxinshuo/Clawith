"""Tests for task 8.4: read/write API for the tenant-level token budget columns.

Covers `TenantQuotaUpdate`'s three new fields (`max_tokens_per_day`,
`default_agent_max_tokens_per_day`, `default_agent_max_tokens_per_month`) and the
three-state PATCH semantics `update_tenant_quotas` must implement for them:

  1. key absent from the request body -> leave the column unchanged
  2. key present with an explicit `null` -> write NULL (explicit "unlimited")
  3. key present with a positive integer -> write that value as the new cap

This is deliberately different from the nine pre-existing quota fields, which
treat "key absent" and "key present with null" the same way (both are
`is None` and both leave the column untouched). See the Preservation test
below, which asserts that existing-field behaviour is unaffected by the new
`model_fields_set` check added for the three new fields.

Test style follows `test_enterprise_token_budget_enforcement.py`: the FastAPI
route functions are called directly (bypassing dependency injection), with a
minimal in-memory stand-in for the AsyncSession.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api import enterprise


def _tenant(**overrides) -> SimpleNamespace:
    """A `Tenant`-shaped stand-in with sane defaults for every field the two
    handlers under test read or write.
    """
    defaults = dict(
        id=uuid.uuid4(),
        default_message_limit=50,
        default_message_period="permanent",
        default_max_agents=2,
        default_agent_ttl_hours=0,
        default_max_llm_calls_per_day=1000,
        min_heartbeat_interval_minutes=240,
        default_max_triggers=20,
        min_poll_interval_floor=5,
        max_webhook_rate_ceiling=5,
        max_tokens_per_day=None,
        default_agent_max_tokens_per_day=None,
        default_agent_max_tokens_per_month=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    """Minimal `AsyncSession` stand-in: a single tenant row, `execute` always
    returns it, `commit` is a no-op recorded for assertions.
    """

    def __init__(self, tenant) -> None:
        self._tenant = tenant
        self.committed = False

    async def execute(self, _statement):
        return _Result(self._tenant)

    async def commit(self) -> None:
        self.committed = True


def _admin(tenant_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Three-state semantics: key absent / null / positive integer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_absent_leaves_the_column_unchanged() -> None:
    """Not including `max_tokens_per_day` in the request body at all must not
    touch the existing value, even though `TenantQuotaUpdate`'s default for
    that field is also `None` -- the distinguishing signal is
    `model_fields_set`, not the field's runtime value.
    """
    tenant = _tenant(max_tokens_per_day=100_000)
    db = _DB(tenant)

    data = enterprise.TenantQuotaUpdate(default_message_limit=75)  # unrelated field only
    assert "max_tokens_per_day" not in data.model_fields_set

    await enterprise.update_tenant_quotas(data, current_user=_admin(tenant.id), db=db)  # type: ignore[arg-type]

    assert tenant.max_tokens_per_day == 100_000, "key absent must not clear an existing cap"
    assert db.committed is True


@pytest.mark.asyncio
async def test_explicit_null_clears_an_existing_cap_to_unlimited() -> None:
    """Explicitly passing `max_tokens_per_day=None` must write NULL even
    though the tenant currently has a concrete numeric cap -- this is the
    case the nine pre-existing fields cannot express (their `is not None`
    check would treat this identically to "key absent").
    """
    tenant = _tenant(max_tokens_per_day=100_000)
    db = _DB(tenant)

    data = enterprise.TenantQuotaUpdate(max_tokens_per_day=None)
    assert "max_tokens_per_day" in data.model_fields_set

    await enterprise.update_tenant_quotas(data, current_user=_admin(tenant.id), db=db)  # type: ignore[arg-type]

    assert tenant.max_tokens_per_day is None


@pytest.mark.asyncio
async def test_positive_integer_sets_a_new_cap() -> None:
    tenant = _tenant(max_tokens_per_day=None)
    db = _DB(tenant)

    data = enterprise.TenantQuotaUpdate(max_tokens_per_day=250_000)

    await enterprise.update_tenant_quotas(data, current_user=_admin(tenant.id), db=db)  # type: ignore[arg-type]

    assert tenant.max_tokens_per_day == 250_000


@pytest.mark.asyncio
async def test_default_agent_max_tokens_per_day_three_state_semantics() -> None:
    """Same three-state semantics apply to the second new column."""
    tenant = _tenant(default_agent_max_tokens_per_day=50_000)

    # 1. key absent -> unchanged
    db = _DB(tenant)
    await enterprise.update_tenant_quotas(
        enterprise.TenantQuotaUpdate(default_message_limit=10),
        current_user=_admin(tenant.id),
        db=db,  # type: ignore[arg-type]
    )
    assert tenant.default_agent_max_tokens_per_day == 50_000

    # 2. explicit null -> cleared to unlimited
    db = _DB(tenant)
    await enterprise.update_tenant_quotas(
        enterprise.TenantQuotaUpdate(default_agent_max_tokens_per_day=None),
        current_user=_admin(tenant.id),
        db=db,  # type: ignore[arg-type]
    )
    assert tenant.default_agent_max_tokens_per_day is None

    # 3. positive integer -> new cap
    db = _DB(tenant)
    await enterprise.update_tenant_quotas(
        enterprise.TenantQuotaUpdate(default_agent_max_tokens_per_day=30_000),
        current_user=_admin(tenant.id),
        db=db,  # type: ignore[arg-type]
    )
    assert tenant.default_agent_max_tokens_per_day == 30_000


@pytest.mark.asyncio
async def test_default_agent_max_tokens_per_month_three_state_semantics() -> None:
    """Same three-state semantics apply to the third new column."""
    tenant = _tenant(default_agent_max_tokens_per_month=1_000_000)

    # 1. key absent -> unchanged
    db = _DB(tenant)
    await enterprise.update_tenant_quotas(
        enterprise.TenantQuotaUpdate(default_message_limit=10),
        current_user=_admin(tenant.id),
        db=db,  # type: ignore[arg-type]
    )
    assert tenant.default_agent_max_tokens_per_month == 1_000_000

    # 2. explicit null -> cleared to unlimited
    db = _DB(tenant)
    await enterprise.update_tenant_quotas(
        enterprise.TenantQuotaUpdate(default_agent_max_tokens_per_month=None),
        current_user=_admin(tenant.id),
        db=db,  # type: ignore[arg-type]
    )
    assert tenant.default_agent_max_tokens_per_month is None

    # 3. positive integer -> new cap
    db = _DB(tenant)
    await enterprise.update_tenant_quotas(
        enterprise.TenantQuotaUpdate(default_agent_max_tokens_per_month=500_000),
        current_user=_admin(tenant.id),
        db=db,  # type: ignore[arg-type]
    )
    assert tenant.default_agent_max_tokens_per_month == 500_000


# ---------------------------------------------------------------------------
# Preservation: existing quota fields' PATCH semantics must be unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_field_patch_semantics_and_new_columns_are_unaffected() -> None:
    """A request that only supplies an existing field (`default_message_limit`)
    must still write it via the old `is not None` check, and must leave all
    three new columns untouched -- "not present in the request body" produces
    the same observable outcome (no change) for both old- and new-style
    fields, even though the underlying check differs (`is not None` vs.
    `model_fields_set`).
    """
    tenant = _tenant(
        default_message_limit=50,
        max_tokens_per_day=100_000,
        default_agent_max_tokens_per_day=50_000,
        default_agent_max_tokens_per_month=1_000_000,
    )
    db = _DB(tenant)

    data = enterprise.TenantQuotaUpdate(default_message_limit=999)
    assert "max_tokens_per_day" not in data.model_fields_set
    assert "default_agent_max_tokens_per_day" not in data.model_fields_set
    assert "default_agent_max_tokens_per_month" not in data.model_fields_set

    result = await enterprise.update_tenant_quotas(data, current_user=_admin(tenant.id), db=db)  # type: ignore[arg-type]

    assert tenant.default_message_limit == 999
    assert tenant.max_tokens_per_day == 100_000
    assert tenant.default_agent_max_tokens_per_day == 50_000
    assert tenant.default_agent_max_tokens_per_month == 1_000_000
    assert result == {"message": "Tenant quotas updated", "heartbeat_agents_adjusted": 0}


# ---------------------------------------------------------------------------
# GET /tenant-quotas returns the three new columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_quotas_includes_the_three_new_columns() -> None:
    tenant = _tenant(
        max_tokens_per_day=200_000,
        default_agent_max_tokens_per_day=20_000,
        default_agent_max_tokens_per_month=400_000,
    )
    db = _DB(tenant)
    current_user = SimpleNamespace(tenant_id=tenant.id)

    result = await enterprise.get_tenant_quotas(current_user=current_user, db=db)  # type: ignore[arg-type]

    assert result["max_tokens_per_day"] == 200_000
    assert result["default_agent_max_tokens_per_day"] == 20_000
    assert result["default_agent_max_tokens_per_month"] == 400_000
    # Existing keys must still be present (response shape preserved).
    assert result["default_message_limit"] == tenant.default_message_limit
    assert result["max_webhook_rate_ceiling"] == tenant.max_webhook_rate_ceiling


@pytest.mark.asyncio
async def test_get_tenant_quotas_reports_null_when_unset() -> None:
    tenant = _tenant()  # all three new columns default to None
    db = _DB(tenant)
    current_user = SimpleNamespace(tenant_id=tenant.id)

    result = await enterprise.get_tenant_quotas(current_user=current_user, db=db)  # type: ignore[arg-type]

    assert result["max_tokens_per_day"] is None
    assert result["default_agent_max_tokens_per_day"] is None
    assert result["default_agent_max_tokens_per_month"] is None
